"""Deterministic E-G02 comparison primitives for confirmed workout facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

RECORD_RULE_VERSION = "record_v1"
RECORD_METRICS = {
    "record_full_reps": ("repetition", "max"),
    "record_external_load": ("kilogram", "max"),
    "record_hold_duration": ("second", "max"),
    "record_cardio_time": ("second", "min"),
}


@dataclass(frozen=True)
class RecordFact:
    metric_kind: str
    value: Decimal
    unit_code: str
    condition: dict[str, Any]


def decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("record numeric value is invalid") from error
    if not number.is_finite():
        raise ValueError("record numeric value must be finite")
    normalized = format(number.normalize(), "f")
    return "0" if normalized in ("-0", "") else normalized


def canonical_condition_key(condition: dict[str, Any]) -> str:
    """Serialize the closed ASCII-key condition with canonical decimal number forms."""

    def encode(value: Any, field: str | None = None) -> str:
        if isinstance(value, dict):
            return (
                "{"
                + ",".join(
                    json.dumps(key, ensure_ascii=False) + ":" + encode(value[key], key)
                    for key in sorted(value)
                )
                + "}"
            )
        if isinstance(value, list):
            return "[" + ",".join(encode(item) for item in value) + "]"
        if field in {"mass_kg", "item_count", "fixed_distance_m"} and isinstance(value, str):
            return decimal_text(value) or "null"
        if isinstance(value, Decimal):
            return decimal_text(value) or "null"
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    return encode(condition)


def strictly_improves(metric_kind: str, candidate: Decimal, current: Decimal | None) -> bool:
    if metric_kind not in RECORD_METRICS:
        return False
    if current is None:
        return True
    direction = RECORD_METRICS[metric_kind][1]
    return candidate > current if direction == "max" else candidate < current


def validate_record_fact(fact: RecordFact) -> None:
    expected = RECORD_METRICS.get(fact.metric_kind)
    if expected is None or fact.unit_code != expected[0]:
        raise ValueError("unknown_metric_or_unit")
    if not fact.value.is_finite() or fact.value <= 0:
        raise ValueError("record value must be finite and positive")
    scope = fact.condition.get("scope")
    unknown = fact.condition.get("unknown_fields")
    if scope == "comparable" and unknown:
        raise ValueError("comparable record condition cannot have unknown fields")
    if scope == "historical_unknown" and not unknown:
        raise ValueError("historical_unknown requires unknown fields")


def choose_training_winner(
    candidates: list[dict[str, Any]], metric_kind: str
) -> dict[str, Any] | None:
    """Select a single best item; input order is the stable block/exercise/set order."""
    if metric_kind not in RECORD_METRICS:
        return None
    winner: dict[str, Any] | None = None
    for candidate in candidates:
        if winner is None or strictly_improves(
            metric_kind, Decimal(str(candidate["value"])), Decimal(str(winner["value"]))
        ):
            winner = candidate
    return winner
