from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date
from typing import Any
from uuid import UUID


class DomainValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        field_errors: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.field_errors = field_errors or []


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_payload(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def materialize_onboarding_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy the global context package without inferring inventory facts."""
    return deepcopy(payload)


def validate_goal(goal: dict[str, Any], *, path: str = "goal") -> None:
    if not str(goal.get("name", "")).strip():
        raise DomainValidationError("goal name is required")
    if goal.get("goal_type") not in {"performance", "strength"}:
        raise DomainValidationError(
            "unsupported goal type; use performance or strength",
            field_errors=[{
                "path": f"{path}.goal_type",
                "code": "unsupported_goal_type",
                "message_safe": "Use performance or strength.",
            }],
        )
    set_on = date.fromisoformat(str(goal["set_on"]))
    target_date = date.fromisoformat(str(goal["target_date"]))
    if target_date < set_on:
        raise DomainValidationError("target_date must be on or after set_on")
    priority = int(goal["priority"])
    if not 1 <= priority <= 100:
        raise DomainValidationError("priority must be between 1 and 100")
    if goal.get("parent_goal_id") == goal.get("goal_id") and goal.get("goal_id") is not None:
        raise DomainValidationError("goal cannot be its own parent")
    values_present = goal.get("start_value") is not None or goal.get("target_value") is not None
    if values_present and goal.get("unit_code") is None:
        raise DomainValidationError("unit_code is required for numeric goal values")


def validate_onboarding(payload: dict[str, Any]) -> None:
    goals = payload["goals"]
    if not goals:
        raise DomainValidationError("at least one goal is required")
    for index, goal in enumerate(goals):
        validate_goal(goal, path=f"goals[{index}]")
    goal_ids = [UUID(str(item["goal_id"])) for item in goals if item.get("goal_id") is not None]
    if len(goal_ids) != len(set(goal_ids)):
        raise DomainValidationError("goal identifiers must be unique")
    known_goal_ids = set(goal_ids)
    if len(goal_ids) == len(goals):
        parents: dict[UUID, UUID | None] = {}
        for goal in goals:
            goal_id = UUID(str(goal["goal_id"]))
            parent = (
                UUID(str(goal["parent_goal_id"]))
                if goal.get("parent_goal_id") is not None
                else None
            )
            if parent is not None and parent not in known_goal_ids:
                raise DomainValidationError("parent goal must belong to the onboarding package")
            parents[goal_id] = parent
        for start in parents:
            seen: set[UUID] = set()
            current: UUID | None = start
            while current is not None:
                if current in seen:
                    raise DomainValidationError("goal parent hierarchy must be acyclic")
                seen.add(current)
                current = parents[current]
    place_ids = [UUID(str(item["place_id"])) for item in payload["places"]]
    equipment_ids = [UUID(str(item["equipment_id"])) for item in payload["equipment"]]
    if len(place_ids) != len(set(place_ids)) or len(equipment_ids) != len(set(equipment_ids)):
        raise DomainValidationError("package identifiers must be unique")
    familiar_exercises = payload.get("familiar_exercises", [])
    exercise_refs: list[tuple[UUID, UUID]] = []
    for item in familiar_exercises:
        ref = item["exercise_ref"]
        if ref.get("object_type") != "exercise" or ref.get("revision_id") is None:
            raise DomainValidationError(
                "familiar exercise must reference an exact exercise revision"
            )
        exercise_refs.append((UUID(str(ref["object_id"])), UUID(str(ref["revision_id"]))))
    if len(exercise_refs) != len(set(exercise_refs)):
        raise DomainValidationError("familiar exercise references must be unique")
    usual_place_id = payload["profile"].get("usual_place_id")
    if usual_place_id is not None and UUID(str(usual_place_id)) not in place_ids:
        raise DomainValidationError("usual_place_id references unknown place")
    if "place_equipment_availability" in payload:
        raise DomainValidationError("place availability is excluded from current onboarding")
    from trainer.domain.inventory import validate_card

    for item in payload["equipment"]:
        validate_card({key: value for key, value in item.items() if key != "equipment_id"})
