from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from trainer.domain.context import DomainValidationError


def validate_program_draft(payload: dict[str, Any]) -> None:
    intent = payload["intent"]
    if intent == "create":
        if (
            payload.get("program") is None
            or payload.get("program_id") is not None
            or payload.get("expected_current_version_id") is not None
        ):
            raise DomainValidationError("create requires program and forbids existing identifiers")
    elif intent == "new_version":
        if (
            payload.get("program") is not None
            or not payload.get("program_id")
            or not payload.get("expected_current_version_id")
        ):
            raise DomainValidationError(
                "new_version requires program_id and expected_current_version_id"
            )
    else:
        raise DomainValidationError("unsupported program intent")
    program = payload.get("program")
    version = payload["version"]
    if date.fromisoformat(version["period_start"]) > date.fromisoformat(version["period_end"]):
        raise DomainValidationError("version period_start must not exceed period_end")
    if program:
        starts, ends = (
            date.fromisoformat(program["starts_on"]),
            date.fromisoformat(program["ends_on"]),
        )
        if starts > ends:
            raise DomainValidationError("program starts_on must not exceed ends_on")
        if not (
            starts
            <= date.fromisoformat(version["period_start"])
            <= date.fromisoformat(version["period_end"])
            <= ends
        ):
            raise DomainValidationError("version period must be inside program period")
    _unique_positions(version["templates"], "templates")
    for day in version["templates"]:
        _unique_positions(day["blocks"], "blocks")
        for block in day["blocks"]:
            exercises = block["exercises"]
            _unique_positions(exercises, "exercises")
            kind = block["block_type_code"]
            if kind == "straight":
                if block.get("declared_rounds") is not None or any(
                    e.get("circuit_position") is not None for e in exercises
                ):
                    raise DomainValidationError(
                        "straight block forbids rounds and circuit positions"
                    )
            else:
                rounds = block.get("declared_rounds")
                if rounds is None or len(exercises) < 2:
                    raise DomainValidationError(
                        "superset/circuit requires rounds and at least two exercises"
                    )
                positions = sorted(e.get("circuit_position") or 0 for e in exercises)
                if positions != list(range(1, len(exercises) + 1)):
                    raise DomainValidationError("circuit positions must form 1..N")
            for exercise in exercises:
                _unique_positions(exercise["sets"], "sets")
                for item in exercise["sets"]:
                    _validate_pause_seconds(item.get("pause_seconds"))
                    if kind == "straight" and item.get("round_no") is not None:
                        raise DomainValidationError("straight set forbids round_no")
                    if kind != "straight" and not (
                        1 <= (item.get("round_no") or 0) <= block["declared_rounds"]
                    ):
                        raise DomainValidationError("round_no is outside declared rounds")
                    kinds: set[str] = set()
                    for metric in item["metrics"]:
                        if metric["metric_kind"] in kinds:
                            raise DomainValidationError("metric_kind must be unique inside set")
                        kinds.add(metric["metric_kind"])
                        exact = metric.get("exact_value")
                        low, high = metric.get("min_value"), metric.get("max_value")
                        if not (
                            (exact is not None and low is None and high is None)
                            or (exact is None and low is not None and high is not None)
                        ):
                            raise DomainValidationError(
                                "metric requires exact value or complete range"
                            )
                        if low is not None and Decimal(str(low)) > Decimal(str(high)):
                            raise DomainValidationError(
                                "metric min_value must not exceed max_value"
                            )


def _unique_positions(items: list[dict[str, Any]], name: str) -> None:
    positions = [item["position"] for item in items]
    if len(positions) != len(set(positions)):
        raise DomainValidationError(f"duplicate positions in {name}")


def _validate_pause_seconds(value: Any) -> None:
    """Reject values PostgreSQL numeric(8,3) cannot represent exactly.

    The API must never let PostgreSQL round a planned pause behind the user's
    back: the canonical preview and the committed tree have to contain exactly
    the value the client confirmed.
    """
    if value is None:
        return
    try:
        decimal = Decimal(str(value))
    except Exception as error:  # pragma: no cover - Pydantic normally catches this
        raise DomainValidationError("pause_seconds must be a decimal") from error
    exponent = decimal.as_tuple().exponent
    if (
        not decimal.is_finite()
        or decimal < 0
        or decimal > Decimal("99999.999")
        or not isinstance(exponent, int)
        or exponent < -3
    ):
        raise DomainValidationError("pause_seconds must fit numeric(8,3) without rounding")


def assess_applicability(scopes: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    """Conservative exact matching; unstructured or unknown restrictions never match.

    Only an explicit universal population and exact known inclusion/exclusion fields
    can be evaluated without interpreting scientific prose. No fuzzy inference or DSL.
    """
    outcomes: list[dict[str, Any]] = []
    for scope in scopes:
        inclusion, exclusion = scope["inclusion"], scope["exclusion"]
        if not isinstance(inclusion, dict) or not isinstance(exclusion, dict):
            outcomes.append({"scope_id": str(scope["scope_id"]), "outcome": "insufficient"})
            continue
        known = (
            scope["population"].strip().casefold() == "all"
            and all(key in facts and facts[key] is not None for key in (*inclusion, *exclusion))
            and all(scope.get(key) is None for key in ("activity_or_load", "setting"))
        )
        goal_matches = scope.get("goal") is None or scope["goal"] == facts.get("goal_type")
        matched = (
            known
            and goal_matches
            and all(facts[k] == v for k, v in inclusion.items())
            and not any(facts[k] == v for k, v in exclusion.items())
        )
        outcomes.append(
            {
                "scope_id": str(scope["scope_id"]),
                "outcome": "applicable"
                if matched
                else "not_applicable"
                if known
                else "insufficient",
            }
        )
    return {
        "outcome": "applicable"
        if any(r["outcome"] == "applicable" for r in outcomes)
        else "insufficient",
        "scopes": outcomes,
        "method": "conservative_exact_match_v1",
    }
