"""Honest insufficient recommendation for older synthetic regression scenarios."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from trainer.domain.context import digest_payload


def insufficient_recommendations(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as db:
        fact = db.execute(
            text(
                "SELECT to_jsonb(r) FROM trainer.profile_revisions r "
                "JOIN trainer.profiles p ON p.current_revision_id=r.profile_revision_id "
                "WHERE p.archived_at IS NULL"
            )
        ).scalar_one()
        kind = db.execute(
            text(
                "SELECT code FROM trainer.recommendation_kind_catalog "
                "WHERE archived_at IS NULL ORDER BY code LIMIT 1"
            )
        ).scalar_one()
    return [
        {
            "recommendation_kind": kind,
            "decision_text": "Synthetic provisional regression plan",
            "confidence": "insufficient",
            "revisit_condition": "Before any real use; synthetic data only",
            "fact_links": [
                {
                    "ref": {
                        "object_type": "profile",
                        "object_id": fact["profile_id"],
                        "revision_id": fact["profile_revision_id"],
                    },
                    "role": "context",
                    "reason": "Exact synthetic owner profile",
                    "snapshot_digest": digest_payload(fact),
                }
            ],
            "calculation_links": [],
            "conclusion_links": [],
            "assumptions": {
                "assumption": "Provisional reversible choice for synthetic regression only"
            },
            "alternatives": {},
            "short_explanation": "No verified scientific support; provisional synthetic choice.",
            "detailed_explanation": None,
        }
    ]


def synthetic_plan(engine: Engine, name: str) -> dict[str, Any]:
    with engine.connect() as db:
        exercise = db.execute(
            text(
                "SELECT e.exercise_id FROM trainer.exercises e "
                "JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id "
                "WHERE r.canonical_name='Synthetic pull-up' AND r.status_code='active' "
                "ORDER BY e.created_at LIMIT 1"
            )
        ).scalar_one()
    return {
        "planned_date": (datetime.now(UTC).date() + timedelta(days=2)).isoformat(),
        "planned_time": None,
        "program_id": None,
        "program_version_id": None,
        "day_template_id": None,
        "plan_type": "training",
        "name": name,
        "purpose": "Synthetic E-G07 probe",
        "comment": None,
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
                "rest_seconds": None,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": str(exercise),
                        "circuit_position": None,
                        "comment": None,
                        "sets": [
                            {
                                "position": 1,
                                "round_no": None,
                                "logical_set_no": 1,
                                "side": "both",
                                "set_type_code": "working",
                                "target_rpe_min": None,
                                "target_rpe_max": None,
                                "target_rir_min": None,
                                "target_rir_max": None,
                                "tempo": None,
                                "pause_seconds": None,
                                "rest_after_seconds": None,
                                "loads": [],
                                "metrics": [
                                    {
                                        "metric_kind": "repetitions",
                                        "exact_value": "5",
                                        "min_value": None,
                                        "max_value": None,
                                        "unit_code": "repetition",
                                        "custom_definition": None,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
