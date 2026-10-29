from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from trainer.domain.context import DomainValidationError
from trainer.domain.plan import validate_plan_draft


def valid_payload() -> dict[str, object]:
    return {
        "plan": {
            "planned_date": "2026-09-24",
            "planned_time": "18:30:00",
            "program_id": str(uuid4()),
            "program_version_id": str(uuid4()),
            "day_template_id": None,
            "plan_type": "training",
            "name": "Synthetic plan",
            "temporary_conditions": {
                "place": None,
                "equipment_additions": [],
                "equipment_removals": [],
            },
            "blocks": [
                {
                    "position": 1,
                    "block_type_code": "straight",
                    "name": None,
                    "declared_rounds": None,
                    "rest_seconds": 90,
                    "comment": None,
                    "exercises": [
                        {
                            "position": 1,
                            "exercise_id": str(uuid4()),
                            "circuit_position": None,
                            "equipment_snapshot": {},
                            "comment": None,
                            "sets": [
                                {
                                    "position": 1,
                                    "logical_set_no": 1,
                                    "round_no": None,
                                    "side": "not_applicable",
                                    "set_type_code": "working",
                                    "metrics": [
                                        {
                                            "metric_kind": "repetitions",
                                            "exact_value": "8",
                                            "min_value": None,
                                            "max_value": None,
                                            "unit_code": "repetition",
                                        }
                                    ],
                                    "loads": [],
                                    "target_rpe_min": "6",
                                    "target_rpe_max": "8",
                                    "target_rir_min": None,
                                    "target_rir_max": None,
                                    "tempo": None,
                                    "pause_seconds": None,
                                    "rest_after_seconds": 90,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "recommendations": [],
    }


def test_complete_plan_tree_is_valid() -> None:
    validate_plan_draft(valid_payload())


@pytest.mark.parametrize("field", ["program_id", "program_version_id"])
def test_program_references_must_be_complete_pairs(field: str) -> None:
    payload = valid_payload()
    payload["plan"][field] = None  # type: ignore[index]
    with pytest.raises(DomainValidationError, match="supplied together"):
        validate_plan_draft(payload)


def test_program_may_be_used_without_a_source_day_template() -> None:
    validate_plan_draft(valid_payload())


def test_unlinked_plan_is_allowed() -> None:
    payload = valid_payload()
    plan = payload["plan"]
    plan["program_id"] = None
    plan["program_version_id"] = None
    validate_plan_draft(payload)


def test_duplicate_sibling_positions_are_rejected() -> None:
    payload = valid_payload()
    plan = payload["plan"]
    block = plan["blocks"][0]
    block["exercises"].append(deepcopy(block["exercises"][0]))
    with pytest.raises(DomainValidationError, match="positions"):
        validate_plan_draft(payload)


def test_metric_requires_exact_or_complete_range() -> None:
    payload = valid_payload()
    metric = payload["plan"]["blocks"][0]["exercises"][0]["sets"][0]["metrics"][0]
    metric["exact_value"] = None
    metric["min_value"] = "5"
    with pytest.raises(DomainValidationError, match="complete range"):
        validate_plan_draft(payload)


def test_metric_range_must_be_ordered_and_can_exceed_ten() -> None:
    payload = valid_payload()
    metric = payload["plan"]["blocks"][0]["exercises"][0]["sets"][0]["metrics"][0]
    metric.update(exact_value=None, min_value="8", max_value="12")
    validate_plan_draft(payload)
    metric.update(min_value="13", max_value="12")
    with pytest.raises(DomainValidationError, match="minimum cannot exceed"):
        validate_plan_draft(payload)


def test_rpe_and_rir_are_bounded_to_ten() -> None:
    payload = valid_payload()
    planned_set = payload["plan"]["blocks"][0]["exercises"][0]["sets"][0]
    planned_set["target_rpe_max"] = "11"
    with pytest.raises(DomainValidationError, match="between 0 and 10"):
        validate_plan_draft(payload)


def test_load_numeric_and_ambiguous_forms_cannot_coexist() -> None:
    payload = valid_payload()
    planned_set = payload["plan"]["blocks"][0]["exercises"][0]["sets"][0]
    planned_set["loads"] = [
        {
            "position": 1,
            "load_kind": "external",
            "equipment_id": None,
            "equipment_snapshot": {},
            "item_count": None,
            "mass": "20",
            "unit_code": "kilogram",
            "mass_meaning": "total_external_mass",
            "direction": None,
            "raw_ambiguous_text": "maybe 20 kg",
        }
    ]
    with pytest.raises(DomainValidationError, match="ambiguous"):
        validate_plan_draft(payload)


def test_saved_place_requires_exact_revision() -> None:
    payload = valid_payload()
    payload["plan"]["temporary_conditions"]["place"] = {
        "kind": "saved",
        "place_ref": {
            "object_type": "training_place",
            "object_id": str(uuid4()),
            "revision_id": None,
        },
    }
    with pytest.raises(DomainValidationError, match="revision_id"):
        validate_plan_draft(payload)
