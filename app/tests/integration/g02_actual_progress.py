"""G02-13: three separately confirmed actual records, not calculated e1RM points."""

from __future__ import annotations

import os
from copy import deepcopy
from decimal import Decimal

import progress_flow as flow
import record_flow as records
from sqlalchemy import text


def main() -> None:
    exercise_id = os.environ["TRAINER_E1RM_BENCH_EXERCISE_ID"]
    equipment_id = flow._barbell_equipment_id()
    expected: dict[str, tuple[str, Decimal]] = {}
    sessions = []
    condition_ids = set()
    for index, (on, mass) in enumerate(
        (("2026-01-10", "60"), ("2026-01-24", "62.5"), ("2026-02-10", "65")), 1
    ):
        report = {
            "plan_id": None,
            "plan_revision_id": None,
            "started_at": None,
            "ended_at": None,
            "local_start_date": on,
            "duration_seconds": 60,
            "session_type": "training",
            "name": f"Synthetic G02 actual progress {index}",
            "lifecycle": "completed",
            "effort": None,
            "comment": None,
            "workout_metrics": [],
            "confirmed_unknown_planned_exercise_ids": [],
            "confirmed_unknown_planned_set_ids": [],
            "confirmed_unknown_planned_load_component_ids": [],
            "confirmed_unknown_metrics": [],
            "blocks": [
                {
                    "position": 1,
                    "block_type_code": "straight",
                    "exercises": [
                        {
                            "position": 1,
                            "exercise_id": exercise_id,
                            "result": "added",
                            "sets": [
                                {
                                    "position": 1,
                                    "logical_set_no": 1,
                                    "set_type_code": "working",
                                    "status": "performed",
                                    "side": "not_applicable",
                                    "full_reps": 8,
                                    "metrics": [],
                                    "loads": [
                                        {
                                            "position": 1,
                                            "load_kind": "external",
                                            "equipment_id": equipment_id,
                                            "item_count": "1",
                                            "mass": mass,
                                            "unit_code": "kilogram",
                                            "mass_meaning": "total_external_mass",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        session = flow._create_session(report, f"g02-actual-progress-{index}")
        sessions.append((session, deepcopy(report)))
        candidates = records.post(
            "/api/v1/queries/record-candidates",
            {
                "context": records.context(f"g02-progress-candidate-{index}"),
                "exercise_id": exercise_id,
                "metric_kind": "record_external_load",
                "status": "pending",
                "limit": 100,
            },
        )["data"]["items"]
        candidate = next(
            item
            for item in candidates
            if item["source_session_ref"]["revision_id"] == session["revision_id"]
        )
        condition_ids.add(candidate["condition"]["condition_id"])
        draft = records.post(
            "/api/v1/commands/record-decision-drafts",
            {
                "context": records.context(f"g02-progress-accept-{index}"),
                "candidate_id": candidate["candidate_id"],
                "decision": "accept",
                "technique_self_confirmation": True,
            },
            f"g02-progress-accept-{index}",
        )["data"]
        result = records.confirm_draft(
            draft, f"g02-progress-record-{index}", "Confirm synthetic record"
        )
        record_id = records.created_record_id(result)
        expected[record_id] = (on, Decimal(mass))
        with records.ENGINE.connect() as db:
            links = db.execute(
                text("""
                    SELECT count(*) FROM trainer.actual_records r
                    JOIN trainer.confirmations c USING(confirmation_id)
                    JOIN trainer.domain_changes d ON d.object_type='actual_record'
                      AND d.object_id=r.record_id AND d.operation='create'
                    WHERE r.record_id=:id AND NOT r.is_historical_import
                      AND r.session_revision_id=:revision AND r.actual_set_id IS NOT NULL
                      AND r.candidate_id=:candidate
                """),
                {
                    "id": record_id,
                    "revision": session["revision_id"],
                    "candidate": candidate["candidate_id"],
                },
            ).scalar_one()
        assert links == 1
    assert len(condition_ids) == 1
    condition = next(iter(condition_ids))
    data = records.post(
        "/api/v1/queries/progress",
        {
            "context": records.context("g02-actual-progress-query"),
            "metric_kind": "record_external_load",
            "date_from": "2026-01-01",
            "date_to": "2026-02-28",
            "exercise_id": exercise_id,
            "condition_id": condition,
            "include_history": True,
            "limit": 100,
        },
    )["data"]
    assert not data["has_more"] and data["next_cursor"] is None
    points = {item["point"]["record_ref"]["object_id"]: item["point"] for item in data["items"]}
    assert set(points) == set(expected)
    for record_id, (on, value) in expected.items():
        point = points[record_id]
        assert point["achieved_on"] == on
        assert Decimal(point["value"]["exact_value"]) == value
        assert point["session_ref"] and point["actual_set_ref"]
        assert point["condition_id"] == condition
    assert sum(item["is_current"] for item in points.values()) == 1
    assert data["comparison_status"] == "descriptive_direction", data["comparison_status"]
    print("PASS: G02-13 three actual accepted records, exact sources/history and QRY-06 direction")

    def query(**overrides):
        return records.post(
            "/api/v1/queries/progress",
            {
                "context": records.context("g02-progress-negative-query"),
                "metric_kind": "record_external_load",
                "date_from": "2026-01-01",
                "date_to": "2026-02-28",
                "exercise_id": exercise_id,
                "condition_id": condition,
                "include_history": True,
                "limit": 100,
                **overrides,
            },
        )["data"]

    current_only = query(include_history=False)
    assert len(current_only["items"]) == 1
    assert current_only["items"][0]["point"]["is_current"]
    assert current_only["comparison_status"] == "one_point"
    first_session, corrected_report = sessions[0]
    corrected_report["blocks"][0]["exercises"][0]["sets"][0]["loads"][0]["mass"] = "50"
    correction = records.correction_draft(
        {"object_id": first_session["session_id"], "revision_id": first_session["revision_id"]},
        corrected_report,
        "g02-progress-source-correction",
    )
    records.confirm_draft(correction, "g02-progress-source-correction", "Correct synthetic source")
    after_correction = query()
    assert len(after_correction["items"]) == 3  # Invalidated source stays in history.
    assert after_correction["comparison_status"] == "two_points"
    history = records.record_items(exercise_id, "record_external_load", condition_id=condition)
    invalid_id = next(record_id for record_id, (on, _) in expected.items() if on == "2026-01-10")
    assert (
        next(item for item in history if item["record_id"] == invalid_id)["validity"]
        == "source_invalidated"
    )

    historical = records.historical_input(
        {
            "exercise_id": exercise_id,
            "exercise_revision_id": candidate["exercise_ref"]["revision_id"],
            "metric_kind": "record_external_load",
            "unit_code": "kilogram",
        },
        candidate["condition"],
        Decimal(70),
        "Synthetic historical progress boundary",
    )

    def commit_historical(value, prefix, **command_fields):
        draft = records.post(
            "/api/v1/commands/historical-record-drafts",
            {
                "context": records.context(prefix),
                "intent": "create",
                "historical": value,
                **command_fields,
            },
            prefix + "-key",
        )["data"]
        return records.confirm_draft(draft, prefix, "Confirm synthetic history")

    unknown = deepcopy(historical)
    unknown["achieved_on"] = "2026-01-15"
    unknown["condition"].update(side=None, unknown_fields=["side"])
    unknown_id = records.created_record_id(commit_historical(unknown, "g02-progress-unknown"))
    with_unknown = query(condition_id=None)
    assert any(
        item["point"]["record_ref"]["object_id"] == unknown_id for item in with_unknown["items"]
    )
    assert with_unknown["comparison_status"] == "two_points"

    historical["achieved_on"] = "2026-01-01"
    replaced_id = records.created_record_id(
        commit_historical(historical, "g02-progress-history-create")
    )
    replacement = deepcopy(historical)
    replacement.update(value="75", achieved_on="2026-01-02")
    commit_historical(
        replacement,
        "g02-progress-history-correct",
        intent="correct",
        target_record_id=replaced_id,
        expected_current=True,
    )
    replaced_only = query(date_from="2026-01-01", date_to="2026-01-01")
    assert len(replaced_only["items"]) == 1
    assert replaced_only["items"][0]["point"]["record_ref"]["object_id"] == replaced_id
    assert replaced_only["comparison_status"] == "insufficient"
    print(
        "PASS: QRY-06 excludes invalidated/corrected/unknown from direction, "
        "preserves history/current filter"
    )


if __name__ == "__main__":
    main()
