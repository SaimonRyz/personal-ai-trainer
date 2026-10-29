"""Current global inventory facts; no availability or load inference."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

from trainer.domain.context import DomainValidationError, digest_payload

PLANNING_EQUIPMENT_TYPES = (
    "barbell",
    "weight_plate",
    "bench",
    "pull_up_bar",
    "resistance_band",
    "hyperextension_bench",
    "push_up_handles",
)
EQUIPMENT_TYPES = (
    *PLANNING_EQUIPMENT_TYPES,
    "dumbbell",
    "exercise_mat",
    "treadmill",
    "jump_rope",
    "ab_wheel",
    "boxing_reflex_target",
    "hand_gripper",
    "sportswear",
    "training_gloves",
    "wrist_wraps",
)
CHARACTERISTIC_SCALES = {
    "mass_per_item_kg": 6,
    "bar_sleeve_diameter_mm": 3,
    "plate_bore_diameter_mm": 3,
}
CARD_FIELDS = frozenset(
    {
        "equipment_type",
        "name",
        "maker",
        "brand",
        "model",
        "quantity",
        "quantity_unit_code",
        "condition",
        "characteristics",
        "comment",
    }
)
DECIMAL_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$")


def normalize_characteristic(value: object, scale: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not DECIMAL_PATTERN.fullmatch(value):
        raise DomainValidationError("characteristic must be a positive decimal string")
    if "." in value and len(value.split(".")[1]) > scale:
        raise DomainValidationError("characteristic decimal scale exceeds its limit")
    number = Decimal(value)
    if number <= 0:
        raise DomainValidationError("characteristic must be positive")
    return value.rstrip("0").rstrip(".") if "." in value else value


def validate_card(card: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete replacement, preserving explicit unknowns."""
    if set(card) != CARD_FIELDS:
        raise DomainValidationError("equipment card requires exactly the approved fields")
    result = dict(card)
    if card["equipment_type"] not in EQUIPMENT_TYPES:
        raise DomainValidationError("equipment_type is not an approved code")
    for key in ("name", "maker", "brand", "model", "comment"):
        value = card[key]
        if value is None and key != "name":
            continue
        maximum = 2000 if key == "comment" else 200
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise DomainValidationError(f"{key} must be nonblank and within its length limit")
        result[key] = value.strip()
    quantity, unit = card["quantity"], card["quantity_unit_code"]
    if (quantity is None) != (unit is None):
        raise DomainValidationError("quantity and quantity_unit_code must both be null or set")
    if quantity is not None and (
        type(quantity) is not int
        or not 1 <= quantity <= 999999999
        or unit not in ("piece", "pair", "set")
    ):
        raise DomainValidationError("quantity must be a positive integer with piece/pair/set")
    if card["condition"] is not None and card["condition"] not in ("usable", "unusable"):
        raise DomainValidationError("condition must be usable, unusable or null")
    facts = card["characteristics"]
    if not isinstance(facts, Mapping) or set(facts) != set(CHARACTERISTIC_SCALES):
        raise DomainValidationError("characteristics require exactly the three approved fields")
    result["characteristics"] = {
        key: normalize_characteristic(facts[key], scale)
        for key, scale in CHARACTERISTIC_SCALES.items()
    }
    for key, kind in (
        ("bar_sleeve_diameter_mm", "barbell"),
        ("plate_bore_diameter_mm", "weight_plate"),
    ):
        if result["characteristics"][key] is not None and card["equipment_type"] != kind:
            raise DomainValidationError(f"{key} is only applicable to {kind}")
    return result


def project_card(row: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist a stored current revision without changing its legacy content."""
    quantity = row.get("quantity")
    if quantity is not None:
        number = Decimal(str(quantity))
        if not number.is_finite() or number != number.to_integral_value():
            raise DomainValidationError("stored inventory quantity is not integral")
        quantity = int(number)
    facts = row.get("characteristics")
    if not isinstance(facts, Mapping):
        raise DomainValidationError("stored characteristics are not an object")
    return validate_card(
        {
            **{key: row.get(key) for key in CARD_FIELDS - {"quantity", "characteristics"}},
            "quantity": quantity,
            "characteristics": {key: facts.get(key) for key in CHARACTERISTIC_SCALES},
        }
    )


def inventory_snapshot(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Canonical full membership; only explicitly usable planning types match."""
    snapshot = sorted(items, key=lambda item: str(UUID(str(item["ref"]["object_id"]))))
    identifiers = [item["ref"]["object_id"] for item in snapshot]
    if len(set(identifiers)) != len(identifiers):
        raise DomainValidationError("inventory snapshot contains duplicate items")
    effective = sorted(
        {
            item["equipment"]["equipment_type"]
            for item in snapshot
            if item["equipment"]["condition"] == "usable"
            and item["equipment"]["equipment_type"] in PLANNING_EQUIPMENT_TYPES
        }
    )
    return snapshot, digest_payload(snapshot), effective
