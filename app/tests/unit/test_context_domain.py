from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from trainer.domain.context import (
    DomainValidationError,
    materialize_onboarding_defaults,
    validate_goal,
    validate_onboarding,
)


def goal(
    *, goal_id: str, parent_goal_id: str | None = None, priority: int = 10
) -> dict[str, object]:
    return {
        "goal_id": goal_id,
        "parent_goal_id": parent_goal_id,
        "name": "Synthetic goal",
        "description": None,
        "goal_type": "performance",
        "start_value": "1",
        "target_value": "2",
        "unit_code": "repetition",
        "set_on": "2026-08-24",
        "target_date": "2026-12-31",
        "priority": priority,
        "status": "active",
        "comment": None,
    }


def onboarding(goals: list[dict[str, object]]) -> dict[str, object]:
    place_id, equipment_id = str(uuid4()), str(uuid4())
    return {
        "context": {
            "conversation_id": "synthetic",
            "message_id": "synthetic",
            "effective_at": datetime.now(UTC).isoformat(),
            "timezone": "Asia/Novokuznetsk",
            "locale": None,
        },
        "profile": {"usual_place_id": place_id},
        "goals": goals,
        "places": [{"place_id": place_id}],
        "equipment": [
            {
                "equipment_id": equipment_id,
                "equipment_type": "barbell",
                "name": "Synthetic barbell",
                "maker": None,
                "brand": None,
                "model": None,
                "quantity": None,
                "quantity_unit_code": None,
                "condition": None,
                "comment": None,
                "characteristics": {
                    "mass_per_item_kg": None,
                    "bar_sleeve_diameter_mm": None,
                    "plate_bore_diameter_mm": None,
                },
            }
        ],
    }


def test_goal_requires_valid_date_priority_and_unit() -> None:
    candidate = goal(goal_id=str(uuid4()))
    candidate["target_date"] = "2026-08-23"
    with pytest.raises(DomainValidationError, match="target_date"):
        validate_goal(candidate)
    candidate = goal(goal_id=str(uuid4()), priority=0)
    with pytest.raises(DomainValidationError, match="priority"):
        validate_goal(candidate)
    candidate = goal(goal_id=str(uuid4()), priority=101)
    with pytest.raises(DomainValidationError, match="priority"):
        validate_goal(candidate)
    candidate = goal(goal_id=str(uuid4()))
    candidate["unit_code"] = None
    with pytest.raises(DomainValidationError, match="unit_code"):
        validate_goal(candidate)


def test_onboarding_accepts_parent_first_or_child_first_and_rejects_cycles() -> None:
    parent_id, child_id = str(uuid4()), str(uuid4())
    parent = goal(goal_id=parent_id)
    child = goal(goal_id=child_id, parent_goal_id=parent_id)
    validate_onboarding(onboarding([child, parent]))

    parent["parent_goal_id"] = child_id
    with pytest.raises(DomainValidationError, match="acyclic"):
        validate_onboarding(onboarding([child, parent]))


def test_onboarding_rejects_unknown_usual_place_and_parent() -> None:
    candidate = onboarding([goal(goal_id=str(uuid4()), parent_goal_id=str(uuid4()))])
    with pytest.raises(DomainValidationError, match="parent goal"):
        validate_onboarding(candidate)
    candidate = onboarding([goal(goal_id=str(uuid4()))])
    candidate["profile"]["usual_place_id"] = str(uuid4())  # type: ignore[index]
    with pytest.raises(DomainValidationError, match="usual_place_id"):
        validate_onboarding(candidate)


def test_goal_rejects_self_parent() -> None:
    goal_id = str(uuid4())
    with pytest.raises(DomainValidationError, match="own parent"):
        validate_goal(goal(goal_id=goal_id, parent_goal_id=goal_id))


def test_onboarding_preserves_unknowns_and_rejects_legacy_availability() -> None:
    candidate = onboarding([goal(goal_id=str(uuid4()))])
    materialized = materialize_onboarding_defaults(candidate)
    assert materialized == candidate
    assert materialized is not candidate
    validate_onboarding(materialized)
    materialized["place_equipment_availability"] = []
    with pytest.raises(DomainValidationError, match="place availability"):
        validate_onboarding(materialized)


def test_onboarding_familiarity_requires_unique_exact_exercise_refs() -> None:
    exercise_id, revision_id = str(uuid4()), str(uuid4())
    candidate = onboarding([goal(goal_id=str(uuid4()))])
    candidate["familiar_exercises"] = [
        {
            "exercise_ref": {
                "object_type": "exercise",
                "object_id": exercise_id,
                "revision_id": revision_id,
            }
        }
    ]
    validate_onboarding(candidate)
    candidate["familiar_exercises"].append(candidate["familiar_exercises"][0])  # type: ignore[union-attr]
    with pytest.raises(DomainValidationError, match="must be unique"):
        validate_onboarding(candidate)
    candidate["familiar_exercises"] = [  # type: ignore[assignment]
        {"exercise_ref": {"object_type": "exercise", "object_id": exercise_id, "revision_id": None}}
    ]
    with pytest.raises(DomainValidationError, match="exact exercise revision"):
        validate_onboarding(candidate)
