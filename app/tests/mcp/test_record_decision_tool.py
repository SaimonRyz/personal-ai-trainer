from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from trainer_mcp import server
from trainer_mcp.contracts import RecordDecisionInput

from trainer.application.context import ContextService
from trainer.transport.http.contracts import RecordDecisionDraftRequest


def test_record_decision_tool_builds_cmd08_draft_and_preserves_envelope(
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
        captured.update(
            path=path,
            body=body,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )
        return envelope

    monkeypatch.setattr(server, "api_call", fake_api_call)
    tool = next(
        item
        for item in server.mcp._tool_manager.list_tools()
        if item.name == "create_record_decision_draft"
    )
    result = tool.fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-message",
        timezone="Asia/Novokuznetsk",
        decision=RecordDecisionInput(
            candidate_id=uuid4(),
            decision="accept",
            technique_self_confirmation=True,
            reason="Подтверждаю точный кандидат",
        ),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-record-decision",
    )

    body = captured["body"]
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert captured["path"] == "/api/v1/commands/record-decision-drafts"
    assert captured["idempotency_key"] == "synthetic-record-decision"
    assert digest == expected
    request = {**body, "request_digest": digest}
    RecordDecisionDraftRequest.model_validate(request)
    ContextService.verify_digest(request, digest)
    assert result is envelope


def test_record_decision_tool_rejects_accept_without_user_attestation() -> None:
    with pytest.raises(ValidationError, match="technique self-confirmation"):
        RecordDecisionInput(
            candidate_id=uuid4(),
            decision="accept",
            technique_self_confirmation=False,
        )
