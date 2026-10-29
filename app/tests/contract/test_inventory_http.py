from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, TypeAdapter, ValidationError

from trainer.application.context import ContextService
from trainer.config.settings import Settings
from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import ContextRepository
from trainer.infrastructure.db.inventory import InventoryRepository
from trainer.transport.http.app import create_app
from trainer.transport.http.contracts import InventoryChangeDraftRequest

TOKEN = "synthetic-inventory-fixture-32-chars"


def request_body() -> dict[str, Any]:
    return {
        "context": {
            "conversation_id": "synthetic-inventory",
            "message_id": "draft-1",
            "effective_at": "2026-10-07T00:00:00+00:00",
            "timezone": "UTC",
            "locale": None,
        },
        "intent": "create",
        "equipment": {
            "equipment_type": "barbell",
            "name": "Synthetic barbell",
            "maker": None,
            "brand": None,
            "model": None,
            "quantity": None,
            "quantity_unit_code": None,
            "condition": None,
            "comment": None,
            "characteristics": {
                "mass_per_item_kg": None,
                "bar_sleeve_diameter_mm": None,
                "plate_bore_diameter_mm": None,
            },
        },
    }


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
        "Idempotency-Key": "synthetic-inventory-create",
    }


def test_cmd09_dispatches_a_draft_and_never_a_direct_commit() -> None:
    calls = []

    def executor(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "draft_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "revision_no": 1,
            "content_digest": "a" * 64,
            "dependencies": [],
            "gaps": [],
            "preview_candidate": {
                "summary": "Synthetic full preview",
                "complete": True,
                "confirmation_kind": "normal",
            },
        }

    client = TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), context_service=ContextService(executor))
    )
    body = request_body()
    normalized = (
        TypeAdapter(InventoryChangeDraftRequest)
        .validate_python({**body, "request_digest": "0" * 64})
        .model_dump(mode="json")
    )
    normalized.pop("request_digest")
    response = client.post(
        "/api/v1/commands/inventory-change-drafts",
        headers=headers(),
        json={**normalized, "request_digest": digest_payload(normalized)},
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["action"] == "create_inventory_change_draft"
    assert calls[0]["payload"]["equipment"]["condition"] is None
    assert "commit_result_id" not in response.json()["data"]


@pytest.mark.parametrize("extra", ["equipment_id", "seller", "place_ref", "quantity_delta"])
def test_create_has_no_client_identity_or_excluded_fields(extra: str) -> None:
    body = request_body()
    body.update({"request_digest": "0" * 64, extra: str(uuid4())})
    with pytest.raises(ValidationError):
        TypeAdapter(InventoryChangeDraftRequest).validate_python(body)


def test_correction_requires_target_exact_revision_and_nonblank_reason() -> None:
    body = {
        **request_body(),
        "intent": "correct",
        "request_digest": "0" * 64,
        "equipment_id": str(uuid4()),
        "expected_current_revision_id": str(uuid4()),
        "reason": "Owner corrected the full card",
    }
    TypeAdapter(InventoryChangeDraftRequest).validate_python(body)
    for key in ("equipment_id", "expected_current_revision_id", "reason"):
        invalid = {k: value for k, value in body.items() if k != key}
        with pytest.raises(ValidationError):
            TypeAdapter(InventoryChangeDraftRequest).validate_python(invalid)
    with pytest.raises(ValidationError):
        TypeAdapter(InventoryChangeDraftRequest).validate_python({**body, "reason": "   "})


def test_stale_expected_inventory_revision_has_exact_http_error_code() -> None:
    profile_id, item_id, current_id, stale_id = uuid4(), uuid4(), uuid4(), uuid4()

    class Result:
        def mappings(self) -> Result:
            return self

        def one_or_none(self) -> dict[str, Any]:
            return {"profile_id": profile_id, "current_revision_id": uuid4()}

    class Session:
        def execute(self, *_: Any) -> Result:
            return Result()

    repository = ContextRepository(Session())  # type: ignore[arg-type]

    def executor(**kwargs: Any) -> dict[str, Any]:
        assert kwargs.pop("action") == "create_inventory_change_draft"
        return InventoryRepository(repository).create_draft(**kwargs)

    body = {
        **request_body(),
        "intent": "correct",
        "equipment_id": str(item_id),
        "expected_current_revision_id": str(stale_id),
        "reason": "Synthetic correction",
    }
    normalized = (
        TypeAdapter(InventoryChangeDraftRequest)
        .validate_python({**body, "request_digest": "0" * 64})
        .model_dump(mode="json")
    )
    normalized.pop("request_digest")
    client = TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), context_service=ContextService(executor))
    )
    current = [
        {
            "ref": {
                "object_type": "equipment_item",
                "object_id": str(item_id),
                "revision_id": str(current_id),
            },
            "equipment": body["equipment"],
        }
    ]
    with (
        patch.object(repository, "_reserve", return_value=(uuid4(), None)),
        patch.object(InventoryRepository, "current", return_value=current),
    ):
        response = client.post(
            "/api/v1/commands/inventory-change-drafts",
            headers=headers(),
            json={**normalized, "request_digest": digest_payload(normalized)},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_dependency"
    assert response.json()["error"]["required_next_action"] == "read_current_profile_context"
