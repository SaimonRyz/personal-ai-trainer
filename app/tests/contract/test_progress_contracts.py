from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.calculations import CalculationRepository
from trainer.infrastructure.db.progress import ProgressQueryRepository
from trainer.transport.http.progress_contracts import (
    CalculationRequest,
    CalculationResult,
    ProgressQueryRequest,
    ProgressQueryResponse,
)


def context():
    return {
        "conversation_id": "synthetic",
        "message_id": "progress-query",
        "effective_at": "2026-10-04T12:00:00+07:00",
        "timezone": "Asia/Novokuznetsk",
    }


def test_progress_query_rejects_reversed_date_range_and_volume_condition():
    with pytest.raises(ValidationError):
        ProgressQueryRequest.model_validate(
            {
                "context": context(),
                "metric_kind": "estimated_1rm",
                "date_from": "2026-10-05",
                "date_to": "2026-10-04",
            }
        )
    with pytest.raises(ValidationError):
        ProgressQueryRequest.model_validate(
            {
                "context": context(),
                "metric_kind": "external_working_set_volume",
                "date_from": "2026-10-01",
                "date_to": "2026-10-04",
                "condition_id": str(uuid4()),
            }
        )


def test_calculation_request_requires_exact_attestation_and_forbids_volume_persistence():
    common = {
        "context": context(),
        "method_id": "e1rm_brzycki",
        "method_version": "1",
        "mode": "persist",
        "source": {
            "kind": "actual_set",
            "session_id": str(uuid4()),
            "session_revision_id": str(uuid4()),
            "exercise_id": str(uuid4()),
            "exercise_revision_id": str(uuid4()),
            "actual_set_id": str(uuid4()),
        },
        "request_digest": "0" * 64,
    }
    with pytest.raises(ValidationError):
        CalculationRequest.model_validate(common)

    actual_set_id = uuid4()
    valid = {
        **common,
        "source": {**common["source"], "actual_set_id": str(actual_set_id)},
        "execution_attestation": {
            "source_actual_set_id": str(actual_set_id),
            "grip_condition": "explicitly reported",
            "bench_pause_condition": "no_pause",
            "standard_confirmed": True,
            "spotter_released_before_rep": True,
            "no_assistance_during_rep": True,
        },
    }
    assert CalculationRequest.model_validate(valid).source.actual_set_id == actual_set_id
    assert (
        CalculationRequest.model_validate(
            {**valid, "prior_derived_value_id": str(uuid4())}
        ).prior_derived_value_id
        is not None
    )
    with pytest.raises(ValidationError):
        CalculationRequest.model_validate({**valid, "mode": "attached"})
    with pytest.raises(ValidationError, match="only for persistent e1RM"):
        CalculationRequest.model_validate(
            {**valid, "mode": "ephemeral", "prior_derived_value_id": str(uuid4())}
        )

    volume = {
        **common,
        "method_id": "external_working_set_volume",
        "mode": "persist",
        "source": {
            "kind": "exercise_session",
            "session_id": str(uuid4()),
            "session_revision_id": str(uuid4()),
            "exercise_id": str(uuid4()),
        },
        "execution_attestation": None,
    }
    with pytest.raises(ValidationError, match="read-time only"):
        CalculationRequest.model_validate(volume)
    with pytest.raises(ValidationError, match="only for persistent e1RM"):
        CalculationRequest.model_validate(
            {**volume, "mode": "ephemeral", "prior_derived_value_id": str(uuid4())}
        )


def test_progress_cursor_binds_keyset_boundary_and_all_query_filters():
    filters = {
        "metric_kind": "estimated_1rm",
        "date_from": "2026-01-01",
        "date_to": "2026-10-04",
        "exercise_id": None,
        "condition_id": None,
        "include_history": True,
        "limit": 50,
    }
    digest = digest_payload(filters)
    boundary = ["2026-10-04", str(uuid4()), str(uuid4()), "derived", "a" * 64]
    cursor = ProgressQueryRepository._encode_cursor(digest, boundary)
    assert ProgressQueryRepository._decode_cursor(cursor, digest) == boundary
    with pytest.raises(RuntimeError, match="does not match"):
        ProgressQueryRepository._decode_cursor(cursor, "f" * 64)


def test_comparison_status_excludes_stale_and_replaced_derived_points():
    exercise_ref = {"object_id": str(uuid4())}
    current = {
        "kind": "derived",
        "point": {
            "exercise_ref": exercise_ref,
            "condition_id": str(uuid4()),
            "method_id": "e1rm_brzycki",
            "method_version": "1",
            "state": "current",
            "completeness": "complete",
            "achieved_on": "2026-10-01",
        },
    }
    historical = [
        {
            "kind": "derived",
            "point": {
                **current["point"],
                "state": state,
                "achieved_on": day,
            },
        }
        for state, day in (("stale", "2026-10-02"), ("replaced", "2026-10-03"))
    ]

    assert ProgressQueryRepository._comparison_status([current, *historical]) == "one_point"
    assert ProgressQueryRepository._comparison_status(historical) == "insufficient"


def test_progress_query_cursor_pages_stable_points_without_gaps_or_duplicates(monkeypatch):
    from unittest.mock import MagicMock

    profile_id, exercise_id = uuid4(), uuid4()
    session_id, revision_id = uuid4(), uuid4()
    records = [
        {
            "category": "actual_record",
            "record_ref": {"object_type": "actual_record", "object_id": str(uuid4())},
            "exercise_ref": {"object_type": "exercise", "object_id": str(exercise_id)},
            "metric_kind": "record_full_reps",
            "value": {"exact_value": "8", "unit_code": "repetition"},
            "result_date": "2026-10-01",
            "condition_id": str(uuid4()),
            "source_session_ref": {
                "object_type": "workout_session",
                "object_id": str(session_id),
                "revision_id": str(revision_id),
            },
            "source_set_ref": None,
            "lifecycle": "current",
            "is_historical": False,
        }
        for _ in range(3)
    ]
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [profile_id]
    repository = ProgressQueryRepository(session)
    monkeypatch.setattr(repository, "_actual_record_items", lambda *_args: records)
    query = {
        "metric_kind": "record_full_reps",
        "date_from": "2026-10-01",
        "date_to": "2026-10-31",
        "exercise_id": str(exercise_id),
        "condition_id": None,
        "include_history": True,
        "limit": 1,
        "cursor": None,
    }

    first = repository.query(query)
    second = repository.query({**query, "cursor": first["next_cursor"]})
    third = repository.query({**query, "cursor": second["next_cursor"]})
    point_keys = [page["items"][0]["point_key"] for page in (first, second, third)]

    assert all(page["has_more"] for page in (first, second))
    assert third["has_more"] is False
    assert third["next_cursor"] is None
    assert len(set(point_keys)) == 3
    ordered_keys = sorted(point_keys)
    assert point_keys == ordered_keys
    assert all(
        page["items"][0]["point"]["achieved_on"] == "2026-10-01" for page in (first, second, third)
    )


@pytest.mark.parametrize("state", ["current", "stale", "replaced"])
def test_persisted_derived_progress_dto_preserves_lifecycle(state):
    point = {
        "derived_value_ref": {
            "object_type": "derived_value",
            "object_id": str(uuid4()),
        },
        "method_id": "e1rm_brzycki",
        "method_version": "1",
        "metric_kind": "estimated_1rm",
        "value": {"exact_value": "90.000000", "unit_code": "kilogram"},
        "achieved_on": "2026-10-04",
        "exercise_ref": {"object_type": "exercise", "object_id": str(uuid4())},
        "exercise_revision_ref": {
            "object_type": "exercise_revision",
            "object_id": str(uuid4()),
        },
        "session_ref": {
            "object_type": "workout_session",
            "object_id": str(uuid4()),
            "revision_id": str(uuid4()),
        },
        "session_revision_ref": {
            "object_type": "workout_session_revision",
            "object_id": str(uuid4()),
        },
        "source_actual_set_ref": {
            "object_type": "actual_set",
            "object_id": str(uuid4()),
            "revision_id": str(uuid4()),
        },
        "condition_id": str(uuid4()),
        "state": state,
        "completeness": "complete",
        "included_count": 1,
        "excluded_count": 0,
        "inputs": [],
        "exclusions": [],
        "input_digest": "a" * 64,
        "result_digest": "b" * 64,
        "stale_reason": "source workout revision was corrected" if state != "current" else None,
    }
    item = ProgressQueryResponse.model_validate(
        {
            "items": [{"kind": "derived", "point_key": "c" * 64, "point": point}],
            "next_cursor": None,
            "has_more": False,
            "gaps": [],
            "comparison_status": "one_point",
        }
    )

    assert item.items[0].point.state == state


def test_progress_response_is_a_closed_fact_or_derived_union_with_comparison_fields():
    fact = {
        "kind": "actual",
        "point_key": "a" * 64,
        "point": {
            "record_ref": {"object_type": "actual_record", "object_id": str(uuid4())},
            "exercise_ref": {"object_type": "exercise", "object_id": str(uuid4())},
            "metric_kind": "record_full_reps",
            "value": {"exact_value": "12", "unit_code": "repetition"},
            "achieved_on": "2026-10-04",
            "condition_id": str(uuid4()),
            "session_ref": None,
            "actual_set_ref": None,
            "is_current": True,
            "is_historical": False,
        },
    }
    response = ProgressQueryResponse.model_validate(
        {
            "items": [fact],
            "next_cursor": None,
            "has_more": False,
            "gaps": [],
            "comparison_status": "one_point",
        }
    )
    assert response.items[0].kind == "actual"
    with pytest.raises(ValidationError):
        ProgressQueryResponse.model_validate(
            {
                "items": [fact],
                "next_cursor": None,
                "has_more": False,
                "gaps": [],
                "comparison_status": "one_point",
                "uncontracted": True,
            }
        )


def test_ephemeral_volume_point_key_accepts_database_uuid_and_decimal_snapshots():
    from decimal import Decimal

    session_id, revision_id, exercise_id, set_id, load_id, equipment_id = [
        uuid4() for _ in range(6)
    ]
    item = {
        "category": "external_volume",
        "metric_kind": "external_working_set_volume",
        "value": {"exact_value": Decimal("240"), "unit_code": "kilogram_repetition"},
        "result_date": "2026-10-04",
        "exercise_ref": {
            "object_type": "exercise",
            "object_id": str(exercise_id),
            "revision_id": None,
        },
        "condition_id": None,
        "source_session_ref": {
            "object_type": "workout_session",
            "object_id": str(session_id),
            "revision_id": str(revision_id),
        },
        "source_set_ref": None,
        "source_derived_value_ref": None,
        "method_id": "external_working_set_volume",
        "method_version": "1",
        "completeness": "complete",
        "lifecycle": "current",
        "inputs": [
            {
                "role": "load_component",
                "source_ref": {
                    "object_type": "actual_load_component",
                    "object_id": str(load_id),
                    "revision_id": str(revision_id),
                },
                "normalized_value": "40",
                "unit_code": "kilogram",
                "snapshot": {
                    "actual_load_component_id": load_id,
                    "equipment_id": equipment_id,
                    "mass": Decimal("40"),
                },
            },
            {
                "role": "actual_set",
                "source_ref": {
                    "object_type": "actual_set",
                    "object_id": str(set_id),
                    "revision_id": str(revision_id),
                },
                "normalized_value": "6",
                "unit_code": "repetition",
                "snapshot": {"full_reps": 6},
            },
        ],
        "stale_reason": None,
        "included_count": 1,
        "excluded_count": 0,
        "included_source_refs": [],
        "excluded_sources": [],
        "input_digest": None,
        "result_digest": None,
    }
    point = ProgressQueryRepository._present(item)
    assert point["point_key"] == ProgressQueryRepository._present(item)["point_key"]
    response = ProgressQueryResponse.model_validate(
        {
            "items": [{key: value for key, value in point.items() if key != "sort_key"}],
            "next_cursor": None,
            "has_more": False,
            "gaps": [],
            "comparison_status": "one_point",
        }
    )
    assert response.items[0].kind == "derived"


@pytest.mark.parametrize(
    ("method_id", "metric_kind", "unit_code", "value", "completeness"),
    [
        ("e1rm_brzycki", "estimated_1rm", "kilogram", "90.009001", "complete"),
        (
            "external_working_set_volume",
            "external_working_set_volume",
            None,
            None,
            "insufficient",
        ),
    ],
)
def test_ephemeral_calculation_returns_closed_result_without_subject_row_writes(
    method_id, metric_kind, unit_code, value, completeness
):
    from unittest.mock import MagicMock

    session = MagicMock()
    source_ref = {"object_type": "workout_session", "object_id": str(uuid4())}
    calculated = {
        "method_id": method_id,
        "method_version": "1",
        "metric_kind": metric_kind,
        "value": {"exact_value": value, "unit_code": unit_code} if value else None,
        "completeness": completeness,
        "input_digest": "1" * 64,
        "result_digest": "2" * 64,
        "applicability_version": f"{method_id}_v1",
        "included_count": 1 if value else 0,
        "excluded_count": 0,
        "normalized_inputs": [{"source_ref": source_ref}],
        "exclusions": [],
        "attestation": None,
    }
    repository = CalculationRepository(session, lambda _session, _payload: calculated)
    operation_id = uuid4()
    repository._reserve = MagicMock(return_value=(operation_id, None))
    repository._finish = MagicMock()
    repository._audit = MagicMock()
    payload = {
        "mode": "ephemeral",
        "method_id": method_id,
        "request_digest": "3" * 64,
    }
    result = repository.create_calculation_draft(
        payload=payload,
        key="idempotency-key-for-ephemeral",
        correlation_id=uuid4(),
    )
    assert CalculationResult.model_validate(result).state == "ephemeral"
    repository._reserve.assert_called_once_with("CMD-17", "idempotency-key-for-ephemeral", "3" * 64)
    repository._finish.assert_called_once_with(operation_id, result)
    repository._audit.assert_called_once()
    assert session.execute.call_count == 0


def test_ephemeral_calculation_replays_the_idempotent_response_without_recalculating():
    from unittest.mock import MagicMock

    session = MagicMock()
    loader = MagicMock(side_effect=AssertionError("a replay must not recalculate"))
    repository = CalculationRepository(session, loader)
    operation_id = uuid4()
    replay = {
        "calculation_id": str(uuid4()),
        "method_id": "external_working_set_volume",
        "state": "ephemeral",
        "source_refs": [{"object_type": "workout_session", "object_id": str(uuid4())}],
    }
    repository._reserve = MagicMock(return_value=(operation_id, replay))
    repository._audit = MagicMock()
    repository._finish = MagicMock()

    result = repository.create_calculation_draft(
        payload={"mode": "ephemeral", "request_digest": "4" * 64},
        key="same-key",
        correlation_id=uuid4(),
    )

    assert result == replay
    loader.assert_not_called()
    repository._finish.assert_not_called()
    repository._audit.assert_called_once()
