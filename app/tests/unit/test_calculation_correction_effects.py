from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

from trainer.infrastructure.db.workouts import WorkoutRepository

SESSION_ID = UUID("8d5ef338-a9f5-4a94-a87f-89166fc62fc0")
REVISION_ID = UUID("d75d2fa9-eb81-48d1-b586-b4f2aaf9571b")
PROFILE_ID = UUID("c8855942-6ad9-4f3a-b80b-ef2af94d4373")
EXERCISE_ID = UUID("f0851bd1-dd74-4e9d-9c79-140798b0d28b")
CONDITION_ID = UUID("0ed98a6a-7264-4cc9-a8f4-476f26390cb0")
RETIRED_BEST_ID = UUID("e819d687-97bc-4706-af02-7b019c9a8369")
RETIRED_DERIVED_ID = UUID("b2270598-586e-4f90-b1b4-d81e6b5ec1a3")
FALLBACK_DERIVED_ID = UUID("ce039eb5-5f0b-4553-a195-98a977b3a4f1")
FALLBACK_SESSION_ID = UUID("f765be5e-5ca2-4c1c-951c-1bc43f99878a")
FALLBACK_REVISION_ID = UUID("789d70d3-d72d-4f71-b48c-d17f76ac7867")
FALLBACK_SET_ID = UUID("703fd5c2-7118-4dcf-84c1-0131b74781f8")


def _result(rows: list[dict[str, object]]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    result.mappings.return_value.one_or_none.return_value = rows[0] if rows else None
    return result


def test_correction_fallback_uses_saved_point_without_existing_best_row() -> None:
    affected = {
        "profile_id": PROFILE_ID,
        "exercise_id": EXERCISE_ID,
        "metric_kind": "estimated_1rm",
        "condition_id": CONDITION_ID,
        "calculated_best_id": RETIRED_BEST_ID,
        "derived_value_id": RETIRED_DERIVED_ID,
        "value": 100,
        "achieved_on": date(2026, 9, 1),
    }
    candidate = {
        "derived_value_id": FALLBACK_DERIVED_ID,
        "exercise_id": EXERCISE_ID,
        "condition_id": CONDITION_ID,
        "session_id": FALLBACK_SESSION_ID,
        "session_revision_id": FALLBACK_REVISION_ID,
        "source_actual_set_id": FALLBACK_SET_ID,
        "profile_id": PROFILE_ID,
        "value": 95,
        "achieved_on": date(2026, 8, 1),
        "calculated_best_id": None,
    }
    session = MagicMock()
    session.execute.side_effect = [_result([affected]), _result([candidate])]
    repository = WorkoutRepository(SimpleNamespace(session=session))

    effects = repository._plan_calculation_best_effects(SESSION_ID, REVISION_ID)

    assert len(effects) == 1
    effect = effects[0]
    assert effect["fallback_create"] is True
    assert effect["fallback_derived_value_id"] == str(FALLBACK_DERIVED_ID)
    assert effect["fallback_session_id"] == str(FALLBACK_SESSION_ID)
    assert effect["fallback_session_revision_id"] == str(FALLBACK_REVISION_ID)
    assert effect["fallback_source_actual_set_id"] == str(FALLBACK_SET_ID)
    assert effect["fallback_value"] == "95"
    assert effect["fallback_date"] == "2026-08-01"
    assert effect["fallback_best_id"]


def test_correction_fallback_prefers_existing_calculated_best_row() -> None:
    affected = {
        "profile_id": PROFILE_ID,
        "exercise_id": EXERCISE_ID,
        "metric_kind": "estimated_1rm",
        "condition_id": CONDITION_ID,
        "calculated_best_id": RETIRED_BEST_ID,
        "derived_value_id": RETIRED_DERIVED_ID,
        "value": 100,
        "achieved_on": date(2026, 9, 1),
    }
    existing_best_id = UUID("01591cb5-e995-4a2d-87b0-91beb0768c71")
    candidate = {
        "derived_value_id": FALLBACK_DERIVED_ID,
        "exercise_id": EXERCISE_ID,
        "condition_id": CONDITION_ID,
        "session_id": FALLBACK_SESSION_ID,
        "session_revision_id": FALLBACK_REVISION_ID,
        "source_actual_set_id": FALLBACK_SET_ID,
        "profile_id": PROFILE_ID,
        "value": 95,
        "achieved_on": date(2026, 8, 1),
        "calculated_best_id": existing_best_id,
    }
    session = MagicMock()
    session.execute.side_effect = [_result([affected]), _result([candidate])]
    repository = WorkoutRepository(SimpleNamespace(session=session))

    effect = repository._plan_calculation_best_effects(SESSION_ID, REVISION_ID)[0]

    assert effect["fallback_create"] is False
    assert effect["fallback_best_id"] == str(existing_best_id)


def test_correction_with_no_remaining_point_has_no_current_fallback() -> None:
    affected = {
        "profile_id": PROFILE_ID,
        "exercise_id": EXERCISE_ID,
        "metric_kind": "estimated_1rm",
        "condition_id": CONDITION_ID,
        "calculated_best_id": RETIRED_BEST_ID,
        "derived_value_id": RETIRED_DERIVED_ID,
        "value": 100,
        "achieved_on": date(2026, 9, 1),
    }
    session = MagicMock()
    session.execute.side_effect = [_result([affected]), _result([])]
    repository = WorkoutRepository(SimpleNamespace(session=session))

    effect = repository._plan_calculation_best_effects(SESSION_ID, REVISION_ID)[0]

    assert effect["fallback_best_id"] is None
    assert effect["fallback_derived_value_id"] is None
    assert effect["fallback_create"] is False
