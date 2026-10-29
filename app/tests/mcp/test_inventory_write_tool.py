from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from trainer_mcp import server
from trainer_mcp.write_tools import InventoryChangeInput

from trainer.application.context import ContextService
from trainer.transport.http.contracts import InventoryChangeDraftRequest


def card() -> dict[str, Any]:
    return {
        "equipment_type": "weight_plate",
        "name": "Synthetic plate",
        "maker": None,
        "brand": None,
        "model": None,
        "quantity": None,
        "quantity_unit_code": None,
        "condition": None,
        "characteristics": {
            "mass_per_item_kg": "2.5",
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": "30",
        },
        "comment": None,
    }


@pytest.mark.parametrize("intent", ["create", "correct"])
def test_inventory_tool_uses_exact_cmd09_contract_and_preserves_unknowns(
    monkeypatch: pytest.MonkeyPatch, intent: str
) -> None:
    captured: dict[str, Any] = {}
    envelope = {"data": {"draft_id": "synthetic"}, "meta": {"api_version": "v1"}}

    def capture(path: str, body: Any, correlation: Any, causation: Any, key: Any) -> Any:
        captured.update(path=path, body=body, key=key)
        return envelope

    monkeypatch.setattr(server, "api_call", capture)
    payload: dict[str, Any] = {"intent": intent, "equipment": card()}
    if intent == "correct":
        payload.update(
            equipment_id=str(uuid4()),
            expected_current_revision_id=str(uuid4()),
            reason="Synthetic correction",
        )
    inventory: Any = TypeAdapter(InventoryChangeInput).validate_python(payload)
    tool = next(
        tool
        for tool in server.mcp._tool_manager.list_tools()
        if tool.name == "create_inventory_change_draft"
    )
    result = tool.fn(
        conversation_id="synthetic",
        message_id="synthetic",
        timezone="UTC",
        inventory=inventory,
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-inventory",
    )
    body = captured["body"]
    TypeAdapter(InventoryChangeDraftRequest).validate_python(body)
    ContextService.verify_digest(body, body["request_digest"])
    assert captured["path"] == "/api/v1/commands/inventory-change-drafts"
    assert captured["key"] == "synthetic-inventory"
    assert body["equipment"]["quantity"] is None
    assert body["equipment"]["condition"] is None
    assert body["equipment"]["characteristics"] == card()["characteristics"]
    assert result is envelope
    if intent == "correct":
        assert body["expected_current_revision_id"] == payload["expected_current_revision_id"]


@pytest.mark.parametrize("field", ["place_id", "seller", "purchases", "events"])
def test_inventory_tool_rejects_excluded_card_fields(field: str) -> None:
    equipment = {**card(), field: "forbidden"}
    with pytest.raises(ValidationError):
        TypeAdapter(InventoryChangeInput).validate_python(
            {"intent": "create", "equipment": equipment}
        )


def test_inventory_correction_requires_exact_revision_and_nonblank_reason() -> None:
    payload = {"intent": "correct", "equipment": card(), "reason": "  "}
    with pytest.raises(ValidationError):
        TypeAdapter(InventoryChangeInput).validate_python(payload)
    payload.update(equipment_id=str(uuid4()), expected_current_revision_id=str(uuid4()))
    with pytest.raises(ValidationError, match="nonblank"):
        TypeAdapter(InventoryChangeInput).validate_python(payload)
