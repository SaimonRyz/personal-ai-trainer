from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, TypeAdapter, ValidationError

from trainer.application.context import ContextService
from trainer.config.settings import Settings
from trainer.domain.context import digest_payload
from trainer.domain.evidence import PROTOCOL_CHECKS, PROTOCOL_VERSION
from trainer.transport.http.app import create_app
from trainer.transport.http.evidence_contracts import (
    ConclusionSourceView,
    EvidenceAvailabilityDraftRequest,
    EvidenceCommandRequest,
    EvidenceDraftRequest,
    VerificationDraftRequest,
)

TOKEN = "synthetic-agent-credential-at-least-32-characters"
OWNER = "synthetic-owner-credential-at-least-32-characters"


def request() -> dict[str, Any]:
    body = VerificationDraftRequest.model_validate(
        {
            "context": {
                "conversation_id": "synthetic",
                "message_id": "verify",
                "effective_at": "2026-10-09T00:00:00Z",
                "timezone": "UTC",
            },
            "request_digest": "0" * 64,
            "conclusion_version_id": str(uuid4()),
            "protocol_version": PROTOCOL_VERSION,
            "checklist": [{"check_id": code, "result": True} for code in sorted(PROTOCOL_CHECKS)],
            "decision": "verified",
            "reason": "Owner checked synthetic fixture",
        }
    ).model_dump(mode="json")
    body.pop("request_digest")
    return {**body, "request_digest": digest_payload(body)}


def headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


def test_owner_credential_is_separate_and_optional() -> None:
    for value in (None, "", "null"):
        assert (
            Settings(api_token=SecretStr(TOKEN), owner_verifier_token=value).owner_verifier_token
            is None
        )
    with pytest.raises(ValidationError):
        Settings(api_token=SecretStr(TOKEN), owner_verifier_token=SecretStr(TOKEN))


def test_agent_cannot_self_verify_or_forge_a_role() -> None:
    calls = []

    def executor(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"recorded": True}

    client = TestClient(
        create_app(
            Settings(api_token=SecretStr(TOKEN), owner_verifier_token=SecretStr(OWNER)),
            context_service=ContextService(executor),
        )
    )
    response = client.post(
        "/api/v1/commands/conclusion-verification-drafts", headers=headers(TOKEN), json=request()
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "capability_required"
    forged = {**request(), "owner_verifier": True}
    assert (
        client.post(
            "/api/v1/commands/conclusion-verification-drafts", headers=headers(TOKEN), json=forged
        ).status_code
        == 400
    )
    assert all(item["action"] == "record_failed_request" for item in calls)


def test_owner_capability_is_server_derived_and_not_echoed() -> None:
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
                "summary": "Owner review",
                "confirmation_kind": "owner_verification",
                "complete": True,
            },
        }

    client = TestClient(
        create_app(
            Settings(api_token=SecretStr(TOKEN), owner_verifier_token=SecretStr(OWNER)),
            context_service=ContextService(executor),
        )
    )
    response = client.post(
        "/api/v1/commands/conclusion-verification-drafts", headers=headers(OWNER), json=request()
    )
    assert response.status_code == 200
    assert calls[0]["owner_verifier"] is True
    assert OWNER not in response.text and TOKEN not in response.text


def test_reserved_recommendation_and_sql_routes_are_absent() -> None:
    client = TestClient(create_app(Settings(api_token=SecretStr(TOKEN))))
    for route in ("recommendation-drafts", "batch-drafts", "execute-sql"):
        assert (
            client.post(f"/api/v1/commands/{route}", headers=headers(TOKEN), json={}).status_code
            == 404
        )


def test_cmd14_availability_draft_is_closed_and_uses_expected_source_state() -> None:
    body = {
        "context": {
            "conversation_id": "synthetic",
            "message_id": "availability",
            "effective_at": "2026-10-22T12:00:00Z",
            "timezone": "UTC",
        },
        "request_digest": "0" * 64,
        "source_availability_update": {
            "source_id": str(uuid4()),
            "expected_availability": "available",
            "expected_last_checked_at": None,
            "new_availability": "unavailable",
            "reason": "The saved URL returned an unavailable page.",
        },
    }

    parsed = EvidenceAvailabilityDraftRequest.model_validate(body)
    assert parsed.source_availability_update.expected_last_checked_at is None
    assert parsed.source_availability_update.new_availability == "unavailable"
    command = TypeAdapter(EvidenceCommandRequest).validate_python(body)
    assert isinstance(command, EvidenceAvailabilityDraftRequest)
    assert "source_availability_update" in command.model_dump(mode="json")
    content_command = TypeAdapter(EvidenceCommandRequest).validate_python(
        {
            "context": body["context"],
            "request_digest": body["request_digest"],
            "evidence": {
                "sources": [
                    {
                        "source": {
                            "citation_key": "synthetic-contract-source",
                            "title": "Synthetic source",
                            "authors_or_org": "Synthetic organization",
                            "publication_year": 2026,
                            "source_type": "controlled_trial",
                            "doi": None,
                            "pmid": None,
                            "url": "https://example.invalid/source",
                            "metadata_snapshot": {"synthetic": True},
                            "availability": "available",
                            "conflict_of_interest_note": None,
                        },
                        "relation": "supports",
                        "cited_locator": None,
                        "note": None,
                    }
                ],
                "conclusion": {
                    "conclusion_id": None,
                    "topic": "synthetic topic",
                    "statement": "Synthetic statement",
                    "population_conditions": "Synthetic population",
                    "confidence": "insufficient",
                    "review_due_on": "2027-01-01",
                    "applicability": [],
                    "caveats": [],
                },
            },
        }
    )
    assert isinstance(content_command, EvidenceDraftRequest)

    with pytest.raises(ValidationError):
        EvidenceAvailabilityDraftRequest.model_validate(
            {
                **body,
                "source_availability_update": {
                    **body["source_availability_update"],
                    "status": "verified",
                },
            }
        )


def test_qry09_separates_current_availability_from_immutable_snapshot() -> None:
    view = ConclusionSourceView.model_validate(
        {
            "source_ref": {"object_type": "scientific_source", "object_id": str(uuid4())},
            "relation": "supports",
            "cited_locator": None,
            "metadata_snapshot": {
                "title": "Saved title",
                "url": "https://example.invalid/paper",
                "availability": "available",
            },
            "current_availability": "unavailable",
            "last_checked_at": "2026-10-22T12:00:00Z",
        }
    )
    assert view.metadata_snapshot["availability"] == "available"
    assert view.current_availability == "unavailable"
    assert view.last_checked_at is not None

    legacy = ConclusionSourceView.model_validate(
        {
            "source_ref": {"object_type": "scientific_source", "object_id": str(uuid4())},
            "relation": "supports",
            "cited_locator": None,
            "metadata_snapshot": {"availability": "unavailable"},
            "current_availability": "unavailable",
            "last_checked_at": None,
        }
    )
    assert legacy.metadata_snapshot["availability"] == "unavailable"
    assert legacy.current_availability == "unavailable"
    assert legacy.last_checked_at is None
