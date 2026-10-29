"""Closed E-G06 measurement rules; no health interpretation."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from trainer.domain.context import DomainValidationError

MASS = "body_mass"
HEIGHT = "height"
AXIAL = frozenset({"waist_circumference", "chest_circumference", "neck_circumference"})
PAIRED = frozenset(
    {
        "upper_arm_circumference",
        "forearm_circumference",
        "thigh_circumference",
        "calf_circumference",
    }
)
KINDS = frozenset({MASS, HEIGHT, *AXIAL, *PAIRED})
LENGTH_UNITS = frozenset({"centimeter", "millimeter", "inch"})
MASS_UNITS = frozenset({"kilogram", "pound"})
SCALE = Decimal("0.000001")
MAX_VALUE = Decimal("999999999999.999999")
DECIMAL_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$")
FACTORS = {
    "kilogram": Decimal("1"),
    "pound": Decimal("0.45359237"),
    "centimeter": Decimal("1"),
    "millimeter": Decimal("0.1"),
    "inch": Decimal("2.54"),
}


def positive_decimal(value: object, path: str) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_PATTERN.fullmatch(value):
        raise DomainValidationError(
            f"{path}: positive decimal string with at most six places required"
        )
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise DomainValidationError(f"{path}: invalid decimal") from error
    if not 0 < number <= MAX_VALUE:
        raise DomainValidationError(f"{path}: value outside numeric(18,6)")
    return number


def normalize_value(value: dict[str, Any], path: str) -> dict[str, Any]:
    kind, unit, side = value["measurement_kind"], value["original_unit"], value["side"]
    if kind not in KINDS:
        raise DomainValidationError(f"{path}.measurement_kind: unsupported type")
    if unit not in (MASS_UNITS if kind == MASS else LENGTH_UNITS):
        raise DomainValidationError(f"{path}.original_unit: incompatible unit")
    if (kind in PAIRED and side not in ("left", "right")) or (
        kind not in PAIRED and side is not None
    ):
        raise DomainValidationError(f"{path}.side: incompatible side")
    method = value["method"]
    if kind in (MASS, HEIGHT):
        if method is not None:
            raise DomainValidationError(f"{path}.method: not applicable")
    elif not isinstance(method, dict) or method.get("mode") not in ("unknown", "new", "reuse"):
        raise DomainValidationError(f"{path}.method: explicit mode required")
    number = positive_decimal(value["original_value"], f"{path}.original_value")
    normalized = (number * FACTORS[unit]).quantize(SCALE, rounding=ROUND_HALF_UP)
    if not 0 < normalized <= MAX_VALUE:
        raise DomainValidationError(f"{path}.original_value: conversion outside numeric(18,6)")
    return {
        **value,
        "normalized_value": str(normalized),
        "normalized_unit": "kilogram" if kind == MASS else "centimeter",
        "conversion_version": "body_units_v1",
    }


def normalize_session(session: dict[str, Any]) -> dict[str, Any]:
    values = session["values"]
    if not 1 <= len(values) <= 100:
        raise DomainValidationError("session.values: 1..100 required")
    seen: set[tuple[str, str | None]] = set()
    normalized = []
    for index, value in enumerate(values):
        item = normalize_value(value, f"session.values[{index}]")
        key = item["measurement_kind"], item["side"]
        if key in seen:
            raise DomainValidationError(f"session.values[{index}]: duplicate kind and side")
        seen.add(key)
        normalized.append(item)
    return {**session, "values": normalized}


def select_latest_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the unique latest fact or None if absent/chronologically ambiguous."""
    if not candidates:
        return None
    latest_day = max(row["measured_on"] for row in candidates)
    same_day = [row for row in candidates if row["measured_on"] == latest_day]
    if len(same_day) == 1:
        return same_day[0]
    if any(row["measured_time"] is None for row in same_day):
        return None
    latest_time = max(row["measured_time"] for row in same_day)
    latest = [row for row in same_day if row["measured_time"] == latest_time]
    return latest[0] if len(latest) == 1 else None


def calculate_bmi(mass_kg: object, height_cm: object) -> str | None:
    """Decimal BMI, only an on-demand calculation without health categories."""
    try:
        mass, height = Decimal(str(mass_kg)), Decimal(str(height_cm))
        if mass <= 0 or height <= 0:
            return None
        value = (mass / (height / 100) ** 2).quantize(SCALE, rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError, ZeroDivisionError):
        return None
    return str(value) if 0 < value <= MAX_VALUE else None
