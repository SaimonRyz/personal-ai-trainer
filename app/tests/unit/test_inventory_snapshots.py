from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.domain.context import DomainValidationError, digest_payload
from trainer.domain.inventory_snapshots import saved_inventory_format
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError
from trainer.transport.http.contracts import PlanLoadView


def snapshot() -> dict[str, Any]:
    return {
        "inventory_snapshot_version": "global_inventory_v2",
        "profile_ref": {
            "object_type": "profile",
            "object_id": str(uuid4()),
            "revision_id": str(uuid4()),
        },
        "goal_refs": [],
        "program_version_ref": None,
        "day_template_ref": None,
        "effective_at": "2026-10-07T00:00:00+00:00",
        "timezone": "UTC",
        "planned_date": "2026-10-08",
        "planned_time": None,
        "temporary_conditions": {
            "place": None,
            "equipment_additions": [],
            "equipment_removals": [],
        },
        "effective_equipment_types": [],
        "equipment_inventory_snapshot": [],
        "equipment_inventory_digest": digest_payload([]),
        "dependencies": [],
    }


def legacy() -> dict[str, Any]:
    result = snapshot()
    for key in (
        "inventory_snapshot_version",
        "equipment_inventory_snapshot",
        "equipment_inventory_digest",
    ):
        del result[key]
    old_inventory = {"place_id": None, "effective_at": result["effective_at"], "items": []}
    result.update(
        {
            "availability_place_id": None,
            "equipment_availability_snapshot": old_inventory,
            "equipment_availability_digest": digest_payload(old_inventory),
        }
    )
    return result


def test_saved_legacy_is_returned_without_inventing_global_facts() -> None:
    value = legacy()
    value["effective_equipment_types"] = ["historical_custom_code"]
    original = deepcopy(value)
    assert saved_inventory_format(value) == "legacy"
    assert value == original
    assert "equipment_inventory_snapshot" not in value


def test_global_empty_inventory_and_explicit_overlay() -> None:
    value = snapshot()
    assert saved_inventory_format(value) == "global_inventory_v2"
    value["temporary_conditions"]["equipment_additions"] = ["barbell"]
    value["effective_equipment_types"] = ["barbell"]
    assert saved_inventory_format(value) == "global_inventory_v2"
    assert value["equipment_inventory_snapshot"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("inventory_snapshot_version", "unknown"),
        ("equipment_inventory_digest", "f" * 64),
        ("availability_place_id", None),
        ("effective_equipment_types", ["barbell"]),
    ],
)
def test_global_saved_corruption_is_fail_closed(field: str, value: object) -> None:
    candidate = snapshot()
    candidate[field] = value
    with pytest.raises(ContextRepositoryError) as error:
        ContextRepository._saved_inventory_format(candidate)
    assert error.value.code == "inconsistent_domain_state"
    assert error.value.status_code == 500


def test_legacy_with_v2_marker_is_not_reclassified_or_rebuilt() -> None:
    candidate = legacy()
    candidate["inventory_snapshot_version"] = "global_inventory_v2"
    with pytest.raises(DomainValidationError, match="mixed or unknown"):
        saved_inventory_format(candidate)


def test_overlay_inventory_only_code_and_incorrect_digest_are_rejected() -> None:
    candidate = snapshot()
    candidate["temporary_conditions"]["equipment_additions"] = ["dumbbell"]
    candidate["effective_equipment_types"] = ["dumbbell"]
    with pytest.raises(DomainValidationError, match="overlay"):
        saved_inventory_format(candidate)
    old = legacy()
    old["equipment_availability_digest"] = "f" * 64
    with pytest.raises(DomainValidationError, match="legacy availability"):
        saved_inventory_format(old)


def test_plan_digest_includes_v2_full_snapshot_but_preserves_legacy_formula() -> None:
    repository = ContextRepository.__new__(ContextRepository)
    tree: dict[str, Any] = {"blocks": [], "planned_date": "2026-10-08"}
    old = legacy()
    assert repository._plan_content_digest(tree, [], old) == digest_payload(
        {"tree": repository._canonical_plan_tree(tree), "recommendation_ids": []}
    )
    current = snapshot()
    first = repository._plan_content_digest(tree, [], current)
    current["temporary_conditions"]["equipment_additions"] = ["barbell"]
    current["effective_equipment_types"] = ["barbell"]
    assert repository._plan_content_digest(tree, [], current) != first


def test_read_only_planned_child_ids_do_not_change_saved_plan_digest() -> None:
    repository = ContextRepository.__new__(ContextRepository)
    base = {
        "blocks": [{
            "position": 1,
            "exercises": [{
                "position": 1,
                "sets": [{
                    "position": 1,
                    "metrics": [],
                    "loads": [{"position": 1, "mass": "30", "item_count": None}],
                }],
            }],
        }],
        "planned_date": "2026-10-08",
    }
    with_ids = deepcopy(base)
    block = with_ids["blocks"][0]
    block["planned_block_id"] = str(uuid4())
    exercise = block["exercises"][0]
    exercise["planned_exercise_id"] = str(uuid4())
    exercise["sets"][0]["planned_set_id"] = str(uuid4())
    exercise["sets"][0]["loads"][0]["planned_load_component_id"] = str(uuid4())

    assert repository._plan_content_digest(base, [], legacy()) == repository._plan_content_digest(
        with_ids, [], legacy()
    )


def test_legacy_plan_decimal_scale_requires_exact_confirmed_source() -> None:
    plan_id, revision_id, confirmation_id = uuid4(), uuid4(), uuid4()
    original = {
        "planned_date": "2026-10-08",
        "blocks": [{
            "position": 1,
            "exercises": [{
                "position": 1,
                "sets": [{
                    "position": 1,
                    "target_rpe_min": "7.0",
                    "pause_seconds": "1.0",
                    "metrics": [{"exact_value": "4.0", "min_value": None, "max_value": None}],
                    "loads": [{"mass": "70.0", "item_count": None}],
                }],
            }],
        }],
    }
    readback = deepcopy(original)
    readback_set = readback["blocks"][0]["exercises"][0]["sets"][0]
    readback_set["target_rpe_min"] = "7.00"
    readback_set["pause_seconds"] = "1.000"
    readback_set["metrics"][0]["exact_value"] = "4.000000"
    readback_set["loads"][0]["mass"] = "70.000000"
    saved_snapshot = legacy()
    session = Mock()
    session.execute.return_value.scalar_one_or_none.return_value = {
        "plan_id": str(plan_id),
        "plan_revision_id": str(revision_id),
        "plan": original,
    }
    repository = ContextRepository(session)
    old_digest = repository._plan_content_digest(original, [], saved_snapshot)
    assert old_digest != repository._plan_content_digest(readback, [], saved_snapshot)

    arguments = {
        "tree": readback,
        "recommendation_ids": [],
        "snapshot": saved_snapshot,
        "stored_digest": old_digest,
        "confirmation_id": confirmation_id,
        "plan_id": plan_id,
        "revision_id": revision_id,
    }
    assert repository._verified_legacy_plan_scale(**arguments)
    changed = deepcopy(readback)
    changed["blocks"][0]["exercises"][0]["sets"][0]["target_rpe_min"] = "8.00"
    assert not repository._verified_legacy_plan_scale(**{**arguments, "tree": changed})
    assert not repository._verified_legacy_plan_scale(
        **{**arguments, "stored_digest": "f" * 64}
    )


def test_plan_load_view_requires_exact_saved_component_id() -> None:
    view = {
        "position": 1,
        "load_kind": "external",
        "equipment_id": None,
        "item_count": None,
        "mass": "30",
        "unit_code": "kilogram",
        "mass_meaning": "total_external_mass",
        "direction": None,
        "raw_ambiguous_text": None,
        "equipment_snapshot": {},
    }
    with pytest.raises(ValidationError):
        PlanLoadView.model_validate(view)
    load_id = uuid4()
    parsed = PlanLoadView.model_validate({**view, "planned_load_component_id": load_id})
    assert parsed.planned_load_component_id == load_id
