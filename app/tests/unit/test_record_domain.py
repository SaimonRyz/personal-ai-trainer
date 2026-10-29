from decimal import Decimal

import pytest

from trainer.domain.records import (
    RecordFact,
    canonical_condition_key,
    choose_training_winner,
    strictly_improves,
    validate_record_fact,
)
from trainer.infrastructure.db.workouts import WorkoutRepository


@pytest.mark.parametrize(
    ("kind", "candidate", "current", "expected"),
    [
        ("record_full_reps", "12", "11", True),
        ("record_full_reps", "12", "12", False),
        ("record_external_load", "82.5", "80", True),
        ("record_hold_duration", "30", "31", False),
        ("record_cardio_time", "120", "121", True),
        ("record_cardio_time", "120", "120", False),
    ],
)
def test_strict_improvement_uses_metric_direction(kind, candidate, current, expected):
    assert strictly_improves(kind, Decimal(candidate), Decimal(current)) is expected


def test_first_record_improves_and_unknown_metric_does_not():
    assert strictly_improves("record_full_reps", Decimal("1"), None)
    assert not strictly_improves("one_rep_max", Decimal("100"), None)


@pytest.mark.parametrize(
    "metric_kind",
    [
        "record_full_reps",
        "record_external_load",
        "record_hold_duration",
        "record_cardio_time",
    ],
)
def test_equal_value_is_not_an_improvement_for_any_record_metric(metric_kind):
    assert not strictly_improves(metric_kind, Decimal("10"), Decimal("10"))


def test_condition_key_is_stable_for_mapping_order():
    first = {"metric_kind": "record_full_reps", "side": "both", "load_signature": []}
    second = {"load_signature": [], "side": "both", "metric_kind": "record_full_reps"}
    assert canonical_condition_key(first) == canonical_condition_key(second)


def test_condition_key_canonicalizes_decimal_text_as_numbers():
    first = {
        "metric_kind": "record_external_load",
        "load_signature": [{"mass_kg": "15.000000", "item_count": "2.0"}],
    }
    second = {
        "load_signature": [{"item_count": "2", "mass_kg": "15"}],
        "metric_kind": "record_external_load",
    }
    assert canonical_condition_key(first) == canonical_condition_key(second)
    assert '"mass_kg":15' in canonical_condition_key(first)


def test_condition_key_distinguishes_load_mode_meaning_and_side():
    baseline = {
        "metric_kind": "record_external_load",
        "unit_code": "kilogram",
        "side": "not_applicable",
        "load_signature": [
            {
                "load_kind": "external",
                "equipment_type": "barbell",
                "item_count": "1",
                "mass_kg": None,
                "mass_meaning": "total_external_mass",
                "direction": None,
            }
        ],
        "fixed_reps": 8,
        "fixed_distance_m": None,
        "unknown_fields": [],
        "rule_version": "record_v1",
    }
    variants = [
        {**baseline, "side": "left"},
        {
            **baseline,
            "load_signature": [{**baseline["load_signature"][0], "load_kind": "bodyweight"}],
        },
        {
            **baseline,
            "load_signature": [{**baseline["load_signature"][0], "mass_meaning": "per_item_mass"}],
        },
        {
            **baseline,
            "load_signature": [{**baseline["load_signature"][0], "direction": "assistance"}],
        },
        {**baseline, "unknown_fields": ["side"]},
    ]
    baseline_key = canonical_condition_key(baseline)
    assert all(canonical_condition_key(variant) != baseline_key for variant in variants)


def test_training_winner_keeps_first_on_equal_value():
    items = [{"value": "8", "id": "first"}, {"value": "8", "id": "second"}]
    assert choose_training_winner(items, "record_full_reps") == items[0]


def test_workout_fingerprint_planner_accepts_unmaterialized_corrected_set_ids():
    repository = WorkoutRepository.__new__(WorkoutRepository)
    tree = {
        "local_start_date": "2026-09-29",
        "blocks": [
            {
                "position": 1,
                "exercises": [
                    {
                        "exercise_id": "11111111-1111-4111-8111-111111111111",
                        "exercise_revision_id": "22222222-2222-4222-8222-222222222222",
                        "position": 1,
                        "sets": [
                            {
                                "position": 1,
                                "status": "performed",
                                "set_type": "working",
                                "full_reps": 5,
                                "partial_reps": None,
                                "side": "both",
                                "metrics": [],
                                "loads": [],
                                "rom_state": None,
                                "technique_state": None,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    first = repository._record_set_fingerprints(tree)
    second = repository._record_set_fingerprints(tree)
    assert first == second
    assert len(next(iter(first.values()))) == 1


def test_record_fact_rejects_unknown_metric_unit_and_bad_scope():
    with pytest.raises(ValueError, match="unknown_metric_or_unit"):
        validate_record_fact(
            RecordFact("record_full_reps", Decimal("8"), "kilogram", {"scope": "comparable"})
        )
    with pytest.raises(ValueError, match="cannot have unknown"):
        validate_record_fact(
            RecordFact(
                "record_full_reps",
                Decimal("8"),
                "repetition",
                {"scope": "comparable", "unknown_fields": ["side"]},
            )
        )
