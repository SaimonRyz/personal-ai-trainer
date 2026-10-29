"""Deterministic, fail-closed calculations for the E-G03 progress slice."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

E1RM_METHOD_ID = "e1rm_brzycki"
E1RM_METHOD_VERSION = "1"
VOLUME_METHOD_ID = "external_working_set_volume"
VOLUME_METHOD_VERSION = "1"
RESULT_QUANTUM = Decimal("0.000001")
BRZYCKI_CONSTANT = Decimal("1.0278")
BRZYCKI_REP_FACTOR = Decimal("0.0278")


def _decimal(value: Decimal | int | str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("invalid_decimal") from error
    if not result.is_finite():
        raise ValueError("non_finite_decimal")
    return result


def quantize_result(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("non_finite_result")
    return value.quantize(RESULT_QUANTUM, rounding=ROUND_HALF_UP)


def classify_calculated_best(
    candidate: Decimal, current: Decimal | None
) -> Literal["new_current", "strict_improvement", "equal_keeps_first", "not_better"]:
    """Keep the first achievement date for an equal estimate."""
    if current is None:
        return "new_current"
    if candidate > current:
        return "strict_improvement"
    if candidate == current:
        return "equal_keeps_first"
    return "not_better"


@dataclass(frozen=True)
class E1RMAttestation:
    """Per-calculation statements tied to one exact source set; not technique facts."""

    source_actual_set_id: str
    grip_condition_code: str
    bench_pause_condition: Literal["paused", "touch_and_go"]
    standard_confirmed: bool
    spotter_released_before_rep: bool
    no_assistance_during_rep: bool


@dataclass(frozen=True)
class E1RMInput:
    exercise_code: str
    actual_set_id: str
    source_revision_id: str
    set_type: str
    status: str
    full_reps: int | None
    partial_reps: int | None
    failure_reached: bool | None
    reported_rir: Decimal | int | None
    load_kg: Decimal | None
    load_kind: str
    attestation: E1RMAttestation | None


def calculate_brzycki_e1rm(value: E1RMInput) -> Decimal:
    """Calculate only eligible standard barbell bench sets, with explicit attestation."""
    if value.exercise_code != "barbell_bench_press":
        raise ValueError("unsupported_exercise")
    if value.set_type != "working" or value.status != "performed":
        raise ValueError("ineligible_set_state")
    if value.full_reps is None or not 2 <= value.full_reps <= 10:
        raise ValueError("ineligible_repetitions")
    if value.partial_reps != 0:
        raise ValueError("partial_repetitions_unknown_or_present")
    if value.failure_reached is not True:
        raise ValueError("explicit_failure_required")
    if value.load_kind != "external" or value.load_kg is None:
        raise ValueError("known_external_load_required")
    load = _decimal(value.load_kg)
    if load <= 0:
        raise ValueError("positive_load_required")
    attestation = value.attestation
    if attestation is None or attestation.source_actual_set_id != value.actual_set_id:
        raise ValueError("source_bound_attestation_required")
    if not attestation.grip_condition_code.strip():
        raise ValueError("grip_condition_required")
    if not attestation.standard_confirmed:
        raise ValueError("standard_confirmation_required")
    if not attestation.spotter_released_before_rep or not attestation.no_assistance_during_rep:
        raise ValueError("spotter_or_assistance_condition_not_met")
    if value.reported_rir is not None and value.reported_rir != 0:
        # RIR alone is not evidence of failure; explicit user failure report is still required,
        # and a contradictory RIR requires clarification rather than overriding the fact.
        raise ValueError("contradictory_effort_report")
    denominator = BRZYCKI_CONSTANT - BRZYCKI_REP_FACTOR * Decimal(value.full_reps)
    if denominator <= 0:
        raise ValueError("invalid_formula_denominator")
    return quantize_result(load / denominator)


@dataclass(frozen=True)
class VolumeSet:
    actual_set_id: str
    set_type: str
    status: str
    mass_kg: Decimal | None
    full_reps: int | None
    load_kind: str
    simultaneous_count: int = 1
    partial_reps: int | None = 0
    performed_sides: tuple[str, ...] = ()
    expected_sides: tuple[str, ...] = ()


@dataclass(frozen=True)
class VolumeResult:
    status: Literal["complete", "partial", "insufficient"]
    value_kg_reps: Decimal | None
    included_set_ids: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]


def calculate_external_working_volume(sets: tuple[VolumeSet, ...]) -> VolumeResult:
    """Compute a transient summary. Callers must not persist this result."""
    total = Decimal(0)
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    partial = False
    for item in sets:
        if item.set_type == "warmup":
            excluded.append((item.actual_set_id, "warmup"))
            continue
        if item.set_type != "working":
            excluded.append((item.actual_set_id, "not_working"))
            continue
        if item.status == "skipped":
            excluded.append((item.actual_set_id, "skipped"))
            partial = True
            continue
        if item.load_kind != "external" or item.mass_kg is None:
            reason = (
                item.load_kind
                if item.load_kind in ("non_external_load", "ambiguous_load", "unknown_item_count")
                else "unknown_load"
            )
            excluded.append((item.actual_set_id, reason))
            partial = True
            continue
        if item.full_reps is None or item.full_reps < 0:
            excluded.append((item.actual_set_id, "unknown_repetitions"))
            partial = True
            continue
        mass = _decimal(item.mass_kg)
        if mass < 0 or item.simultaneous_count < 1:
            excluded.append((item.actual_set_id, "invalid_load_shape"))
            partial = True
            continue
        if item.expected_sides and set(item.performed_sides) != set(item.expected_sides):
            excluded.append((item.actual_set_id, "missing_or_unknown_side"))
            partial = True
            if not item.performed_sides:
                continue
        total += mass * item.simultaneous_count * item.full_reps
        included.append(item.actual_set_id)
        if item.partial_reps is None or item.partial_reps > 0:
            partial = True
            excluded.append((item.actual_set_id, "partial_repetitions"))
        if item.status == "interrupted":
            partial = True
            excluded.append((item.actual_set_id, "interrupted_set"))
        elif item.status != "performed":
            excluded.append((item.actual_set_id, "unsupported_status"))
            partial = True
            included.pop()
            total -= mass * item.simultaneous_count * item.full_reps
    if not included:
        return VolumeResult("insufficient", None, (), tuple(excluded))
    return VolumeResult(
        "partial" if partial else "complete", total, tuple(included), tuple(excluded)
    )
