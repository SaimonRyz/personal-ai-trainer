from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from trainer.domain.context import DomainValidationError
from trainer.domain.inventory import inventory_snapshot, project_card, validate_card


def card(kind: str = "barbell", condition: str | None = "usable") -> dict[str, Any]:
    return {
        "equipment_type": kind,
        "name": "Synthetic equipment",
        "maker": None,
        "brand": None,
        "model": None,
        "quantity": None,
        "quantity_unit_code": None,
        "condition": condition,
        "comment": None,
        "characteristics": {
            "mass_per_item_kg": None,
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": None,
        },
    }


@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True, 1000000000])
def test_inventory_quantity_is_integral_and_not_a_load_count(value: object) -> None:
    value_card = card()
    value_card.update(quantity=value, quantity_unit_code="piece")
    with pytest.raises(DomainValidationError, match="quantity"):
        validate_card(value_card)


@pytest.mark.parametrize("unit", ["piece", "pair", "set"])
def test_quantity_units_are_preserved_without_conversion(unit: str) -> None:
    value = card()
    value.update(quantity=2, quantity_unit_code=unit)
    assert validate_card(value)["quantity"] == 2
    assert validate_card(value)["quantity_unit_code"] == unit


def test_full_replacement_requires_explicit_nulls_and_rejects_excluded_fields() -> None:
    value = card()
    del value["maker"]
    with pytest.raises(DomainValidationError):
        validate_card(value)
    for forbidden in ("seller", "price", "place_id", "availability", "rating"):
        with pytest.raises(DomainValidationError):
            validate_card({**card(), forbidden: None})
    value = card()
    value["quantity"] = 1
    with pytest.raises(DomainValidationError):
        validate_card(value)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "1e2", "01", 2, "1.0000001"])
def test_mass_requires_positive_decimal_string(value: object) -> None:
    value_card = card()
    value_card["characteristics"]["mass_per_item_kg"] = value
    with pytest.raises(DomainValidationError):
        validate_card(value_card)


def test_dimensions_are_applicable_facts_and_not_compatibility_inference() -> None:
    bar, plate = card(), card("weight_plate")
    bar["characteristics"]["bar_sleeve_diameter_mm"] = "30.000"
    plate["characteristics"]["plate_bore_diameter_mm"] = "30.001"
    assert validate_card(bar)["characteristics"]["bar_sleeve_diameter_mm"] == "30"
    assert validate_card(plate)["characteristics"]["plate_bore_diameter_mm"] == "30.001"
    bar["characteristics"]["plate_bore_diameter_mm"] = "30"
    with pytest.raises(DomainValidationError, match="only applicable"):
        validate_card(bar)


def test_decimal_normalization_preserves_more_than_context_precision() -> None:
    value = card()
    exact = "12345678901234567890123456789.123450"
    value["characteristics"]["mass_per_item_kg"] = exact
    assert validate_card(value)["characteristics"]["mass_per_item_kg"] == exact[:-1]
    value["characteristics"]["mass_per_item_kg"] = exact.split(".")[0]
    assert validate_card(value)["characteristics"]["mass_per_item_kg"] == exact.split(".")[0]


def test_legacy_projection_hides_arbitrary_keys_without_rewriting() -> None:
    stored = card()
    stored.update(quantity=Decimal("2.000"), quantity_unit_code="pair")
    stored["characteristics"] = {"seller_mass": "99", "raw": {"unsafe": True}}
    original = deepcopy(stored)
    projected = project_card(stored)
    assert projected["quantity"] == 2
    assert projected["characteristics"] == card()["characteristics"]
    assert stored == original
    stored["quantity"] = Decimal("1.500")
    with pytest.raises(DomainValidationError, match="not integral"):
        project_card(stored)


def test_snapshot_is_global_sorted_and_includes_inventory_only_and_unknown() -> None:
    kinds = [
        ("barbell", "usable"),
        ("bench", None),
        ("dumbbell", "usable"),
        ("weight_plate", "unusable"),
    ]
    items = [
        {
            "ref": {
                "object_type": "equipment_item",
                "object_id": str(UUID(int=i)),
                "revision_id": str(UUID(int=100 + i)),
            },
            "equipment": card(kind, state),
        }
        for i, (kind, state) in enumerate(kinds, start=1)
    ]
    snapshot, digest, effective = inventory_snapshot(list(reversed(items)))
    assert snapshot == items
    assert len(snapshot) == 4 and effective == ["barbell"]
    assert inventory_snapshot(items)[1] == digest
    corrected = deepcopy(items)
    corrected[2]["equipment"]["comment"] = "Synthetic correction of inventory-only item"
    assert inventory_snapshot(corrected)[1] != digest
    assert inventory_snapshot(items[:3])[1] != digest
    with pytest.raises(DomainValidationError, match="duplicate"):
        inventory_snapshot([items[0], items[0]])
