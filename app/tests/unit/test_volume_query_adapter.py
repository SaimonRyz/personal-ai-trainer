from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from trainer.infrastructure.db.progress import ProgressQueryRepository


def _row(
    *,
    exercise_id,
    execution_id,
    actual_set_id,
    load_id,
    unilateral=False,
    side=None,
    logical_set_no=1,
    mass="10",
    unit_code="kilogram",
    mass_meaning="total_external_mass",
    item_count=None,
    full_reps=5,
):
    return {
        "exercise_id": exercise_id,
        "exercise_revision_id": uuid4(),
        "execution_id": execution_id,
        "unilateral": unilateral,
        "actual_set_id": actual_set_id,
        "logical_set_no": logical_set_no,
        "round_no": None,
        "side": side,
        "set_type": "working",
        "status": "performed",
        "full_reps": full_reps,
        "partial_reps": 0,
        "actual_load_component_id": load_id,
        "load_kind": "external",
        "mass": mass,
        "unit_code": unit_code,
        "mass_meaning": mass_meaning,
        "item_count": item_count,
        "raw_ambiguous_text": None,
    }


def test_volume_query_adapter_normalizes_pounds_counts_items_and_actual_sides():
    profile_id, session_id, revision_id = uuid4(), uuid4(), uuid4()
    pound_exercise, item_exercise, unilateral_exercise = uuid4(), uuid4(), uuid4()
    rows = [
        _row(
            exercise_id=pound_exercise,
            execution_id=uuid4(),
            actual_set_id=uuid4(),
            load_id=uuid4(),
            mass="88",
            unit_code="pound",
            full_reps=6,
        ),
        _row(
            exercise_id=item_exercise,
            execution_id=uuid4(),
            actual_set_id=uuid4(),
            load_id=uuid4(),
            mass="10",
            mass_meaning="per_item_mass",
            item_count=2,
            full_reps=5,
        ),
        _row(
            exercise_id=unilateral_exercise,
            execution_id=(execution_id := uuid4()),
            actual_set_id=uuid4(),
            load_id=uuid4(),
            unilateral=True,
            side="left",
            mass="10",
            full_reps=8,
        ),
        _row(
            exercise_id=unilateral_exercise,
            execution_id=execution_id,
            actual_set_id=uuid4(),
            load_id=uuid4(),
            unilateral=True,
            side="right",
            mass="10",
            full_reps=8,
        ),
    ]
    session = MagicMock()
    session.execute.side_effect = [
        SimpleNamespace(
            mappings=lambda: [
                {
                    "session_id": session_id,
                    "current_revision_id": revision_id,
                    "local_start_date": date(2026, 10, 1),
                }
            ]
        ),
        SimpleNamespace(mappings=lambda: rows),
    ]

    items = ProgressQueryRepository(session)._volume_items(
        profile_id, date(2026, 10, 1), date(2026, 10, 31), None
    )

    by_exercise = {item["exercise_ref"]["object_id"]: item for item in items}
    assert by_exercise[str(pound_exercise)]["value"]["exact_value"] == (
        Decimal("88") * Decimal("0.45359237") * 6
    )
    assert by_exercise[str(item_exercise)]["value"]["exact_value"] == Decimal("100")
    assert by_exercise[str(unilateral_exercise)]["value"]["exact_value"] == Decimal("160")
    assert by_exercise[str(unilateral_exercise)]["completeness"] == "complete"
