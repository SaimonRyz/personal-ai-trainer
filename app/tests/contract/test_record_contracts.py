from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.transport.http.contracts import (
    HistoricalRecordInput,
    RecordConditionView,
    RecordDecisionDraftRequest,
    RecordMetricValue,
    RecordResultItem,
)


def condition(**overrides):
    value = {
        "condition_id": uuid4(),
        "rule_version": "record_v1",
        "scope": "comparable",
        "metric_kind": "record_full_reps",
        "unit_code": "repetition",
        "side": "both",
        "loads": [
            {
                "load_kind": "bodyweight",
                "direction": None,
                "equipment_type": "bodyweight",
                "equipment_id": None,
                "item_count": None,
                "mass_kg": None,
                "mass_meaning": None,
            }
        ],
        "fixed_reps": None,
        "fixed_distance_m": None,
        "unknown_fields": [],
        "normalized_key": "{}",
    }
    value.update(overrides)
    return value


def test_record_decision_keeps_the_approved_closed_wire_shape():
    base = {
        "context": {
            "conversation_id": "synthetic",
            "message_id": "message",
            "effective_at": "2026-09-29T12:00:00Z",
            "timezone": "Asia/Novokuznetsk",
        },
        "request_digest": "0" * 64,
        "candidate_id": uuid4(),
        "decision": "accept",
        "technique_self_confirmation": True,
    }
    accepted = RecordDecisionDraftRequest.model_validate(base)
    assert accepted.decision == "accept"
    with pytest.raises(ValidationError):
        RecordDecisionDraftRequest.model_validate(
            {**base, "full_performance_self_confirmation": True}
        )


def test_closed_condition_rejects_unexpected_key_and_inapplicable_unknown():
    with pytest.raises(ValidationError):
        RecordConditionView.model_validate({**condition(), "free_text": "x"})
    with pytest.raises(ValidationError):
        RecordConditionView.model_validate(condition(unknown_fields=["distance"]))


def test_comparable_and_historical_unknown_condition_views():
    assert RecordConditionView.model_validate(condition()).scope == "comparable"
    unknown = condition(
        scope="historical_unknown",
        side=None,
        unknown_fields=["side"],
        loads=[],
    )
    assert RecordConditionView.model_validate(unknown).scope == "historical_unknown"


def test_external_load_value_is_not_duplicated_in_its_condition_key():
    external = {
        "load_kind": "external",
        "direction": None,
        "equipment_type": "barbell",
        "equipment_id": None,
        "item_count": "1",
        "mass_kg": None,
        "mass_meaning": "total_external_mass",
    }
    view = condition(
        metric_kind="record_external_load",
        unit_code="kilogram",
        fixed_reps=5,
        loads=[external],
    )
    assert RecordConditionView.model_validate(view).metric_kind == "record_external_load"
    historical = HistoricalRecordInput.model_validate(
        {
            "exercise_id": uuid4(),
            "exercise_revision_id": uuid4(),
            "metric_kind": "record_external_load",
            "value": "80",
            "unit_code": "kilogram",
            "achieved_on": date(2026, 9, 1),
            "condition": {
                "side": "not_applicable",
                "loads": [external],
                "fixed_reps": 5,
                "fixed_distance_m": None,
                "unknown_fields": [],
            },
            "body_mass_kg": None,
            "historical_import": True,
            "pre_system_assertion": True,
            "technique_self_confirmation": True,
            "reason": "synthetic history",
            "comment": None,
        }
    )
    assert historical.value == Decimal("80")


def test_historical_external_load_mass_must_match_record_value_when_repeated():
    input_data = {
        "exercise_id": uuid4(),
        "exercise_revision_id": uuid4(),
        "metric_kind": "record_external_load",
        "value": "80",
        "unit_code": "kilogram",
        "achieved_on": date(2026, 9, 1),
        "condition": {
            "side": "not_applicable",
            "loads": [
                {
                    "load_kind": "external",
                    "direction": None,
                    "equipment_type": "barbell",
                    "equipment_id": None,
                    "item_count": "1",
                    "mass_kg": "75",
                    "mass_meaning": "total_external_mass",
                }
            ],
            "fixed_reps": 5,
            "fixed_distance_m": None,
            "unknown_fields": [],
        },
        "body_mass_kg": None,
        "historical_import": True,
        "pre_system_assertion": True,
        "technique_self_confirmation": True,
        "reason": "synthetic history",
        "comment": None,
    }
    with pytest.raises(ValidationError):
        HistoricalRecordInput.model_validate(input_data)


def test_record_result_requires_complete_workout_refs_and_exit_event():
    base = {
        "record_id": uuid4(),
        "exercise_ref": {"object_type": "exercise", "object_id": uuid4(), "revision_id": uuid4()},
        "metric_kind": "record_full_reps",
        "value": RecordMetricValue(exact_value=Decimal("8"), unit_code="repetition"),
        "achieved_on": date(2026, 9, 29),
        "condition": condition(condition_id=uuid4()),
        "is_current": False,
        "is_historical_import": True,
        "validity": "valid",
        "source_session_ref": None,
        "source_set_ref": None,
        "source_candidate_ref": None,
        "confirmation_ref": {
            "object_type": "confirmation",
            "object_id": uuid4(),
            "revision_id": None,
        },
        "supersedes_record_ref": None,
        "body_mass_kg": None,
        "current_exit_reason": "historical_unknown",
        "current_exit_change_id": uuid4(),
        "comment": None,
    }
    assert RecordResultItem.model_validate(base).is_historical_import
    with pytest.raises(ValidationError):
        RecordResultItem.model_validate({**base, "current_exit_change_id": None})
    with pytest.raises(ValidationError):
        RecordResultItem.model_validate(
            {
                **base,
                "source_candidate_ref": {
                    "object_type": "record_candidate",
                    "object_id": uuid4(),
                    "revision_id": None,
                },
            }
        )
