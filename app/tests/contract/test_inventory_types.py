from __future__ import annotations

from typing import Any, get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.domain.inventory import EQUIPMENT_TYPES, PLANNING_EQUIPMENT_TYPES
from trainer.transport.http.inventory_types import (
    ContextEquipmentInput,
    EquipmentCardInput,
    EquipmentTypeCode,
    PlanningEquipmentTypeCode,
)


def equipment() -> dict[str, Any]:
    return {
        "equipment_type": "barbell",
        "name": " Synthetic bar ",
        "maker": None,
        "brand": None,
        "model": None,
        "quantity": None,
        "quantity_unit_code": None,
        "condition": None,
        "characteristics": {
            "mass_per_item_kg": "7.000000",
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": None,
        },
        "comment": None,
    }


def test_closed_codes_match_owner_vocabulary() -> None:
    assert get_args(EquipmentTypeCode) == EQUIPMENT_TYPES
    assert get_args(PlanningEquipmentTypeCode) == PLANNING_EQUIPMENT_TYPES


def test_onboarding_is_flat_full_card_with_stable_identity() -> None:
    identifier = uuid4()
    value = ContextEquipmentInput.model_validate({**equipment(), "equipment_id": identifier})
    wire = value.model_dump(mode="json")
    assert wire["equipment_id"] == str(identifier)
    assert wire["name"] == "Synthetic bar"
    assert wire["characteristics"]["mass_per_item_kg"] == "7"
    assert wire["quantity"] is None and wire["condition"] is None
    assert "equipment_id" not in EquipmentCardInput.model_fields


@pytest.mark.parametrize(
    "key",
    [
        "maker",
        "brand",
        "model",
        "quantity",
        "quantity_unit_code",
        "condition",
        "characteristics",
        "comment",
    ],
)
def test_nullable_fields_are_required_by_presence(key: str) -> None:
    value = equipment()
    del value[key]
    with pytest.raises(ValidationError):
        EquipmentCardInput.model_validate(value)


@pytest.mark.parametrize("quantity", [True, 1.0, "1", 0, -1])
def test_wire_quantity_has_no_numeric_coercion(quantity: object) -> None:
    with pytest.raises(ValidationError):
        EquipmentCardInput.model_validate(
            {**equipment(), "quantity": quantity, "quantity_unit_code": "piece"}
        )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"mass_per_item_kg": None},
        {
            "mass_per_item_kg": None,
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": None,
            "legacy": "1",
        },
    ],
)
def test_characteristic_map_is_closed_and_full(value: object) -> None:
    with pytest.raises(ValidationError):
        EquipmentCardInput.model_validate({**equipment(), "characteristics": value})
