from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from trainer_mcp import server
from trainer_mcp.contracts import (
    MeasurementCorrectDraftInput,
    MeasurementCreateDraftInput,
)

from trainer.application.context import ContextService
from trainer.transport.http.measurement_contracts import MeasurementDraftRequest


def _invoke_measurement_tool(
    monkeypatch: pytest.MonkeyPatch,
    measurement: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        if item.name == "create_measurement_session_draft"
    )
    result = tool.fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-message",
        timezone="Asia/Novokuznetsk",
        measurement=measurement,
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-measurement-draft",
    )
    return captured, result


@pytest.mark.parametrize(
    "measurement",
    [
        MeasurementCreateDraftInput.model_validate(
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
        MeasurementCorrectDraftInput.model_validate(
            {
                "intent": "correct",
                "measurement_root_id": str(uuid4()),
                "expected_current_session_id": str(uuid4()),
                "reason": "Исправляю способ и значение замера",
                "session": {
                    "measured_on": date(2026, 10, 10),
                    "measured_time": None,
                    "conditions": None,
                    "comment": None,
                    "values": [
                        {
                            "measurement_kind": "waist_circumference",
                            "original_value": "84.2",
                            "original_unit": "centimeter",
                            "side": None,
                            "method": {
                                "mode": "new",
                                "description": "Standing, tape at navel",
                            },
                            "comment": None,
                        }
                    ],
                },
            }
        ),
    ],
)
def test_measurement_write_tool_matches_cmd11_and_keeps_preview_flow(
    monkeypatch: pytest.MonkeyPatch,
    measurement: Any,
) -> None:
    captured, result = _invoke_measurement_tool(monkeypatch, measurement)
    body = captured["body"]
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert captured["path"] == "/api/v1/commands/measurement-session-drafts"
    assert captured["idempotency_key"] == "synthetic-measurement-draft"
    assert digest == expected
    request = {**body, "request_digest": digest}
    TypeAdapter(MeasurementDraftRequest).validate_python(request)
    ContextService.verify_digest(request, digest)
    assert result == {"data": {"draft_id": "synthetic"}, "meta": {"api_version": "v1"}}
