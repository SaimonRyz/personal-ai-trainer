from __future__ import annotations

import re
import unicodedata
from typing import Any

from trainer.domain.context import DomainValidationError

NORMALIZATION_VERSION = "exercise-name-v1"
ALLOWED_MEASUREMENT_COMBINATIONS = frozenset(
    (metric_kind, requiredness, unit_family)
    for metric_kind, unit_family in (
        ("repetitions", "count"),
        ("duration", "time"),
        ("distance", "length"),
    )
    for requiredness in ("required", "optional")
)


def normalize_name(value: str) -> str:
    """Apply the owner-approved, versioned catalogue-name normalization."""
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", normalized).strip()


def validate_exercise(payload: dict[str, Any]) -> None:
    exercise = payload["exercise"]
    name = exercise["canonical_name"].strip()
    if not name or len(name) > 200:
        raise DomainValidationError("canonical_name must contain 1..200 characters")
    if exercise["status_code"] == "active" and not any(
        role["role"] == "primary" for role in exercise["muscle_roles"]
    ):
        raise DomainValidationError("active exercise requires a primary muscle group")

    aliases: set[str] = set()
    for alias in exercise["aliases"]:
        normalized = normalize_name(alias["alias"])
        if not normalized:
            raise DomainValidationError("alias cannot be blank")
        if normalized in aliases:
            raise DomainValidationError("duplicate normalized alias")
        aliases.add(normalized)

    roles: set[tuple[str, str]] = set()
    for role in exercise["muscle_roles"]:
        normalized_role_name = normalize_name(role.get("name", ""))
        if role.get("kind") == "create_by_name" and not normalized_role_name:
            raise DomainValidationError("create_by_name name cannot be blank")
        reference = role.get("muscle_group_id") or normalized_role_name
        role_key = (str(reference), role["role"])
        if role_key in roles:
            raise DomainValidationError("duplicate muscle role")
        roles.add(role_key)

    metrics: set[str] = set()
    for index, measurement in enumerate(exercise["measurements"]):
        if measurement["metric_kind"] in metrics:
            raise DomainValidationError("duplicate measurement metric_kind")
        combination = (
            measurement["metric_kind"],
            measurement["requiredness"],
            measurement["unit_family"],
        )
        if combination not in ALLOWED_MEASUREMENT_COMBINATIONS:
            kind = measurement["metric_kind"]
            expected_family = {
                "repetitions": "count",
                "duration": "time",
                "distance": "length",
            }.get(kind)
            if expected_family is None:
                path = f"exercise.measurements[{index}].metric_kind"
                detail = (
                    "Use repetitions, duration, or distance. External mass is a load "
                    "on each planned or performed set, not an exercise measurement."
                )
                code = "unsupported_metric_kind"
            else:
                path = f"exercise.measurements[{index}].unit_family"
                detail = f"{kind} requires unit_family={expected_family}."
                code = "incompatible_unit_family"
            raise DomainValidationError(
                "invalid exercise measurement",
                status_code=422,
                field_errors=[{"path": path, "code": code, "message_safe": detail}],
            )
        metrics.add(measurement["metric_kind"])

    direct: set[tuple[str, str]] = set()
    alternatives: dict[str, set[str]] = {}
    for requirement in exercise["equipment_requirements"]:
        optionality = requirement["optionality"]
        group = requirement.get("alternative_group")
        if optionality == "alternative":
            if not group or not group.strip():
                raise DomainValidationError("alternative equipment requires a group")
            alternatives.setdefault(group.strip(), set()).add(requirement["equipment_type"])
        elif group is not None:
            raise DomainValidationError("required or optional equipment cannot have a group")
        equipment_key = (requirement["equipment_type"], optionality)
        if equipment_key in direct:
            raise DomainValidationError("duplicate equipment requirement")
        direct.add(equipment_key)
    if any(len(types) < 2 for types in alternatives.values()):
        raise DomainValidationError("alternative equipment group requires two distinct types")
