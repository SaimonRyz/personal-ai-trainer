from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from trainer_mcp import server
from trainer_mcp.contracts import MeasurementCreateDraftInput

from trainer.transport.http.contracts import ConfirmRequest, PreviewRequest, PreviewShownRequest


@pytest.mark.parametrize("cross_dialog", [False, True], ids=["same-dialog", "cross-dialog"])
def test_measurement_draft_requires_full_preview_shown_then_explicit_confirm(
    monkeypatch: pytest.MonkeyPatch, cross_dialog: bool
) -> None:
    revision_id = uuid4()
    preview_id = uuid4()
    preview_digest = "a" * 64
    canonical_text = "Полный синтетический предпросмотр замера: 84.2 kg; дата 2026-10-10."
    shown_conversation_id = (
        "synthetic-followup-conversation" if cross_dialog else "synthetic-conversation"
    )
    shown_message_id = "synthetic-reshown-message"
    calls: list[tuple[str, dict[str, Any]]] = []
    envelopes: dict[str, dict[str, Any]] = {}
    shown = False

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: Any,
        causation_id: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        nonlocal shown
        calls.append((path, body.copy()))
        if path == "/api/v1/commands/measurement-session-drafts":
            envelope = {"data": {"revision_id": str(revision_id)}, "meta": {"v": 1}}
        elif path == "/api/v1/commands/previews":
            PreviewRequest.model_validate(body)
            assert body["revision_id"] == str(revision_id)
            assert body["detail_level"] == "full"
            envelope = {
                "data": {
                    "preview_id": str(preview_id),
                    "revision_id": str(revision_id),
                    "preview_digest": preview_digest,
                    "canonical_text": canonical_text,
                },
                "meta": {"v": 1},
            }
        elif path == f"/api/v1/commands/previews/{preview_id}/shown":
            assert body["revision_id"] == str(revision_id)
            assert body["preview_digest"] == preview_digest
            assert body["cross_dialog"] is cross_dialog
            assert (
                body["displayed_text_digest"] == hashlib.sha256(canonical_text.encode()).hexdigest()
            )
            PreviewShownRequest.model_validate(body)
            shown = True
            envelope = {"data": {"shown": True}, "meta": {"v": 1}}
        elif path == f"/api/v1/commands/previews/{preview_id}/confirm":
            assert shown, "confirmation must not precede acknowledgement that preview was shown"
            assert body["revision_id"] == str(revision_id)
            assert body["preview_digest"] == preview_digest
            assert body["explicit_intent"] == "Подтверждаю именно этот замер"
            assert body["confirmation_mode"] == ("cross_dialog" if cross_dialog else "same_dialog")
            assert body["referenced_preview_id"] == (str(preview_id) if cross_dialog else None)
            assert body["reshown_message_id"] == (shown_message_id if cross_dialog else None)
            ConfirmRequest.model_validate(body)
            envelope = {"data": {"committed": True}, "meta": {"v": 1}}
        else:
            raise AssertionError(f"Unexpected route: {path}")
        envelopes[path] = envelope
        return envelope

    monkeypatch.setattr(server, "api_call", fake_api_call)
    draft_tool = next(
        tool
        for tool in server.mcp._tool_manager.list_tools()
        if tool.name == "create_measurement_session_draft"
    )
    draft = draft_tool.fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-draft-message",
        timezone="Asia/Novokuznetsk",
        measurement=MeasurementCreateDraftInput.model_validate(
            {
                "intent": "create",
                "session": {
                    "measured_on": date(2026, 10, 10),
                    "measured_time": None,
                    "conditions": None,
                    "comment": None,
                    "values": [
                        {
                            "measurement_kind": "body_mass",
                            "original_value": "84.2",
                            "original_unit": "kilogram",
                            "side": None,
                            "method": None,
                            "comment": None,
                        }
                    ],
                },
            }
        ),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-measurement-draft",
    )
    assert draft == envelopes["/api/v1/commands/measurement-session-drafts"]

    preview = server.render_preview(
        conversation_id="synthetic-conversation",
        message_id="synthetic-preview-message",
        timezone="Asia/Novokuznetsk",
        revision_id=revision_id,
        detail_level="full",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-preview",
    )
    assert preview["data"]["canonical_text"] == canonical_text

    shown_result = server.acknowledge_preview_shown(
        preview_id=preview_id,
        revision_id=revision_id,
        preview_digest=preview_digest,
        canonical_text=canonical_text,
        conversation_id=shown_conversation_id,
        shown_message_id=shown_message_id,
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-shown",
        timezone="Asia/Novokuznetsk",
        cross_dialog=cross_dialog,
    )
    assert shown_result == envelopes[f"/api/v1/commands/previews/{preview_id}/shown"]

    confirmed = server.confirm_preview(
        preview_id=preview_id,
        revision_id=revision_id,
        preview_digest=preview_digest,
        conversation_id=shown_conversation_id,
        confirmation_message_id="synthetic-confirmation-message",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-confirmation",
        timezone="Asia/Novokuznetsk",
        explicit_intent="Подтверждаю именно этот замер",
        confirmation_mode="cross_dialog" if cross_dialog else "same_dialog",
        referenced_preview_id=preview_id if cross_dialog else None,
        reshown_message_id=shown_message_id if cross_dialog else None,
    )
    assert confirmed == envelopes[f"/api/v1/commands/previews/{preview_id}/confirm"]
    assert [path for path, _ in calls] == [
        "/api/v1/commands/measurement-session-drafts",
        "/api/v1/commands/previews",
        f"/api/v1/commands/previews/{preview_id}/shown",
        f"/api/v1/commands/previews/{preview_id}/confirm",
    ]

    for _, body in calls:
        digest = body.pop("request_digest")
        expected = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert digest == expected
