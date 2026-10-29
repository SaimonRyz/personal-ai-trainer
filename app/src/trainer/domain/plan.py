from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from trainer.domain.context import DomainValidationError


def validate_plan_draft(payload: dict[str, Any]) -> None:
    """Validate a canonical plan payload before it can become a technical draft."""
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        raise DomainValidationError("plan is required")
    if not str(plan.get("plan_type", "")).strip() or not str(plan.get("name", "")).strip():
        raise DomainValidationError("plan_type and name are required")
    if plan["plan_type"] != "training":
        raise DomainValidationError("unsupported plan_type")
    _validate_source(plan)
    _validate_overlay(plan.get("temporary_conditions"))
    blocks = plan.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise DomainValidationError("plan requires at least one block")
    _validate_positions(blocks, "blocks")
    for block in blocks:
        exercises = block.get("exercises")
        if not isinstance(exercises, list) or not exercises:
            raise DomainValidationError("each block requires at least one exercise")
        _validate_positions(exercises, "exercises")
        for exercise in exercises:
            _uuid(exercise.get("exercise_id"), "exercise_id")
            sets = exercise.get("sets")
            if not isinstance(sets, list) or not sets:
                raise DomainValidationError("each exercise requires at least one set")
            _validate_positions(sets, "sets")
            for planned_set in sets:
                _validate_set(planned_set)
    recommendations = payload.get("recommendations")
    if not isinstance(recommendations, list):
        raise DomainValidationError("recommendations must be a list")
    for recommendation in recommendations:
        _validate_recommendation(recommendation)


def _validate_source(plan: dict[str, Any]) -> None:
    program_id = plan.get("program_id")
    version_id = plan.get("program_version_id")
    template_id = plan.get("day_template_id")
    if (program_id is None) != (version_id is None):
        raise DomainValidationError("program_id and program_version_id must be supplied together")
    if template_id is not None and version_id is None:
        raise DomainValidationError("day_template_id requires a program version")
    for field, value in (
        ("program_id", program_id),
        ("program_version_id", version_id),
        ("day_template_id", template_id),
    ):
        if value is not None:
            _uuid(value, field)


def _validate_overlay(overlay: Any) -> None:
    if not isinstance(overlay, dict):
        raise DomainValidationError("temporary_conditions is required")
    additions = overlay.get("equipment_additions")
    removals = overlay.get("equipment_removals")
    if not isinstance(additions, list) or not isinstance(removals, list):
        raise DomainValidationError("equipment additions and removals must be lists")
    if any(not isinstance(code, str) or not code.strip() for code in additions + removals):
        raise DomainValidationError("equipment overlay codes must be non-empty strings")
    if len(additions) != len(set(additions)) or len(removals) != len(set(removals)):
        raise DomainValidationError("equipment overlay entries must be unique")
    if set(additions) & set(removals):
        raise DomainValidationError("equipment additions and removals cannot overlap")
    place = overlay.get("place")
    if place is not None:
        if place.get("kind") == "saved":
            _validate_ref(place.get("place_ref"), "training_place", "place.place_ref")
        elif place.get("kind") == "ad_hoc":
            if (
                not str(place.get("name", "")).strip()
                or not str(place.get("place_type", "")).strip()
            ):
                raise DomainValidationError("ad hoc place requires name and place_type")
            if "place_id" in place or "object_id" in place:
                raise DomainValidationError("ad hoc place cannot create an identity")
        else:
            raise DomainValidationError("temporary place overlay kind is invalid")


def _validate_positions(items: list[dict[str, Any]], label: str) -> None:
    try:
        positions = [int(item["position"]) for item in items]
    except (KeyError, TypeError, ValueError) as error:
        raise DomainValidationError(f"{label} require integer positions") from error
    if any(position < 1 for position in positions) or len(positions) != len(set(positions)):
        raise DomainValidationError(f"{label} positions must be unique positive integers")


def _validate_set(planned_set: dict[str, Any]) -> None:
    if planned_set.get("round_no") is not None and int(planned_set["round_no"]) < 1:
        raise DomainValidationError("round_no must be positive")
    if int(planned_set.get("logical_set_no", 0)) < 1:
        raise DomainValidationError("logical_set_no must be positive")
    side = planned_set.get("side")
    if side is not None and side not in {"left", "right", "both", "not_applicable"}:
        raise DomainValidationError("side is invalid")
    for prefix in ("target_rpe", "target_rir"):
        lower, upper = planned_set.get(f"{prefix}_min"), planned_set.get(f"{prefix}_max")
        _validate_range(lower, upper, prefix, bounded_0_10=True)
    for field in ("pause_seconds", "rest_after_seconds"):
        value = planned_set.get(field)
        if value is not None and _decimal(value, field) < 0:
            raise DomainValidationError(f"{field} cannot be negative")

    metrics = planned_set.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise DomainValidationError("each set requires at least one metric")
    metric_kinds: set[str] = set()
    for metric in metrics:
        kind = str(metric.get("metric_kind", ""))
        if not kind or kind in metric_kinds:
            raise DomainValidationError(
                "metric kinds must be non-empty and unique within a planned set"
            )
        metric_kinds.add(kind)
        exact = metric.get("exact_value")
        lower, upper = metric.get("min_value"), metric.get("max_value")
        if exact is not None:
            if lower is not None or upper is not None:
                raise DomainValidationError("metric exact_value cannot be combined with a range")
            if _decimal(exact, "metric exact_value") < 0:
                raise DomainValidationError("metric exact_value cannot be negative")
        elif lower is None or upper is None:
            raise DomainValidationError("metric requires either exact_value or a complete range")
        else:
            _validate_range(lower, upper, "metric")
        if not str(metric.get("unit_code", "")).strip():
            raise DomainValidationError("metric unit_code is required")

    loads = planned_set.get("loads")
    if not isinstance(loads, list):
        raise DomainValidationError("loads must be a list")
    if loads:
        _validate_positions(loads, "loads")
    for component in loads:
        mass = component.get("mass")
        if mass is not None and (
            not component.get("unit_code") or not component.get("mass_meaning")
        ):
            raise DomainValidationError("numeric load mass requires unit_code and mass_meaning")
        if mass is not None and component.get("raw_ambiguous_text") is not None:
            raise DomainValidationError("ambiguous load text cannot accompany numeric mass")
        if mass is not None and _decimal(mass, "load mass") < 0:
            raise DomainValidationError("load mass cannot be negative")
        item_count = component.get("item_count")
        if item_count is not None and _decimal(item_count, "load item_count") <= 0:
            raise DomainValidationError("load item_count must be positive")
        if component.get("equipment_id") is not None:
            _uuid(component["equipment_id"], "equipment_id")


def _validate_range(lower: Any, upper: Any, label: str, *, bounded_0_10: bool = False) -> None:
    minimum = _decimal(lower, label) if lower is not None else None
    maximum = _decimal(upper, label) if upper is not None else None
    if minimum is not None and minimum < 0 or maximum is not None and maximum < 0:
        raise DomainValidationError(f"{label} bounds cannot be negative")
    if bounded_0_10 and any(
        value is not None and value > Decimal("10") for value in (minimum, maximum)
    ):
        raise DomainValidationError(f"{label} bounds must be between 0 and 10")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise DomainValidationError(f"{label} minimum cannot exceed maximum")


def _decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise DomainValidationError(f"{label} must be numeric") from error
    if not number.is_finite():
        raise DomainValidationError(f"{label} must be finite")
    return number


def _uuid(value: Any, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise DomainValidationError(f"{label} must be a UUID") from error


def _validate_ref(ref: Any, object_type: str, label: str) -> None:
    if not isinstance(ref, dict) or ref.get("object_type") != object_type:
        raise DomainValidationError(f"{label} must reference a {object_type}")
    _uuid(ref.get("object_id"), f"{label}.object_id")
    _uuid(ref.get("revision_id"), f"{label}.revision_id")


def _validate_recommendation(recommendation: dict[str, Any]) -> None:
    for field in ("recommendation_kind", "decision_text", "revisit_condition", "short_explanation"):
        if not str(recommendation.get(field, "")).strip():
            raise DomainValidationError(f"recommendation {field} is required")
    if recommendation.get("confidence") not in {"high", "moderate", "low", "insufficient"}:
        raise DomainValidationError("recommendation confidence is invalid")
    for link in recommendation.get("fact_links", []):
        ref = link.get("ref", {})
        if not ref.get("revision_id"):
            raise DomainValidationError("recommendation fact links require exact revisions")
        _uuid(ref.get("object_id"), "recommendation fact object_id")
        _uuid(ref.get("revision_id"), "recommendation fact revision_id")
        if not link.get("snapshot_digest") or len(link["snapshot_digest"]) != 64:
            raise DomainValidationError("recommendation fact link requires a snapshot digest")
    for link in recommendation.get("calculation_links", []):
        _uuid(link.get("derived_value_id"), "recommendation derived_value_id")
    for link in recommendation.get("conclusion_links", []):
        _uuid(link.get("conclusion_version_id"), "recommendation conclusion_version_id")
