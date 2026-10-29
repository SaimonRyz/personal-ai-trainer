"""Valid historical record dates remain useful; invalidated facts do not."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from trainer.infrastructure.db.progress import ProgressQueryRepository
from trainer.transport.http.progress_contracts import ProgressQueryResponse


@pytest.mark.parametrize(
    ("eligible", "expected"),
    [([True, True, True], "descriptive_direction"), ([False, True, True], "two_points")],
)
def test_actual_progress_counts_valid_superseded_dates_without_leaking_internal_flag(
    monkeypatch, eligible, expected
):
    profile, exercise, condition = uuid4(), uuid4(), uuid4()
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [profile]
    rows = [
        {
            "category": "actual_record",
            "record_ref": {"object_type": "actual_record", "object_id": str(uuid4())},
            "exercise_ref": {"object_type": "exercise", "object_id": str(exercise)},
            "metric_kind": "record_external_load",
            "value": {"exact_value": value, "unit_code": "kilogram"},
            "result_date": day,
            "condition_id": str(condition),
            "source_session_ref": None,
            "source_set_ref": None,
            "lifecycle": "current" if index == 2 else "historical",
            "is_historical": index != 2,
            "comparison_eligible": eligible[index],
        }
        for index, (day, value) in enumerate(
            (("2026-01-10", "60"), ("2026-01-24", "62.5"), ("2026-02-10", "65"))
        )
    ]
    repository = ProgressQueryRepository(session)
    monkeypatch.setattr(repository, "_actual_record_items", lambda *_args: rows)
    result = repository.query(
        {
            "metric_kind": "record_external_load",
            "date_from": "2026-01-01",
            "date_to": "2026-02-28",
            "exercise_id": str(exercise),
            "condition_id": str(condition),
            "include_history": True,
            "limit": 100,
            "cursor": None,
        }
    )
    assert result["comparison_status"] == expected
    assert len(result["items"]) == 3  # Ineligible history remains visible.
    ProgressQueryResponse.model_validate(result)  # No new wire fields.


@pytest.mark.parametrize(
    ("condition", "state", "eligible"),
    [
        ({"scope": "comparable", "unknown_fields": []}, {"validity": "valid"}, True),
        (
            {"scope": "comparable", "unknown_fields": []},
            {"current_exit_reason": "better_record"},
            True,
        ),
        ({"scope": "comparable", "unknown_fields": []}, {"validity": "source_invalidated"}, False),
        (
            {"scope": "comparable", "unknown_fields": []},
            {"validity": "replaced_by_correction"},
            False,
        ),
        (
            {"scope": "comparable", "unknown_fields": []},
            {"current_exit_reason": "replaced_by_correction"},
            False,
        ),
        (
            {"scope": "comparable", "unknown_fields": []},
            {"current_exit_reason": "source_corrected"},
            False,
        ),
        ({"scope": "historical_unknown", "unknown_fields": ["load"]}, {"validity": "valid"}, False),
        ({"scope": "comparable", "unknown_fields": ["side"]}, {"validity": "valid"}, False),
        (None, {"validity": "valid"}, False),
        ({"scope": "comparable", "unknown_fields": []}, None, False),
    ],
)
def test_actual_progress_eligibility_does_not_revive_corrected_or_unknown_records(
    condition, state, eligible
):
    assert ProgressQueryRepository._actual_record_comparison_eligible(condition, state) is eligible
