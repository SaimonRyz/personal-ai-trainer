"""CMD-11/QRY-13 closed HTTP contract checks without database state."""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr, TypeAdapter, ValidationError

from trainer.application.context import ContextService
from trainer.config.settings import Settings
from trainer.domain.context import digest_payload
from trainer.transport.http.app import create_app
from trainer.transport.http.measurement_contracts import (
    MeasurementDraftRequest,
    MeasurementQueryRequest,
)

TOKEN = "synthetic-measurement-fixture-32-chars"


def context() -> dict[str, object]:
    return {
        "conversation_id": "synthetic-measures",
        "message_id": "one",
        "effective_at": "2026-10-08T00:00:00+00:00",
        "timezone": "UTC",
        "locale": None,
    }


def value(
    kind: str,
    original: str,
    unit: str,
    side: str | None = None,
    method: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "measurement_kind": kind,
        "original_value": original,
        "original_unit": unit,
        "side": side,
        "method": method,
        "comment": None,
    }


def draft_body() -> dict[str, object]:
    return {
        "context": context(),
        "intent": "create",
        "session": {
            "measured_on": "2026-10-08",
            "measured_time": None,
            "conditions": None,
            "comment": None,
            "values": [value("body_mass", "80", "kilogram"), value("height", "180", "centimeter")],
        },
    }


def headers(*, command: bool) -> dict[str, str]:
    result = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
    }
    if command:
        result["Idempotency-Key"] = str(uuid4())
    return result


def test_draft_dispatches_only_after_validated_digest() -> None:
    calls: list[dict[str, Any]] = []

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
                "summary": "synthetic full preview",
                "complete": True,
                "confirmation_kind": "normal",
            },
        }

    client = TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), context_service=ContextService(executor))
    )
    body = draft_body()
    normalized = (
        TypeAdapter(MeasurementDraftRequest)
        .validate_python({**body, "request_digest": "0" * 64})
        .model_dump(mode="json")
    )
    normalized.pop("request_digest")
    normalized["request_digest"] = digest_payload(normalized)
    response = client.post(
        "/api/v1/commands/measurement-session-drafts",
        json=normalized,
        headers=headers(command=True),
    )
    assert response.status_code == 200, response.json()
    assert calls[0]["action"] == "create_measurement_session_draft"
    tampered = deepcopy(normalized)
    tampered["session"]["values"][0]["original_value"] = "81"  # type: ignore[index]
    response = client.post(
        "/api/v1/commands/measurement-session-drafts", json=tampered, headers=headers(command=True)
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "digest_mismatch"
    assert [call["action"] for call in calls].count("create_measurement_session_draft") == 1


def test_closed_method_unit_side_and_query_shapes() -> None:
    adapter = TypeAdapter(MeasurementDraftRequest)
    valid = {**draft_body(), "request_digest": "0" * 64}
    for changed in (
        {
            "measurement_kind": "body_mass",
            "original_value": "10",
            "original_unit": "inch",
            "side": None,
            "method": None,
            "comment": None,
        },
        value("body_mass", "10", "kilogram", "left"),
        value("height", "180", "centimeter", None, {"mode": "new", "description": "a"}),
        value(
            "waist_circumference",
            "80",
            "centimeter",
            None,
            {"mode": "new", "description": "a", "method_id": str(uuid4())},
        ),
    ):
        bad = deepcopy(valid)
        bad["session"]["values"][0] = changed  # type: ignore[index]
        try:
            adapter.validate_python(bad)
        except (ValidationError, ValueError):
            pass
        else:
            raise AssertionError("invalid measurement accepted")
    query = TypeAdapter(MeasurementQueryRequest)
    query.validate_python({"context": context(), "mode": "bmi"})
    for bad in (
        {"context": context(), "mode": "bmi", "measurement_kind": "height"},
        {
            "context": context(),
            "mode": "series",
            "measurement_kind": "waist_circumference",
            "side": None,
            "method_source_value_id": None,
            "date_from": None,
            "date_to": None,
        },
    ):
        try:
            query.validate_python(bad)
        except ValidationError:
            pass
        else:
            raise AssertionError("invalid query accepted")


def test_isolated_flow_helper_selects_cmd11_dto_before_http(monkeypatch: Any) -> None:
    """No Docker: the shared integration helper must not parse CMD-11 as a program."""
    monkeypatch.setenv("TRAINER_TEST_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TRAINER_API_TOKEN", TOKEN)
    path = Path(__file__).parents[1] / "integration" / "program_flow.py"
    spec = spec_from_file_location("synthetic_program_flow_helper", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: list[dict[str, Any]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"data":{}}'

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        assert timeout == 20
        captured.append(json.loads(request.data))
        return FakeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    module.post("/api/v1/commands/measurement-session-drafts", draft_body(), "eg06-dto-check")
    assert len(captured) == 1
    assert captured[0]["session"]["values"][0]["original_value"] == "80"
    digest = captured[0].pop("request_digest")
    assert digest == digest_payload(captured[0])
