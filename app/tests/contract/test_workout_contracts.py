from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.transport.http.contracts import (
    ActualLoadComponentInput,
    ActualSetInput,
    PlanLoadInput,
    WorkoutReportInput,
)


def test_actual_set_preserves_zero_and_keeps_status_separate_from_result() -> None:
    actual_set = ActualSetInput(
        position=1,
        logical_set_no=1,
        set_type_code="working",
        full_reps=0,
        status="interrupted",
        metrics=[],
        loads=[],
    )
    assert actual_set.full_reps == 0
    assert actual_set.status == "interrupted"
    assert not hasattr(actual_set, "execution_result")


def test_skipped_set_requires_planned_set_and_has_no_actual_values() -> None:
    with pytest.raises(ValidationError):
        ActualSetInput(
            position=1,
            logical_set_no=1,
            set_type_code="working",
            status="skipped",
            metrics=[],
            loads=[],
        )
    with pytest.raises(ValidationError):
        ActualSetInput(
            position=1,
            planned_set_id=uuid4(),
            logical_set_no=1,
            set_type_code="working",
            full_reps=0,
            status="skipped",
            metrics=[],
            loads=[],
        )


def test_report_requires_exact_plan_pair_and_single_v1_session_type() -> None:
    base = {
        "local_start_date": date(2026, 9, 23),
        "started_at": datetime(2026, 9, 23, 22, 30, tzinfo=UTC),
        "session_type": "training",
        "lifecycle": "completed",
        "workout_metrics": [],
        "blocks": [],
        "confirmed_unknown_planned_exercise_ids": [],
        "confirmed_unknown_planned_set_ids": [],
        "confirmed_unknown_planned_load_component_ids": [],
        "confirmed_unknown_metrics": [],
    }
    assert WorkoutReportInput(**base).duration_seconds is None
    with pytest.raises(ValidationError):
        WorkoutReportInput(**{**base, "plan_id": uuid4()})
    with pytest.raises(ValidationError):
        WorkoutReportInput(**{**base, "session_type": "strength"})
    for field in (
        "confirmed_unknown_planned_exercise_ids",
        "confirmed_unknown_planned_set_ids",
        "confirmed_unknown_planned_load_component_ids",
        "confirmed_unknown_metrics",
    ):
        missing = {**base}
        del missing[field]
        with pytest.raises(ValidationError):
            WorkoutReportInput(**missing)


def test_report_keeps_skipped_unknown_and_replacement_execution_distinct() -> None:
    planned_exercise_id = uuid4()
    skipped_replacement_exercise_id = uuid4()
    unknown_exercise_id = uuid4()
    completed_set_id, skipped_set_id, unknown_set_id = uuid4(), uuid4(), uuid4()
    skipped_marker = {
        "position": 2,
        "exercise_id": skipped_replacement_exercise_id,
        "planned_exercise_id": skipped_replacement_exercise_id,
        "result": "skipped",
        "reason": "replacement",
        "sets": [],
    }
    replacement_execution = {
        "position": 3,
        "exercise_id": uuid4(),
        "planned_exercise_id": None,
        "result": "added",
        "replacement_source": {
            "kind": "skipped_execution",
            "block_position": 1,
            "exercise_position": 2,
        },
        "reason": "replacement",
        "sets": [
            {
                "position": 1,
                "planned_set_id": None,
                "logical_set_no": 1,
                "set_type_code": "working",
                "full_reps": 10,
                "status": "performed",
                "metrics": [],
                "loads": [],
            }
        ],
    }
    report = WorkoutReportInput.model_validate(
        {
            "plan_id": uuid4(),
            "plan_revision_id": uuid4(),
            "local_start_date": date(2026, 10, 10),
            "session_type": "training",
            "lifecycle": "completed",
            "workout_metrics": [],
            "blocks": [
                {
                    "position": 1,
                    "planned_block_id": uuid4(),
                    "block_type_code": "straight",
                    "exercises": [
                        {
                            "position": 1,
                            "exercise_id": planned_exercise_id,
                            "planned_exercise_id": planned_exercise_id,
                            "result": "partial",
                            "sets": [
                                {
                                    "position": 1,
                                    "planned_set_id": completed_set_id,
                                    "logical_set_no": 1,
                                    "set_type_code": "working",
                                    "full_reps": 8,
                                    "status": "performed",
                                    "metrics": [],
                                    "loads": [],
                                },
                                {
                                    "position": 2,
                                    "planned_set_id": skipped_set_id,
                                    "logical_set_no": 2,
                                    "set_type_code": "working",
                                    "status": "skipped",
                                    "metrics": [],
                                    "loads": [],
                                },
                            ],
                        },
                        skipped_marker,
                        replacement_execution,
                    ],
                }
            ],
            "confirmed_unknown_planned_exercise_ids": [unknown_exercise_id],
            "confirmed_unknown_planned_set_ids": [unknown_set_id],
            "confirmed_unknown_planned_load_component_ids": [],
            "confirmed_unknown_metrics": [],
        }
    )

    serialized = report.model_dump(mode="json")
    exercises = serialized["blocks"][0]["exercises"]
    actual_partial, skipped_marker_result, replacement_result = exercises
    assert actual_partial["sets"][0]["status"] == "performed"
    assert actual_partial["sets"][1]["status"] == "skipped"
    assert actual_partial["sets"][1]["full_reps"] is None
    assert str(unknown_set_id) in serialized["confirmed_unknown_planned_set_ids"]
    assert all(
        item.get("planned_set_id") != str(unknown_set_id)
        for exercise in exercises
        for item in exercise["sets"]
    )
    assert str(unknown_exercise_id) in serialized["confirmed_unknown_planned_exercise_ids"]
    assert skipped_marker_result["result"] == "skipped"
    assert skipped_marker_result["reason"] == "replacement"
    assert replacement_result["result"] == "added"
    assert replacement_result["replacement_source"] == {
        "kind": "skipped_execution",
        "block_position": 1,
        "exercise_position": 2,
    }


def test_calculation_metric_requires_an_exact_derived_value() -> None:
    from trainer.transport.http.contracts import WorkoutMetricInput

    with pytest.raises(ValidationError):
        WorkoutMetricInput(
            metric_kind="heart_rate_avg",
            value=Decimal("140"),
            unit_code="beats_per_minute",
            source_kind="calculation",
        )


def test_load_kind_direction_catalog_is_closed_and_consistent() -> None:
    base = {"position": 1, "load_kind": "external", "direction": None}
    assert PlanLoadInput(**base).load_kind == "external"
    assert ActualLoadComponentInput(**base).direction is None
    assert PlanLoadInput(position=1, load_kind="band", direction="assistance")
    assert ActualLoadComponentInput(position=1, load_kind="band", direction="resistance")
    for invalid in (
        {"position": 1, "load_kind": "barbell", "direction": None},
        {"position": 1, "load_kind": "band", "direction": None},
        {"position": 1, "load_kind": "bodyweight", "direction": "resistance"},
    ):
        with pytest.raises(ValidationError):
            PlanLoadInput(**invalid)
        with pytest.raises(ValidationError):
            ActualLoadComponentInput(**invalid)
