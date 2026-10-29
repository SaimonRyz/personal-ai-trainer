from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from trainer.transport.http.contracts import PlanTreeInput, WorkoutReportInput

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/g02/g02_synthetic_pack.json"


def _plan_tree_for_current_dto(value: dict[str, Any]) -> dict[str, Any]:
    """Map fixture's conceptual labels to the current DTO field names only."""
    plan = deepcopy(value)
    for block in plan["blocks"]:
        block["block_type_code"] = block.pop("block_type")
        for exercise in block["exercises"]:
            for planned_set in exercise["sets"]:
                planned_set["set_type_code"] = planned_set.pop("set_type")
    return plan


def test_g02_core_trees_match_current_plan_and_workout_dtos() -> None:
    pack = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert pack["classification"] == "synthetic_test_data_only"
    assert "not a complete HTTP request" in pack["notice"]

    plan = PlanTreeInput.model_validate(_plan_tree_for_current_dto(pack["plan"]["input"]))
    report = WorkoutReportInput.model_validate(pack["workout_report"]["input"])

    assert len(plan.blocks) == 8
    assert {block.block_type_code for block in plan.blocks} >= {"straight", "superset", "circuit"}
    assert len(report.blocks) == 9

    planned_cardio = plan.blocks[5].exercises[0].sets[0]
    actual_cardio = report.blocks[5].exercises[0].sets[0]
    assert {metric.metric_kind: metric.exact_value for metric in planned_cardio.metrics} == {
        "duration": 600,
        "distance": 2000,
    }
    assert {metric.metric_kind: metric.value for metric in actual_cardio.metrics} == {
        "duration": 600,
        "distance": 2000,
    }


def test_g02_fixture_is_not_treated_as_api_ids_or_seed() -> None:
    pack = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert pack["exercise_refs"]["treadmill"]["object_id"].startswith("02000000-")
    assert pack["exercise_refs"]["treadmill"]["revision_id"].startswith("02000000-")
    assert "not a complete HTTP request" in pack["notice"]
    assert pack["saved_state_expectations"]["test_only_identity_map"][
        "materialization_rule"
    ].startswith("These are deterministic test-only identities")
