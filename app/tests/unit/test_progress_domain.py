from decimal import Decimal

import pytest

from trainer.domain.progress import (
    E1RMAttestation,
    E1RMInput,
    VolumeSet,
    calculate_brzycki_e1rm,
    calculate_external_working_volume,
    classify_calculated_best,
)


def eligible_e1rm(**updates):
    values = {
        "exercise_code": "barbell_bench_press",
        "actual_set_id": "set-1",
        "source_revision_id": "rev-1",
        "set_type": "working",
        "status": "performed",
        "full_reps": 5,
        "partial_reps": 0,
        "failure_reached": True,
        "reported_rir": None,
        "load_kg": Decimal("80"),
        "load_kind": "external",
        "attestation": E1RMAttestation(
            source_actual_set_id="set-1",
            grip_condition_code="owner_confirmed_grip",
            bench_pause_condition="touch_and_go",
            standard_confirmed=True,
            spotter_released_before_rep=True,
            no_assistance_during_rep=True,
        ),
    }
    values.update(updates)
    return E1RMInput(**values)


def test_brzycki_uses_decimal_half_up_and_exact_attestation_source():
    # Approved formula: 80 / (1.0278 - 0.0278 * 5).
    assert calculate_brzycki_e1rm(eligible_e1rm()) == Decimal("90.009001")


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("90", None, "new_current"),
        ("91", "90", "strict_improvement"),
        ("90", "90", "equal_keeps_first"),
        ("89", "90", "not_better"),
    ],
)
def test_calculated_best_retains_first_date_when_values_are_equal(candidate, current, expected):
    assert (
        classify_calculated_best(
            Decimal(candidate), Decimal(current) if current is not None else None
        )
        == expected
    )


def test_explicit_failure_with_zero_rir_is_consistent_but_rir_alone_is_not_failure():
    assert calculate_brzycki_e1rm(eligible_e1rm(reported_rir=0)) == Decimal("90.009001")
    with pytest.raises(ValueError, match="explicit_failure_required"):
        calculate_brzycki_e1rm(eligible_e1rm(reported_rir=0, failure_reached=None))


@pytest.mark.parametrize(("reps", "expected"), [(2, "82.287595"), (10, "106.695119")])
def test_brzycki_approved_repetition_boundaries(reps, expected):
    assert calculate_brzycki_e1rm(eligible_e1rm(full_reps=reps)) == Decimal(expected)


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"full_reps": 1}, "ineligible_repetitions"),
        ({"full_reps": 11}, "ineligible_repetitions"),
        ({"failure_reached": None}, "explicit_failure_required"),
        ({"reported_rir": 1}, "contradictory_effort_report"),
        ({"reported_rir": Decimal("0.5")}, "contradictory_effort_report"),
        ({"failure_reached": False}, "explicit_failure_required"),
        ({"load_kg": Decimal("NaN")}, "non_finite_decimal"),
        ({"load_kg": Decimal("Infinity")}, "non_finite_decimal"),
        ({"set_type": "warmup"}, "ineligible_set_state"),
        ({"status": "interrupted"}, "ineligible_set_state"),
        ({"load_kind": "bodyweight"}, "known_external_load_required"),
        ({"partial_reps": None}, "partial_repetitions_unknown_or_present"),
        ({"attestation": None}, "source_bound_attestation_required"),
        (
            {
                "attestation": E1RMAttestation(
                    "different-set", "grip", "touch_and_go", True, True, True
                )
            },
            "source_bound_attestation_required",
        ),
        (
            {"attestation": E1RMAttestation("set-1", "grip", "touch_and_go", False, True, True)},
            "standard_confirmation_required",
        ),
    ],
)
def test_brzycki_fails_closed(updates, error):
    with pytest.raises(ValueError, match=error):
        calculate_brzycki_e1rm(eligible_e1rm(**updates))


def test_external_volume_is_working_sets_only_and_preserves_partial_status():
    result = calculate_external_working_volume(
        (
            VolumeSet("warm", "warmup", "performed", Decimal("50"), 10, "external"),
            VolumeSet("work", "working", "performed", Decimal("80"), 5, "external"),
            VolumeSet("interrupted", "working", "interrupted", Decimal("60"), 3, "external"),
            VolumeSet("unknown", "working", "performed", None, 4, "unknown"),
        )
    )
    assert result.status == "partial"
    assert result.value_kg_reps == Decimal("580")
    assert result.included_set_ids == ("work", "interrupted")
    assert result.excluded == (
        ("warm", "warmup"),
        ("interrupted", "interrupted_set"),
        ("unknown", "unknown_load"),
    )


@pytest.mark.parametrize(
    ("load_kind", "reason"),
    [
        ("non_external_load", "non_external_load"),
        ("ambiguous_load", "ambiguous_load"),
        ("unknown_item_count", "unknown_item_count"),
        ("unknown", "unknown_load"),
    ],
)
def test_external_volume_preserves_load_exclusion_reason(load_kind, reason):
    result = calculate_external_working_volume(
        (VolumeSet("excluded", "working", "performed", None, 5, load_kind),)
    )
    assert result.status == "insufficient"
    assert result.excluded == (("excluded", reason),)


def test_external_volume_sums_simultaneous_load_but_not_missing_side_as_zero():
    result = calculate_external_working_volume(
        (
            VolumeSet("pair", "working", "performed", Decimal("12.5"), 8, "external", 2),
            VolumeSet(
                "side-missing",
                "working",
                "performed",
                Decimal("10"),
                6,
                "external",
                performed_sides=("left",),
                expected_sides=("left", "right"),
            ),
        )
    )
    assert result.status == "partial"
    assert result.value_kg_reps == Decimal("260")
    assert result.included_set_ids == ("pair", "side-missing")
    assert result.excluded == (("side-missing", "missing_or_unknown_side"),)


def test_external_volume_has_no_durable_result_for_no_eligible_working_sets():
    result = calculate_external_working_volume(
        (VolumeSet("warm", "warmup", "performed", Decimal("20"), 5, "external"),)
    )
    assert result.status == "insufficient"
    assert result.value_kg_reps is None


def test_external_volume_counts_only_full_reps_and_marks_partial_rep_report_incomplete():
    result = calculate_external_working_volume(
        (
            VolumeSet(
                "partial-reps",
                "working",
                "performed",
                Decimal("40"),
                6,
                "external",
                partial_reps=2,
            ),
        )
    )
    assert result.status == "partial"
    assert result.value_kg_reps == Decimal("240")
    assert result.included_set_ids == ("partial-reps",)
    assert result.excluded == (("partial-reps", "partial_repetitions"),)


def test_external_volume_unknown_partial_rep_count_does_not_claim_complete_result():
    result = calculate_external_working_volume(
        (
            VolumeSet(
                "unknown-partials",
                "working",
                "performed",
                Decimal("40"),
                6,
                "external",
                partial_reps=None,
            ),
        )
    )
    assert result.status == "partial"
    assert result.value_kg_reps == Decimal("240")
    assert result.excluded == (("unknown-partials", "partial_repetitions"),)


def test_only_known_left_side_preserves_contribution_and_marks_incomplete_pair():
    result = calculate_external_working_volume(
        (
            VolumeSet(
                "left",
                "working",
                "performed",
                Decimal("10"),
                6,
                "external",
                performed_sides=("left",),
                expected_sides=("left", "right"),
            ),
        )
    )
    assert result.status == "partial"
    assert result.value_kg_reps == Decimal("60")
    assert result.included_set_ids == ("left",)
    assert result.excluded == (("left", "missing_or_unknown_side"),)


def test_interrupted_only_has_numeric_partial_value_and_exact_reason():
    result = calculate_external_working_volume(
        (VolumeSet("interrupted", "working", "interrupted", Decimal("20"), 3, "external"),)
    )
    assert result.status == "partial"
    assert result.value_kg_reps == Decimal("60")
    assert result.excluded == (("interrupted", "interrupted_set"),)
