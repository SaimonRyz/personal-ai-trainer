from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from trainer_mcp import server
from trainer_mcp.write_tools import EvidenceInput, SourceAvailabilityUpdateInput

from trainer.application.context import ContextService
from trainer.transport.http.evidence_contracts import EvidenceDraftRequest


def _evidence() -> EvidenceInput:
    return EvidenceInput.model_validate(
        {
            "sources": [
                {
                    "source": {
                        "citation_key": "synthetic-review-2024",
                        "title": "Synthetic review for adapter contract testing",
                        "authors_or_org": "Synthetic Test Group",
                        "publication_year": 2024,
                        "source_type": "systematic_review",
                        "doi": "10.5555/synthetic.2024.1",
                        "pmid": None,
                        "url": None,
                        "metadata_snapshot": {"retrieved_for_test": True},
                        "availability": "synthetic_fixture",
                        "conflict_of_interest_note": None,
                    },
                    "relation": "supports",
                    "cited_locator": "abstract",
                    "note": "Synthetic evidence; not a real scientific claim",
                }
            ],
            "conclusion": {
                "conclusion_id": None,
                "topic": "synthetic adapter topic",
                "statement": "Synthetic conclusion stored as an unverified draft",
                "population_conditions": "Synthetic fixture only",
                "confidence": "insufficient",
                "review_due_on": date(2027, 1, 1),
                "applicability": [{"scope": "synthetic"}],
                "caveats": [{"limitation": "Not verified"}],
            },
        }
    )


def test_cmd14_forwards_closed_evidence_draft_to_exact_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    envelope = {"data": {"draft_id": "synthetic"}, "meta": {"api_version": "v1"}}

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: Any,
        causation_id: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captured.update(path=path, body=body, idempotency_key=idempotency_key)
        return envelope

    monkeypatch.setattr(server, "api_call", fake_api_call)
    tool = next(
        item
        for item in server.mcp._tool_manager.list_tools()
        if item.name == "create_evidence_draft"
    )
    result = tool.fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-evidence-message",
        timezone="Asia/Novokuznetsk",
        evidence=_evidence(),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-evidence-draft",
    )

    body = captured["body"]
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert captured["path"] == "/api/v1/commands/evidence-drafts"
    assert captured["path"] in server.ALLOWED_STATIC_ROUTES
    assert captured["idempotency_key"] == "synthetic-evidence-draft"
    assert digest == expected
    request = EvidenceDraftRequest.model_validate({**body, "request_digest": digest})
    ContextService.verify_digest({**body, "request_digest": digest}, digest)
    assert request.evidence.conclusion.confidence == "insufficient"
    assert result is envelope


def test_evidence_draft_cannot_assign_verified_status_or_owner_capability() -> None:
    evidence = _evidence().model_dump(mode="python")
    evidence["conclusion"]["status"] = "verified"
    with pytest.raises(ValidationError):
        EvidenceInput.model_validate(evidence)

    tool = next(
        item
        for item in asyncio.run(server.mcp.list_tools())
        if item.name == "create_evidence_draft"
    )
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.openWorldHint is False
    schema = json.dumps(tool.inputSchema)
    assert '"status"' not in schema
    assert "owner_verification" not in schema
    tool_names = {item.name for item in asyncio.run(server.mcp.list_tools())}
    assert "prepare_conclusion_verification" not in tool_names


def test_evidence_draft_rejects_duplicate_source_relation_pairs() -> None:
    evidence = _evidence().model_dump(mode="python")
    evidence["sources"].append(evidence["sources"][0].copy())
    with pytest.raises(ValidationError, match="source relations must be unique"):
        EvidenceInput.model_validate(evidence)


def test_source_availability_tool_forwards_one_closed_cmd14_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        server,
        "api_call",
        lambda path, body, correlation_id, causation_id, idempotency_key=None: captured.update(
            path=path, body=body, idempotency_key=idempotency_key
        ) or {"accepted": True},
    )
    tool = next(
        item
        for item in server.mcp._tool_manager.list_tools()
        if item.name == "update_source_availability_draft"
    )
    response = tool.fn(
        conversation_id="synthetic-conversation",
        message_id="availability-message",
        timezone="UTC",
        update=SourceAvailabilityUpdateInput.model_validate(
            {
                "source_id": str(uuid4()),
                "expected_availability": "available",
                "expected_last_checked_at": None,
                "new_availability": "unavailable",
                "reason": "Synthetic test",
            }
        ),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-availability",
    )
    body = captured["body"]
    digest = body.pop("request_digest")
    assert captured["path"] == "/api/v1/commands/evidence-drafts"
    assert captured["idempotency_key"] == "synthetic-availability"
    assert digest == server.canonical_digest(body)
    from trainer.transport.http.evidence_contracts import EvidenceCommandRequest

    parsed = TypeAdapter(EvidenceCommandRequest).validate_python(
        {**body, "request_digest": digest}
    )
    assert parsed.source_availability_update.new_availability == "unavailable"
    assert response == {"accepted": True}
