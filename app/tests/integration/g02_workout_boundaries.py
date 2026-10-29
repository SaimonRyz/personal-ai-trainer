"""Synthetic category-level G02-03/04/05/07/10/11 persisted API checks.

Run after the normal workout flow. IDs come from confirmed synthetic fixtures;
all new domain writes use the public draft/preview/shown/confirm API.
The JSON pack's barbell assembly is test-only metadata, not a request field.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from evidence_fixture import insufficient_recommendations, synthetic_plan
from progress_flow import _template
from sqlalchemy import text
from workout_flow import ENGINE, confirm, context, post


def _set(position: int, reps: int, loads: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {
        "position": position,
        "logical_set_no": position,
        "set_type_code": "working",
        "status": "performed",
        "full_reps": reps,
        "metrics": [],
        "loads": loads,
        **fields,
    }


def _external(mass: str, *, equipment_id: str | None = None, per_item: bool = False):
    return {
        "position": 1,
        "load_kind": "external",
        "equipment_id": equipment_id,
        "item_count": 2 if per_item else 1,
        "mass": mass,
        "unit_code": "kilogram",
        "mass_meaning": "per_item_mass" if per_item else "total_external_mass",
    }


def _added(position: int, exercise_id: str, sets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "position": position,
        "exercise_id": exercise_id,
        "result": "added",
        "reason": "independent synthetic extra exercise; not a replacement",
        "sets": sets,
    }


def _history(ref: dict[str, str]) -> dict[str, Any]:
    cursor = None
    seen = set()
    while True:
        page = post(
            "/api/v1/queries/workouts/history",
            {
                "context": context("g02-boundary-history"),
                "date_from": "2026-09-30",
                "date_to": "2026-09-30",
                "limit": 100,
                "cursor": cursor,
            },
        )["data"]
        for item in page["items"]:
            if item["session_ref"]["revision_id"] == ref["revision_id"]:
                return item["workout"]
        cursor = page["next_cursor"]
        assert cursor is not None and cursor not in seen, "confirmed boundary workout missing"
        seen.add(cursor)


def main() -> None:
    template = _template()
    bench = template["blocks"][0]["exercises"][0]["exercise_id"]
    dumbbell = template["blocks"][0]["exercises"][1]["exercise_id"]
    pullup = template["blocks"][1]["exercises"][0]["exercise_id"]
    hold = template["blocks"][1]["exercises"][1]["exercise_id"]
    source = template["blocks"][2]["exercises"][0]["exercise_id"]
    with ENGINE.connect() as db:
        equipment = dict(
            db.execute(
                text("""
                SELECT r.equipment_type, i.equipment_id
                FROM trainer.equipment_items i
                JOIN trainer.equipment_item_revisions r
                  ON r.equipment_revision_id=i.current_revision_id
                WHERE r.name IN ('Synthetic D-G06 barbell', 'Synthetic D-G06 resistance_band')
                """)
            ).all()
        )
    barbell_id = str(equipment["barbell"])
    band_id = str(equipment["resistance_band"])
    # A dedicated real plan establishes that the skipped node was planned;
    # extra exercises are independent additions, not implicit replacements.
    plan = synthetic_plan(ENGINE, "Synthetic G02 boundary plan")
    plan["blocks"][0]["exercises"][0]["exercise_id"] = source
    draft = post(
        "/api/v1/commands/plan-drafts",
        {
            "context": context("g02-boundary-plan-draft"),
            "plan": plan,
            "recommendations": insufficient_recommendations(ENGINE),
        },
        "g02-boundary-plan-draft-key",
    )["data"]
    result = confirm(draft, "g02-boundary-plan")
    plan_ref = next(r for r in result["created_revisions"] if r["object_type"] == "workout_plan")
    with ENGINE.connect() as db:
        planned = (
            db.execute(
                text("""
            SELECT b.planned_block_id, e.planned_exercise_id
            FROM trainer.planned_blocks b
            JOIN trainer.planned_exercises e USING(planned_block_id)
            WHERE b.plan_revision_id=:id
            """),
                {"id": plan_ref["revision_id"]},
            )
            .mappings()
            .one()
        )
    bodyweight = {"position": 1, "load_kind": "bodyweight"}
    band = {
        "position": 2,
        "load_kind": "band",
        "equipment_id": band_id,
        "direction": "assistance",
    }
    bw_sets = [_set(1, 6, [bodyweight]), _set(2, 8, [bodyweight, band])]
    report = {
        "plan_id": plan_ref["object_id"],
        "plan_revision_id": plan_ref["revision_id"],
        "started_at": None,
        "ended_at": None,
        "duration_seconds": None,
        "local_start_date": "2026-09-30",
        "session_type": "training",
        "name": "Synthetic G02 category boundaries",
        "lifecycle": "completed",
        "effort": None,
        "comment": "G02 category checks; synthetic only",
        "workout_metrics": [],
        "confirmed_unknown_planned_exercise_ids": [],
        "confirmed_unknown_planned_set_ids": [],
        "confirmed_unknown_planned_load_component_ids": [],
        "confirmed_unknown_metrics": [],
        "blocks": [
            {
                "position": 1,
                "planned_block_id": str(planned["planned_block_id"]),
                "block_type_code": "straight",
                "declared_rounds": None,
                "rest_seconds": 60,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": source,
                        "planned_exercise_id": str(planned["planned_exercise_id"]),
                        "result": "skipped",
                        "reason": "independent omission",
                        "sets": [],
                    },
                    _added(
                        2,
                        bench,
                        [
                            _set(1, 8, [_external("60", equipment_id=barbell_id)]),
                            _set(2, 6, [_external("65", equipment_id=barbell_id)]),
                        ],
                    ),
                    _added(3, dumbbell, [_set(1, 10, [_external("8", per_item=True)])]),
                    _added(4, pullup, bw_sets),
                ],
            },
            {
                "position": 2,
                "block_type_code": "circuit",
                "declared_rounds": 2,
                "rest_seconds": 45,
                "comment": None,
                "exercises": [
                    _added(
                        1,
                        bench,
                        [
                            _set(1, 5, [_external("30", equipment_id=barbell_id)], round_no=1),
                            _set(2, 5, [_external("35", equipment_id=barbell_id)], round_no=2),
                        ],
                    )
                ],
            },
            {
                "position": 3,
                "block_type_code": "superset",
                "declared_rounds": 1,
                "rest_seconds": 30,
                "comment": None,
                "exercises": [
                    _added(1, pullup, [_set(1, 5, [bodyweight], round_no=1)]),
                    _added(
                        2,
                        hold,
                        [
                            {
                                "position": 1,
                                "logical_set_no": 1,
                                "round_no": 1,
                                "set_type_code": "working",
                                "status": "performed",
                                "loads": [bodyweight],
                                "metrics": [
                                    {
                                        "metric_kind": "duration",
                                        "value": "30",
                                        "unit_code": "second",
                                        "source_kind": "user",
                                    }
                                ],
                            }
                        ],
                    ),
                ],
            },
        ],
    }
    report = deepcopy(report)
    prepared = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("g02-boundary-workout-draft"), "report": report},
        "g02-boundary-workout-draft-key",
    )["data"]
    assert not prepared["gaps"], prepared["gaps"]
    committed = confirm(prepared, "g02-boundary-workout")
    ref = next(r for r in committed["created_revisions"] if r["object_type"] == "workout_session")
    saved = _history(ref)
    straight, circuit, superset = saved["blocks"]
    skip, barbell, dumbbells, pullups = straight["exercises"]
    assert skip["result"] == "skipped" and skip["sets"] == []
    assert skip["reason"] != "replacement" and skip["planned_exercise_id"] is not None
    for extra in (barbell, dumbbells, pullups):
        assert extra["result"] == "added" and extra["planned_exercise_id"] is None
        assert extra.get("replacement_source") is None
    assert [Decimal(str(s["loads"][0]["mass"])) for s in barbell["sets"]] == [
        Decimal(60),
        Decimal(65),
    ]
    assert all(s["loads"][0]["mass_meaning"] == "total_external_mass" for s in barbell["sets"])
    # The 20kg bar and 20/22.5kg per side in pack metadata describe these totals;
    # they are not another DTO load component to add to 60/65kg.
    assert [len(s["loads"]) for s in barbell["sets"]] == [1, 1]
    item = dumbbells["sets"][0]["loads"][0]
    assert (Decimal(str(item["mass"])), Decimal(str(item["item_count"])), item["mass_meaning"]) == (
        Decimal(8),
        Decimal(2),
        "per_item_mass",
    )
    assert [s["loads"][0]["load_kind"] for s in pullups["sets"]] == ["bodyweight", "bodyweight"]
    assert [len(s["loads"]) for s in pullups["sets"]] == [1, 2]
    assert pullups["sets"][1]["loads"][1]["equipment_id"] == band_id
    assert pullups["sets"][1]["loads"][1]["direction"] == "assistance"
    assert circuit["block_type"] == "circuit" and circuit["declared_rounds"] == 2
    assert [
        (s["round_no"], Decimal(str(s["loads"][0]["mass"])))
        for s in circuit["exercises"][0]["sets"]
    ] == [(1, Decimal(30)), (2, Decimal(35))]
    assert superset["block_type"] == "superset" and superset["declared_rounds"] == 1
    assert [e["exercise_id"] for e in superset["exercises"]] == [pullup, hold]
    assert all(e["sets"][0]["round_no"] == 1 for e in superset["exercises"])
    print(
        "PASS: G02-03/04/05/07/10/11 equivalent category API/persisted boundaries; "
        "no invented assembly field"
    )


if __name__ == "__main__":
    main()
