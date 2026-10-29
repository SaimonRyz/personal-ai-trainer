from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from trainer_mcp import server
from trainer_mcp.contracts import CalculationInput, WorkoutHistoryInput

from trainer.transport.http.progress_contracts import CalculationRequest


def test_calculation_tool_builds_exact_cmd17_request_and_preserves_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    envelope = {"data": {"state": "ephemeral"}, "meta": {"api_version": "v1"}}

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
        item for item in server.mcp._tool_manager.list_tools() if item.name == "calculate_metric"
    )
    result = tool.fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-message",
        timezone="Asia/Novokuznetsk",
        calculation=CalculationInput.model_validate(
            {
                "method_id": "external_working_set_volume",
                "method_version": "1",
                "mode": "ephemeral",
                "source": {
                    "kind": "exercise_session",
                    "session_id": uuid4(),
                    "session_revision_id": uuid4(),
                    "exercise_id": uuid4(),
                },
            }
        ),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-calculation",
    )

    body = captured["body"]
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert captured["path"] == "/api/v1/commands/calculations"
    assert captured["idempotency_key"] == "synthetic-calculation"
    assert digest == expected
    CalculationRequest.model_validate({**body, "request_digest": digest})
    assert result is envelope


def test_calculation_input_fails_closed_for_invalid_method_or_attestation_binding() -> None:
    source_id = uuid4()
    with pytest.raises(ValidationError, match="calculation-time execution attestation"):
        CalculationInput.model_validate(
            {
                "method_id": "e1rm_brzycki",
                "method_version": "1",
                "mode": "ephemeral",
                "source": {
                    "kind": "actual_set",
                    "session_id": uuid4(),
                    "session_revision_id": uuid4(),
                    "exercise_id": uuid4(),
                    "exercise_revision_id": uuid4(),
                    "actual_set_id": source_id,
                },
            }
        )

    with pytest.raises(ValidationError, match="read-time only"):
        CalculationInput.model_validate(
            {
                "method_id": "external_working_set_volume",
                "method_version": "1",
                "mode": "persist",
                "source": {
                    "kind": "exercise_session",
                    "session_id": uuid4(),
                    "session_revision_id": uuid4(),
                    "exercise_id": uuid4(),
                },
            }
        )


def test_persistent_e1rm_uses_exact_set_references_from_confirmed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "20000000-0000-4000-8000-000000000030"
    session_revision_id = "20000000-0000-4000-8000-000000000031"
    exercise_id = "20000000-0000-4000-8000-000000000039"
    exercise_revision_id = "20000000-0000-4000-8000-000000000043"
    actual_set_id = "20000000-0000-4000-8000-000000000041"
    history_envelope = {
        "data": {
            "response": {
                "items": [
                    {
                        "session_ref": {
                            "object_type": "workout_session",
                            "object_id": session_id,
                            "revision_id": session_revision_id,
                        },
                        "workout": {
                            "session_id": session_id,
                            "session_revision_id": session_revision_id,
                            "blocks": [
                                {
                                    "exercises": [
                                        {
                                            "exercise_id": exercise_id,
                                            "exercise_revision_id": exercise_revision_id,
                                            "sets": [{"actual_set_id": actual_set_id}],
                                        }
                                    ]
                                }
                            ],
                        },
                    }
                ],
                "next_cursor": None,
                "snapshot_id": "20000000-0000-4000-8000-000000000099",
                "snapshot_confirmed_at": "2026-10-11T10:00:00Z",
                "total_count": 1,
            }
        },
        "meta": {"api_version": "v1"},
    }
    calculation_envelope = {
        "data": {"state": "draft", "synthetic": True},
        "meta": {"api_version": "v1"},
    }
    captured: list[dict[str, Any]] = []

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: Any,
        causation_id: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captured.append({"path": path, "body": body.copy(), "idempotency_key": idempotency_key})
        if path.endswith("/workouts/history"):
            return history_envelope
        return calculation_envelope

    monkeypatch.setattr(server, "api_call", fake_api_call)
    tools = {item.name: item for item in server.mcp._tool_manager.list_tools()}
    history = tools["get_workout_history"].fn(
        conversation_id="synthetic-e1rm-conversation",
        message_id="synthetic-history-read",
        timezone="Asia/Novokuznetsk",
        query=WorkoutHistoryInput(mode="all_confirmed", limit=100),
        correlation_id=uuid4(),
        causation_id=uuid4(),
    )
    workout = history["data"]["response"]["items"][0]["workout"]
    actual_set = workout["blocks"][0]["exercises"][0]["sets"][0]
    source = {
        "kind": "actual_set",
        "session_id": workout["session_id"],
        "session_revision_id": workout["session_revision_id"],
        "exercise_id": workout["blocks"][0]["exercises"][0]["exercise_id"],
        "exercise_revision_id": workout["blocks"][0]["exercises"][0]["exercise_revision_id"],
        "actual_set_id": actual_set["actual_set_id"],
    }
    calculation = CalculationInput.model_validate(
        {
            "method_id": "e1rm_brzycki",
            "method_version": "1",
            "mode": "persist",
            "source": source,
            "execution_attestation": {
                "source_actual_set_id": actual_set["actual_set_id"],
                "grip_condition": "synthetic standard grip",
                "bench_pause_condition": "intentional_chest_pause",
                "standard_confirmed": True,
                "spotter_released_before_rep": True,
                "no_assistance_during_rep": True,
            },
            "prior_derived_value_id": None,
        }
    )

    result = tools["calculate_metric"].fn(
        conversation_id="synthetic-e1rm-conversation",
        message_id="synthetic-e1rm-draft",
        timezone="Asia/Novokuznetsk",
        calculation=calculation,
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-e1rm-from-history",
    )

    assert captured[0]["path"] == "/api/v1/queries/workouts/history"
    assert captured[1]["path"] == "/api/v1/commands/calculations"
    assert captured[1]["body"]["source"] == source
    assert captured[1]["body"]["execution_attestation"]["source_actual_set_id"] == actual_set_id
    assert captured[1]["idempotency_key"] == "synthetic-e1rm-from-history"
    body = captured[1]["body"].copy()
    digest = body.pop("request_digest")
    CalculationRequest.model_validate({**body, "request_digest": digest})
    assert result is calculation_envelope
