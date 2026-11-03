"""Synthetic plan preview/read-back check; run only against an isolated database."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from plan_flow import context, current_plan, post, show_and_confirm
from sqlalchemy import create_engine, text

from trainer.domain.context import digest_payload


def run() -> None:
    import os

    engine = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    with engine.connect() as db:
        unilateral = (
            db.execute(
                text("""
            SELECT e.exercise_id,r.exercise_revision_id,to_jsonb(r) AS revision
            FROM trainer.exercises e JOIN trainer.exercise_revisions r
              ON r.exercise_revision_id=e.current_revision_id
            WHERE r.canonical_name='Synthetic unilateral curl'
        """)
            )
            .mappings()
            .one()
        )
        other = (
            db.execute(
                text("""
            SELECT e.exercise_id,r.exercise_revision_id
            FROM trainer.exercises e JOIN trainer.exercise_revisions r
              ON r.exercise_revision_id=e.current_revision_id
            WHERE r.canonical_name='Synthetic pull-up'
        """)
            )
            .mappings()
            .one()
        )
    sets = []
    for position, (round_no, side) in enumerate(
        ((1, "left"), (1, "right"), (2, "left"), (2, "right")), 1
    ):
        sets.append(
            {
                "position": position,
                "round_no": round_no,
                "logical_set_no": position,
                "side": side,
                "set_type_code": "working",
                "target_rpe_min": None,
                "target_rpe_max": None,
                "target_rir_min": None,
                "target_rir_max": None,
                "tempo": None,
                "pause_seconds": None,
                "rest_after_seconds": 45,
                "metrics": [
                    {
                        "metric_kind": "repetitions",
                        "exact_value": "8",
                        "min_value": None,
                        "max_value": None,
                        "unit_code": "repetition",
                        "custom_definition": None,
                    }
                ],
                "loads": [],
            }
        )
    second_sets = [deepcopy(sets[0]), deepcopy(sets[2])]
    for position, planned_set in enumerate(second_sets, 1):
        planned_set.update(position=position, logical_set_no=position, side="both")
    plan = {
        "context": context("compact-plan-draft"),
        "plan": {
            "planned_date": (datetime.now(UTC).date() + timedelta(days=4)).isoformat(),
            "planned_time": None,
            "program_id": None,
            "program_version_id": None,
            "day_template_id": None,
            "plan_type": "training",
            "name": "Вымышленный план со сторонами",
            "purpose": "Проверка предпросмотра",
            "comment": "Ходьба обсуждалась, но отдельным упражнением не включена",
            "temporary_conditions": {
                "place": None,
                "equipment_additions": [],
                "equipment_removals": [],
            },
            "blocks": [
                {
                    "position": 1,
                    "block_type_code": "circuit",
                    "name": "Вымышленный круг",
                    "declared_rounds": 2,
                    "rest_seconds": 60,
                    "comment": None,
                    "exercises": [
                        {
                            "position": 1,
                            "exercise_id": str(unilateral["exercise_id"]),
                            "circuit_position": 1,
                            "comment": None,
                            "sets": sets,
                        },
                        {
                            "position": 2,
                            "exercise_id": str(other["exercise_id"]),
                            "circuit_position": 2,
                            "comment": None,
                            "sets": second_sets,
                        },
                    ],
                }
            ],
        },
        "recommendations": [
            {
                "recommendation_kind": "plan_structure",
                "decision_text": "Вымышленная проверка порядка",
                "confidence": "insufficient",
                "revisit_condition": "после занятия",
                "fact_links": [
                    {
                        "ref": {
                            "object_type": "exercise",
                            "object_id": str(unilateral["exercise_id"]),
                            "revision_id": str(unilateral["exercise_revision_id"]),
                        },
                        "role": "context",
                        "reason": "Вымышленная карточка",
                        "snapshot_digest": digest_payload(unilateral["revision"]),
                    }
                ],
                "calculation_links": [],
                "conclusion_links": [],
                "assumptions": {"synthetic": "только проверка"},
                "alternatives": {},
                "short_explanation": "Вымышленный пример",
                "detailed_explanation": None,
            }
        ],
    }
    draft = post("/api/v1/commands/plan-drafts", plan, "compact-plan-draft")["data"]
    full = post(
        "/api/v1/commands/previews",
        {
            "context": context("compact-plan-full"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        "compact-plan-full",
    )["data"]
    details = post(
        "/api/v1/commands/previews",
        {
            "context": context("compact-plan-details"),
            "revision_id": draft["revision_id"],
            "detail_level": "details",
        },
        "compact-plan-details",
    )["data"]
    assert full["complete"] is True and details["complete"] is False
    assert full["revision_id"] == details["revision_id"] == draft["revision_id"]
    assert "на каждую сторону" in full["canonical_text"]
    assert "Круги 1–2: Synthetic unilateral curl → Synthetic pull-up" in full["canonical_text"]
    assert "Только в примечании" in full["canonical_text"]
    assert "Подход 1" in details["canonical_text"] and "Подход 4" in details["canonical_text"]
    committed = show_and_confirm(draft, "compact-plan-confirm")
    created = next(
        item for item in committed["created_revisions"] if item["object_type"] == "workout_plan"
    )
    saved = current_plan(created["object_id"])
    saved_sets = saved["tree"]["blocks"][0]["exercises"][0]["sets"]
    assert [item["side"] for item in saved_sets] == ["left", "right", "left", "right"]
    assert [item["round_no"] for item in saved_sets] == [1, 1, 2, 2]
    assert [Decimal(str(item["metrics"][0]["exact_value"])) for item in saved_sets] == [
        Decimal(8)
    ] * 4


if __name__ == "__main__":
    run()
    print("PASS: plan preview, details, side read-back and confirmation replay")
