from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from trainer_mcp import server
from trainer_mcp.contracts import HistoricalRecordCreateInput

from trainer.transport.http.contracts import HistoricalRecordCreateDraftRequest


def _historical_record() -> dict[str, Any]:
    return {
        "exercise_id": uuid4(),
        "exercise_revision_id": uuid4(),
        "metric_kind": "record_external_load",
        "value": Decimal("100"),
        "unit_code": "kilogram",
        "achieved_on": date(2025, 1, 1),
        "condition": {
            "side": "not_applicable",
            "loads": [
                {
                    "load_kind": "external",
                    "direction": None,
                    "equipment_type": "barbell",
                    "equipment_id": None,
                    "item_count": Decimal("1"),
                    "mass_kg": Decimal("100"),
                    "mass_meaning": "total_external_mass",
                }
            ],
            "fixed_reps": 1,
            "fixed_distance_m": None,
            "unknown_fields": [],
        },
        "body_mass_kg": None,
        "historical_import": True,
        "pre_system_assertion": True,
        "technique_self_confirmation": True,
        "reason": "Подтверждённая историческая запись",
        "comment": None,
    }


def test_historical_record_tool_builds_cmd24_draft_and_preserves_envelope(
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
        if item.name == "create_historical_record_draft"
    )
    result = tool.fn(
        conversation_id="synthetic-conversation",
        message_id="synthetic-message",
        timezone="Asia/Novokuznetsk",
        historical_record=HistoricalRecordCreateInput(
            intent="create", historical=_historical_record()
        ),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-historical-record",
    )

    body = captured["body"]
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert captured["path"] == "/api/v1/commands/historical-record-drafts"
    assert captured["idempotency_key"] == "synthetic-historical-record"
    assert digest == expected
    request = {**body, "request_digest": digest}
    HistoricalRecordCreateDraftRequest.model_validate(request)
    assert result is envelope


def test_historical_record_tool_rejects_non_self_attested_or_mismatched_records() -> None:
    invalid_attestation = _historical_record()
    invalid_attestation["technique_self_confirmation"] = False
    with pytest.raises(ValidationError):
        HistoricalRecordCreateInput(intent="create", historical=invalid_attestation)

    mismatched_load = _historical_record()
    mismatched_load["condition"]["loads"][0]["mass_kg"] = Decimal("99")
    with pytest.raises(ValidationError, match="must match the record value"):
        HistoricalRecordCreateInput(intent="create", historical=mismatched_load)
