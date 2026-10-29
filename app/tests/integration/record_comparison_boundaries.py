"""Persisted G-03 record equality and comparison-boundary probes.

Called only inside the isolated synthetic workout flow, after four candidates
have been separately accepted and before historical imports change their groups.
All facts are created through CMD-06 and the normal preview/shown/confirm path.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from sqlalchemy import text


def run(report: dict[str, Any], selected: list[Any], candidates: list[Any]) -> None:
    import record_flow as records
    import workout_flow as workouts

    def source(candidate: Any) -> dict[str, Any]:
        exercise = report["blocks"][candidate["block_position"] - 1]["exercises"][
            candidate["exercise_position"] - 1
        ]
        actual_set = copy.deepcopy(exercise["sets"][candidate["set_position"] - 1])
        actual_set.update(position=1, logical_set_no=1, round_no=None, planned_set_id=None)
        for load in actual_set["loads"]:
            load["planned_load_component_id"] = None
        return {
            "position": 1,
            "exercise_id": str(candidate["exercise_id"]),
            "planned_exercise_id": None,
            "result": "added",
            "replacement_source": None,
            "reason": "synthetic G-03 comparison boundary",
            "sets": [actual_set],
        }

    def commit(
        exercise: dict[str, Any], prefix: str, *, reject_unknown_side: bool = False
    ) -> tuple[str, list[Any]]:
        probe = {
            "plan_id": None,
            "plan_revision_id": None,
            "started_at": None,
            "ended_at": None,
            "local_start_date": report["local_start_date"],
            "duration_seconds": None,
            "session_type": "training",
            "name": prefix,
            "lifecycle": "completed",
            "effort": None,
            "comment": "Synthetic isolated G-03 record probe",
            "workout_metrics": [],
            "confirmed_unknown_planned_exercise_ids": [],
            "confirmed_unknown_planned_set_ids": [],
            "confirmed_unknown_planned_load_component_ids": [],
            "confirmed_unknown_metrics": [],
            "blocks": [
                {
                    "position": 1,
                    "planned_block_id": None,
                    "block_type_code": "straight",
                    "declared_rounds": None,
                    "rest_seconds": None,
                    "comment": None,
                    "exercises": [exercise],
                }
            ],
        }
        if reject_unknown_side:
            with records.ENGINE.connect() as db:
                before_counts = db.execute(
                    text(
                        "SELECT (SELECT count(*) FROM trainer.workout_sessions), "
                        "(SELECT count(*) FROM trainer.record_candidates)"
                    )
                ).one()
            error = workouts.post(
                "/api/v1/commands/workout-report-drafts",
                {"context": workouts.context(prefix + "-draft"), "report": probe},
                prefix + "-draft-key",
                expected=422,
            )
            assert error["error"]["code"] == "validation_error"
            with records.ENGINE.connect() as db:
                assert (
                    db.execute(
                        text(
                            "SELECT (SELECT count(*) FROM trainer.workout_sessions), "
                            "(SELECT count(*) FROM trainer.record_candidates)"
                        )
                    ).one()
                    == before_counts
                )
            return "", []
        draft = workouts.post(
            "/api/v1/commands/workout-report-drafts",
            {"context": workouts.context(prefix + "-draft"), "report": probe},
            prefix + "-draft-key",
        )["data"]
        assert not draft["gaps"], (prefix, draft["gaps"])
        result = workouts.confirm(draft, prefix)
        assert result["status"] == "committed"
        revision = next(
            item["revision_id"]
            for item in result["created_revisions"]
            if item["object_type"] == "workout_session"
        )
        with records.ENGINE.connect() as db:
            # A no-candidate result must still have persisted its complete fact.
            assert (
                db.execute(
                    text("""
                    SELECT count(*) FROM trainer.actual_sets s
                    JOIN trainer.exercise_executions e USING(execution_id)
                    JOIN trainer.actual_blocks b USING(actual_block_id)
                    WHERE b.session_revision_id=:revision AND s.status='performed'
                """),
                    {"revision": revision},
                ).scalar_one()
                == 1
            )
            rows = list(
                db.execute(
                    text("""
                        SELECT c.*, cc.readable_snapshot FROM trainer.record_candidates c
                        JOIN trainer.comparison_conditions cc USING(condition_id)
                        WHERE c.session_revision_id=:revision
                    """),
                    {"revision": revision},
                ).mappings()
            )
        return revision, rows

    def current(candidate: Any) -> list[dict[str, Any]]:
        return records.record_items(
            str(candidate["exercise_id"]),
            candidate["metric_kind"],
            "current",
            str(candidate["condition_id"]),
        )

    def assert_separate(candidate: Any, exercise: dict[str, Any], prefix: str) -> Any:
        before = current(candidate)
        assert before and any(
            row["source_candidate_ref"]
            and row["source_candidate_ref"]["object_id"] == str(candidate["candidate_id"])
            for row in before
        )
        _, rows = commit(exercise, prefix)
        matched = [row for row in rows if row["metric_kind"] == candidate["metric_kind"]]
        assert len(matched) == 1, (prefix, rows)
        row = matched[0]
        assert str(row["condition_id"]) != str(candidate["condition_id"]), (prefix, row)
        assert Decimal(str(row["value"])) == Decimal(str(candidate["value"]))
        assert current(candidate) == before, prefix
        return row

    # Each exact equal result persists as a workout fact, but creates no new
    # candidate of that metric, even under a spuriously different comparison
    # group, and never replaces its accepted current record.
    for index, candidate in enumerate(selected, 1):
        before = current(candidate)
        assert len(before) == 1
        _, rows = commit(source(candidate), f"g03-equality-{index}")
        assert not any(row["metric_kind"] == candidate["metric_kind"] for row in rows), (
            candidate,
            rows,
        )
        assert current(candidate) == before
    print(
        "PASS: G-03 exact equality persists fact without candidate/current replacement (4 metrics)"
    )

    # Side is a comparison boundary for all four metrics, not a numeric value.
    for index, candidate in enumerate(selected, 1):
        side_conditions = []
        baseline_side = candidate["condition_snapshot"]["side"]
        other_sides = [side for side in ("left", "right") if side != baseline_side]
        for side in other_sides:
            exercise = source(candidate)
            exercise["sets"][0]["side"] = side
            row = assert_separate(candidate, exercise, f"g03-side-{index}-{side}")
            assert row["readable_snapshot"]["side"] == side
            side_conditions.append(str(row["condition_id"]))
        assert len(set(side_conditions)) == len(other_sides)

    external = next(row for row in selected if row["metric_kind"] == "record_external_load")
    external_source = source(external)
    external_load = next(
        load for load in external_source["sets"][0]["loads"] if load["load_kind"] == "external"
    )
    assert external_load["mass_meaning"] == "total_external_mass"
    external_load["mass_meaning"] = "per_item_mass"
    different_meaning = assert_separate(external, external_source, "g03-mass-meaning")
    assert (
        next(
            load
            for load in different_meaning["readable_snapshot"]["load_signature"]
            if load["load_kind"] == "external"
        )["mass_meaning"]
        == "per_item_mass"
    )

    different_reps = source(external)
    different_reps["sets"][0]["full_reps"] += 1
    for metric in different_reps["sets"][0]["metrics"]:
        if metric["metric_kind"] == "repetitions":
            metric["value"] = str(different_reps["sets"][0]["full_reps"])
    reps_row = assert_separate(external, different_reps, "g03-external-fixed-reps")
    assert reps_row["readable_snapshot"]["fixed_reps"] == different_reps["sets"][0]["full_reps"]

    cardio = next(row for row in selected if row["metric_kind"] == "record_cardio_time")
    different_distance = source(cardio)
    distance = next(
        metric
        for metric in different_distance["sets"][0]["metrics"]
        if metric["metric_kind"] == "distance"
    )
    distance["value"] = str(Decimal(str(distance["value"])) + 1)
    distance_row = assert_separate(cardio, different_distance, "g03-cardio-fixed-distance")
    assert Decimal(str(distance_row["readable_snapshot"]["fixed_distance_m"])) == Decimal(
        distance["value"]
    )

    # Change only load mode while retaining the accepted metric value. If modes
    # were mixed, the equal result would be suppressed instead of a new group.
    for index, candidate in enumerate(selected, 1):
        if candidate["metric_kind"] == "record_external_load":
            continue  # This metric requires an external component by definition.
        exercise = source(candidate)
        actual_set = exercise["sets"][0]
        old_kinds = {load["load_kind"] for load in actual_set["loads"]}
        if old_kinds == {"bodyweight"}:
            known_external = copy.deepcopy(external_load)
            known_external.update(position=1, mass_meaning="total_external_mass")
            actual_set["loads"] = [known_external]
            expected_kind = "external"
        else:
            actual_set["loads"] = [{"position": 1, "load_kind": "bodyweight"}]
            expected_kind = "bodyweight"
        row = assert_separate(candidate, exercise, f"g03-load-mode-{index}")
        assert {load["load_kind"] for load in row["readable_snapshot"]["load_signature"]} == {
            expected_kind
        }

    with records.ENGINE.connect() as db:
        band_id = str(
            db.execute(
                text("""
            SELECT i.equipment_id FROM trainer.equipment_items i
            JOIN trainer.equipment_item_revisions r
              ON r.equipment_revision_id=i.current_revision_id
            WHERE r.equipment_type='resistance_band'
              AND i.archived_at IS NULL AND i.logically_deleted_at IS NULL
            ORDER BY i.created_at,i.equipment_id LIMIT 1
        """)
            ).scalar_one()
        )
    band_template = {"position": 2, "load_kind": "band", "equipment_id": band_id}
    reps = next(row for row in selected if row["metric_kind"] == "record_full_reps")
    band_conditions = []
    for direction in ("assistance", "resistance"):
        exercise = source(reps)
        band = copy.deepcopy(band_template)
        band.update(position=2, planned_load_component_id=None, direction=direction)
        exercise["sets"][0]["loads"] = [{"position": 1, "load_kind": "bodyweight"}, band]
        row = assert_separate(reps, exercise, "g03-band-" + direction)
        assert (
            next(
                item
                for item in row["readable_snapshot"]["load_signature"]
                if item["load_kind"] == "band"
            )["direction"]
            == direction
        )
        band_conditions.append(str(row["condition_id"]))
    assert len(set(band_conditions)) == 2

    # Unknown load conditions must not become a zero-load comparison group.
    # Use strictly better known repetitions/duration so absence of a candidate
    # cannot be explained by equality to the existing record.
    for index, candidate in enumerate(selected, 1):
        exercise = source(candidate)
        actual_set = exercise["sets"][0]
        if candidate["metric_kind"] == "record_full_reps":
            actual_set["full_reps"] += 1
            for metric in actual_set["metrics"]:
                if metric["metric_kind"] == "repetitions":
                    metric["value"] = str(actual_set["full_reps"])
        elif candidate["metric_kind"] in {"record_hold_duration", "record_cardio_time"}:
            duration = next(m for m in actual_set["metrics"] if m["metric_kind"] == "duration")
            delta = -1 if candidate["metric_kind"] == "record_cardio_time" else 1
            duration["value"] = str(Decimal(str(duration["value"])) + delta)
        actual_set["loads"] = [
            {
                "position": 1,
                "load_kind": "external",
                "raw_ambiguous_text": "Synthetic explicitly unknown external load",
            }
        ]
        before = current(candidate)
        _, rows = commit(exercise, f"g03-unknown-load-{index}")
        assert not rows, (candidate, rows)
        assert current(candidate) == before

    unilateral = next(
        row for row in candidates if row["condition_snapshot"]["side"] in {"left", "right"}
    )
    exercise = source(unilateral)
    exercise["sets"][0]["side"] = None
    _, rows = commit(exercise, "g03-unknown-unilateral-side", reject_unknown_side=True)
    assert not rows, rows
    print("PASS: G-03 persisted side/load-mode/mass-meaning/unknown comparison boundaries")
