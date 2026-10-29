from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from trainer_mcp import server

from trainer.transport.http.contracts import ProfileContextRequest, ProfileContextResponse


def test_profile_context_preserves_current_equipment_cards_and_exact_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    equipment_id = uuid4()
    equipment_revision_id = uuid4()
    response_data = {
        "profile_ref": None,
        "profile": None,
        "goals": [],
        "places": [],
        "equipment": [
            {
                "ref": {
                    "object_type": "equipment_item",
                    "object_id": equipment_id,
                    "revision_id": equipment_revision_id,
                },
                "equipment": {
                    "equipment_type": "barbell",
                    "name": "Synthetic barbell",
                    "maker": None,
                    "brand": None,
                    "model": None,
                    "quantity": 1,
                    "quantity_unit_code": "piece",
                    "condition": "usable",
                    "characteristics": {
                        "mass_per_item_kg": "7",
                        "bar_sleeve_diameter_mm": "30",
                        "plate_bore_diameter_mm": None,
                    },
                    "comment": None,
                },
            }
        ],
        "familiar_exercises": [],
        "effective_at": datetime(2026, 10, 11, tzinfo=UTC),
        "gaps": [],
    }
    envelope: dict[str, Any] = {
        "data": {"response": response_data},
        "meta": {"api_version": "v1", "correlation_id": str(uuid4())},
    }
    captured: dict[str, Any] = {}

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: Any,
        causation_id: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captured.update(path=path, body=body)
        return envelope

    monkeypatch.setattr(server, "api_call", fake_api_call)
    tool = next(
        item for item in server.mcp._tool_manager.list_tools() if item.name == "get_profile_context"
    )
    result = tool.fn(
        conversation_id="synthetic-inventory-context",
        message_id="synthetic-equipment-read",
        timezone="Asia/Novokuznetsk",
        purpose="plan preparation",
        correlation_id=uuid4(),
        causation_id=uuid4(),
    )

    assert captured["path"] == "/api/v1/queries/profile-context"
    ProfileContextRequest.model_validate(captured["body"])
    assert captured["body"]["purpose"] == "plan preparation"
    assert result is envelope
    context = ProfileContextResponse.model_validate(envelope["data"]["response"])
    assert len(context.equipment) == 1
    item = context.equipment[0]
    assert item.ref.object_id == equipment_id
    assert item.ref.revision_id == equipment_revision_id
    assert item.equipment.equipment_type == "barbell"
    assert item.equipment.characteristics.mass_per_item_kg == "7"
    assert not hasattr(item, "place_id")
