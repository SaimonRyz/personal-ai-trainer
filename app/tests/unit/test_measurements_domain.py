"""Closed E-G06 unit, side, method and representability rules."""

from __future__ import annotations

from copy import deepcopy

import pytest

from trainer.domain.context import DomainValidationError
from trainer.domain.measurements import (
    calculate_bmi,
    normalize_session,
    normalize_value,
    select_latest_candidate,
)


def value(
    kind: str,
    original: str,
    unit: str,
    side: str | None = None,
    method: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "measurement_kind": kind,
        "original_value": original,
        "original_unit": unit,
        "side": side,
        "method": method,
        "comment": None,
    }


@pytest.mark.parametrize(
    ("kind", "original", "unit", "expected", "normal_unit"),
    [
        ("body_mass", "1", "pound", "0.453592", "kilogram"),
        ("height", "70", "inch", "177.800000", "centimeter"),
        ("height", "1750", "millimeter", "175.000000", "centimeter"),
        ("body_mass", "80", "kilogram", "80.000000", "kilogram"),
    ],
)
def test_exact_server_conversion(
    kind: str, original: str, unit: str, expected: str, normal_unit: str
) -> None:
    result = normalize_value(value(kind, original, unit), "v")
    assert result["normalized_value"] == expected
    assert result["normalized_unit"] == normal_unit
    assert result["original_value"] == original
    assert result["conversion_version"] == "body_units_v1"


@pytest.mark.parametrize(
    "bad",
    [
        value("body_mass", "10", "centimeter"),
        value("height", "10", "pound"),
        value("body_mass", "10", "kilogram", "left"),
        value("upper_arm_circumference", "10", "centimeter", None, {"mode": "unknown"}),
        value("waist_circumference", "10", "centimeter", None, None),
        value("height", "0", "centimeter"),
        value("height", "0.000001", "millimeter"),
        value("height", "999999999999.999999", "inch"),
    ],
)
def test_incompatible_or_unrepresentable_value_fails(bad: dict[str, object]) -> None:
    with pytest.raises(DomainValidationError):
        normalize_value(bad, "v")


def test_duplicate_kind_and_nullable_side_rejected() -> None:
    session = {
        "measured_on": "2026-10-08",
        "measured_time": None,
        "conditions": None,
        "comment": None,
        "values": [value("body_mass", "80", "kilogram"), value("body_mass", "81", "kilogram")],
    }
    with pytest.raises(DomainValidationError, match="duplicate"):
        normalize_session(deepcopy(session))


def test_latest_selection_is_date_first_and_fails_ambiguous_time() -> None:
    known = {"measured_on": "2026-10-08", "measured_time": "10:00:00", "id": "known"}
    later = {"measured_on": "2026-10-09", "measured_time": None, "id": "later"}
    assert select_latest_candidate([known, later]) == later
    same_day = {**later, "measured_time": "11:00:00", "id": "same-day"}
    assert select_latest_candidate([known, later, same_day]) is None
    assert (
        select_latest_candidate([known, {**known, "measured_time": "11:00:00"}])["measured_time"]
        == "11:00:00"
    )
    assert select_latest_candidate([known, {**known, "id": "tie"}]) is None


def test_bmi_decimal_rounding_and_range_without_health_category() -> None:
    assert calculate_bmi("80", "180") == "24.691358"
    assert calculate_bmi("0", "180") is None
    assert calculate_bmi("80", "0.000001") is None
