from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from trainer_mcp import server
from trainer_mcp.contracts import WorkoutCorrectionDraftInput, WorkoutHistoryInput

from trainer.transport.http.contracts import WorkoutCorrectionRequest


def _correction() -> WorkoutCorrectionDraftInput:
    return WorkoutCorrectionDraftInput.model_validate(
        {
            "target": {
                "object_type": "workout_session",
                "object_id": uuid4(),
                "revision_id": uuid4(),
            },
            "patch": [
                {
                    "op": "replace",
                    "path": "/report",
                    "value": {
                        "plan_id": None,
                        "plan_revision_id": None,
                        "started_at": None,
                        "ended_at": None,
                        "local_start_date": date(2026, 10, 1),
                        "duration_seconds": None,
                        "session_type": "training",
                        "name": None,
                        "lifecycle": "completed",
                        "effort": None,
                        "comment": None,
                        "workout_metrics": [],
                        "blocks": [],
                        "confirmed_unknown_planned_exercise_ids": [],
                        "confirmed_unknown_planned_set_ids": [],
                        "confirmed_unknown_planned_load_component_ids": [],
                        "confirmed_unknown_metrics": [],
                    },
                }
            ],
            "reason": "Исправляю дату и подтверждаю полный отчёт",
        }
    )


def test_workout_correction_tool_builds_cmd07_exact_full_report_request(
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
        if item.name == "create_workout_correction_draft"
    )
    result = tool.fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-message",
        timezone="Asia/Novokuznetsk",
        correction=_correction(),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-correction",
    )

    body = captured["body"]
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert captured["path"] == "/api/v1/commands/correction-drafts"
    assert captured["idempotency_key"] == "synthetic-correction"
    assert digest == expected
    WorkoutCorrectionRequest.model_validate({**body, "request_digest": digest})
    assert result is envelope


def test_workout_correction_dto_rejects_partial_or_unrelated_patch() -> None:
    correction = _correction().model_dump(mode="python")
    correction["patch"][0]["path"] = "/comment"
    with pytest.raises(ValidationError):
        WorkoutCorrectionDraftInput.model_validate(correction)


def test_workout_correction_can_use_exact_reference_returned_by_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_ref = {
        "object_type": "workout_session",
        "object_id": "20000000-0000-4000-8000-000000000030",
        "revision_id": "20000000-0000-4000-8000-000000000031",
    }
    history_envelope = {
        "data": {
            "response": {
                "items": [{"session_ref": source_ref}],
                "next_cursor": None,
                "snapshot_id": "20000000-0000-4000-8000-000000000099",
                "snapshot_confirmed_at": "2026-10-11T10:00:00Z",
                "total_count": 1,
            }
        },
        "meta": {"api_version": "v1"},
    }
    correction_envelope = {"data": {"draft_id": "synthetic-correction"}, "meta": {}}
    captured: list[dict[str, Any]] = []

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: Any,
        causation_id: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captured.append(
            {
                "path": path,
                "body": body.copy(),
                "idempotency_key": idempotency_key,
            }
        )
        return history_envelope if path.endswith("/workouts/history") else correction_envelope

    monkeypatch.setattr(server, "api_call", fake_api_call)
    tools = {item.name: item for item in server.mcp._tool_manager.list_tools()}
    history = tools["get_workout_history"].fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-history-read",
        timezone="Asia/Novokuznetsk",
        query=WorkoutHistoryInput(mode="all_confirmed", limit=100),
        correlation_id=uuid4(),
        causation_id=uuid4(),
    )
    returned_ref = history["data"]["response"]["items"][0]["session_ref"]
    correction_data = _correction().model_dump(mode="python")
    correction_data["target"] = returned_ref
    correction = WorkoutCorrectionDraftInput.model_validate(correction_data)

    result = tools["create_workout_correction_draft"].fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-correction-draft",
        timezone="Asia/Novokuznetsk",
        correction=correction,
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-exact-history-correction",
    )

    assert captured[0]["path"] == "/api/v1/queries/workouts/history"
    assert captured[0]["body"]["mode"] == "all_confirmed"
    assert captured[1]["path"] == "/api/v1/commands/correction-drafts"
    assert captured[1]["body"]["target"] == source_ref
    assert captured[1]["idempotency_key"] == "synthetic-exact-history-correction"
    WorkoutCorrectionRequest.model_validate(captured[1]["body"])
    assert result is correction_envelope
