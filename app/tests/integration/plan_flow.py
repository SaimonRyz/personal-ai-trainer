# ruff: noqa: E501
"""Synthetic D-G05 HTTP/PostgreSQL flow; run only in the isolated gate volume."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
from evidence_fixture import insufficient_recommendations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import (
    ContextRepositoryError,
)
from trainer.infrastructure.db.evidence import EvidenceRepository as ContextRepository
from trainer.transport.http.contracts import (
    ConfirmRequest,
    PlanDraftRequest,
    PlanLifecycleRequest,
    PreviewRequest,
    PreviewShownRequest,
    ProgramDraftRequest,
    ProgramPublicationDraftRequest,
)

BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]
ENGINE = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
ADMIN_DSN = os.environ["TRAINER_TEST_ADMIN_DSN"].replace(
    "postgresql+psycopg://", "postgresql://", 1
)
ROLLBACK_SNAPSHOT_TABLES = (
    "workout_plans",
    "workout_plan_revisions",
    "planned_blocks",
    "planned_exercises",
    "planned_sets",
    "planned_metric_values",
    "planned_load_components",
    "recommendations",
    "recommendation_fact_links",
    "recommendation_calculation_links",
    "recommendation_conclusion_links",
    "recommendation_explanations",
    "plan_reschedule_links",
    "drafts",
    "draft_revisions",
    "draft_dependencies",
    "previews",
    "operation_capabilities",
    "preview_show_events",
    "confirmations",
    "idempotent_operations",
    "commit_results",
    "domain_changes",
    "agent_operation_audit",
)


def context(message: str) -> dict[str, Any]:
    return {
        "conversation_id": "synthetic-dg05",
        "message_id": message,
        "effective_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "timezone": "Asia/Novokuznetsk",
        "locale": "ru",
    }


def post(
    path: str, body: dict[str, Any], key: str | None = None, expected: int = 200
) -> dict[str, Any]:
    status, result = post_with_status(path, body, key)
    assert status == expected, (status, result)
    return result


def post_with_status(
    path: str, body: dict[str, Any], key: str | None = None
) -> tuple[int, dict[str, Any]]:
    if key is not None:
        model = (
            ConfirmRequest
            if path.endswith("/confirm")
            else PreviewShownRequest
            if path.endswith("/shown")
            else PreviewRequest
            if path.endswith("/previews")
            else PlanLifecycleRequest
            if path.endswith("/plan-lifecycle-drafts")
            else ProgramPublicationDraftRequest
            if path.endswith("/program-publication-drafts")
            else ProgramDraftRequest
            if path.endswith("/program-drafts")
            else PlanDraftRequest
        )
        normalized = model.model_validate({**body, "request_digest": "0" * 64}).model_dump(
            mode="json"
        )
        normalized.pop("request_digest", None)
        body = {**normalized, "request_digest": digest_payload(normalized)}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
    }
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, cast(dict[str, Any], json.loads(response.read()))
    except urllib.error.HTTPError as error:
        result = json.loads(error.read())
        return error.code, cast(dict[str, Any], result)


def table_row_counts(tables: tuple[str, ...]) -> tuple[int, ...]:
    with ENGINE.connect() as db:
        return tuple(
            db.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            for table in tables
        )


def prepare_confirmation(draft: dict[str, Any], prefix: str) -> tuple[str, dict[str, Any], str]:
    shown = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        prefix + "-preview",
    )["data"]
    message = prefix + "-shown"
    post(
        f"/api/v1/commands/previews/{shown['preview_id']}/shown",
        {
            "context": context(message),
            "revision_id": draft["revision_id"],
            "preview_digest": shown["digest"],
            "shown_message_id": message,
            "displayed_text_digest": hashlib.sha256(shown["canonical_text"].encode()).hexdigest(),
            "cross_dialog": False,
        },
        message,
    )
    confirm = {
        "context": context(message),
        "revision_id": draft["revision_id"],
        "preview_digest": shown["digest"],
        "confirmation_kind": shown["confirmation_kind"],
        "confirmation_message_id": message,
        "explicit_intent": "synthetic explicit confirmation",
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }
    return shown["preview_id"], confirm, prefix + "-confirm"


def show_and_confirm(draft: dict[str, Any], prefix: str) -> dict[str, Any]:
    preview_id, confirm, key = prepare_confirmation(draft, prefix)
    result = post(f"/api/v1/commands/previews/{preview_id}/confirm", confirm, key)
    assert result["data"]["status"] == "committed"
    replay = post(f"/api/v1/commands/previews/{preview_id}/confirm", confirm, key)
    assert replay["data"]["status"] == "already_committed"
    assert replay["data"]["commit_result_id"] == result["data"]["commit_result_id"]
    return cast(dict[str, Any], result["data"])


def confirm_with_fault(draft: dict[str, Any], prefix: str) -> None:
    preview_id, confirm, key = prepare_confirmation(draft, prefix)
    confirm["explicit_intent"] = "synthetic rollback confirmation"
    payload = ConfirmRequest.model_validate({**confirm, "request_digest": "0" * 64}).model_dump(
        mode="json"
    )
    payload.pop("request_digest")
    payload["request_digest"] = digest_payload(payload)
    before = table_row_counts(ROLLBACK_SNAPSHOT_TABLES)
    with Session(ENGINE) as session:
        try:
            ContextRepository(session).confirm(
                preview_id=UUID(preview_id),
                payload=payload,
                key=key,
                correlation_id=uuid4(),
                fail_before_commit=True,
            )
        except RuntimeError as error:
            assert "synthetic fault before transaction commit" in str(error)
            session.rollback()
        else:
            raise AssertionError("fault injection unexpectedly committed")
    after = table_row_counts(ROLLBACK_SNAPSHOT_TABLES)
    assert after == before, (before, after)


def current_plan(plan_id: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        post(
            "/api/v1/queries/plans/get",
            {"context": context("read-" + str(uuid4())), "mode": "current", "plan_id": plan_id},
        )["data"],
    )


def program_version_input(version: dict[str, Any]) -> dict[str, Any]:
    templates = []
    for template in version["templates"]:
        blocks = []
        for block in template["blocks"]:
            exercises = []
            for exercise in block["exercises"]:
                sets = []
                for planned_set in exercise["sets"]:
                    metrics = [
                        {
                            key: metric[key]
                            for key in (
                                "metric_kind",
                                "exact_value",
                                "min_value",
                                "max_value",
                                "unit_code",
                            )
                        }
                        for metric in planned_set["metrics"]
                    ]
                    sets.append(
                        {
                            key: planned_set[key]
                            for key in (
                                "position",
                                "round_no",
                                "logical_set_no",
                                "side",
                                "set_type_code",
                                "tempo",
                                "pause_seconds",
                                "rest_after_seconds",
                            )
                        }
                        | {"metrics": metrics}
                    )
                exercises.append(
                    {
                        key: exercise[key]
                        for key in ("position", "exercise_ref", "circuit_position", "comment")
                    }
                    | {"sets": sets}
                )
            blocks.append(
                {
                    key: block[key]
                    for key in (
                        "position",
                        "block_type_code",
                        "name",
                        "declared_rounds",
                        "rest_seconds",
                        "comment",
                    )
                }
                | {"exercises": exercises}
            )
        templates.append(
            {key: template[key] for key in ("position", "day_type_code", "purpose")}
            | {"blocks": blocks}
        )
    recommendations = []
    for recommendation in version["recommendations"]:
        recommendations.append(
            {
                key: recommendation[key]
                for key in (
                    "recommendation_kind",
                    "decision_text",
                    "confidence",
                    "revisit_condition",
                    "fact_links",
                    "calculation_links",
                    "assumptions",
                    "alternatives",
                    "short_explanation",
                    "detailed_explanation",
                )
            }
            | {
                "conclusion_links": [
                    {"conclusion_version_id": link["conclusion_version_id"], "role": link["role"]}
                    for link in recommendation["conclusion_links"]
                ]
            }
        )
    return {
        key: version[key] for key in ("version_label", "rationale", "period_start", "period_end")
    } | {"templates": templates, "recommendations": recommendations}


def plan_blocks_from_program(template: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for block in template["blocks"]:
        exercises = []
        for exercise in block["exercises"]:
            sets = []
            for planned_set in exercise["sets"]:
                sets.append(
                    {
                        key: planned_set[key]
                        for key in (
                            "position",
                            "round_no",
                            "logical_set_no",
                            "side",
                            "set_type_code",
                            "tempo",
                            "pause_seconds",
                            "rest_after_seconds",
                        )
                    }
                    | {
                        "target_rpe_min": None,
                        "target_rpe_max": None,
                        "target_rir_min": None,
                        "target_rir_max": None,
                        "metrics": [
                            {
                                key: metric[key]
                                for key in (
                                    "metric_kind",
                                    "exact_value",
                                    "min_value",
                                    "max_value",
                                    "unit_code",
                                )
                            }
                            | {"custom_definition": None}
                            for metric in planned_set["metrics"]
                        ],
                        "loads": [],
                    }
                )
            exercises.append(
                {
                    "position": exercise["position"],
                    "exercise_id": exercise["exercise_ref"]["object_id"],
                    "circuit_position": exercise["circuit_position"],
                    "comment": exercise["comment"],
                    "sets": sets,
                }
            )
        blocks.append(
            {
                key: block[key]
                for key in (
                    "position",
                    "block_type_code",
                    "name",
                    "declared_rounds",
                    "rest_seconds",
                    "comment",
                )
            }
            | {"exercises": exercises}
        )
    return blocks


def restore_synthetic_program_support(program_id: str, confirmation_id: str) -> None:
    with ENGINE.connect() as db:
        support = db.execute(
            text(
                "SELECT DISTINCT l.conclusion_version_id FROM trainer.programs p "
                "JOIN trainer.program_versions v ON v.program_version_id=p.current_version_id "
                "JOIN trainer.recommendations r ON r.program_version_id=v.program_version_id "
                "JOIN trainer.recommendation_conclusion_links l ON l.recommendation_id=r.recommendation_id "
                "WHERE p.program_id=:program"
            ),
            {"program": program_id},
        ).scalar_one()
        profile = (
            db.execute(
                text(
                    "SELECT p.profile_id FROM trainer.profiles p "
                    "JOIN trainer.profile_revisions r ON r.profile_revision_id=p.current_revision_id "
                    "WHERE p.archived_at IS NULL"
                )
            )
            .mappings()
            .one()
        )
        next_revision = db.execute(
            text(
                "SELECT COALESCE(MAX(revision_no),0)+1 FROM trainer.knowledge_status_revisions "
                "WHERE conclusion_version_id=:version"
            ),
            {"version": support},
        ).scalar_one()
    protocol_id, status_id = uuid4(), uuid4()
    with psycopg.connect(ADMIN_DSN) as admin, admin.cursor() as cursor:
        cursor.execute("SET session_replication_role=replica")
        # D-G04 intentionally appends an outdated synthetic status using its
        # own clock. Backdate only that disposable fixture row so the D-G05
        # verified successor is unambiguously current across container clocks.
        cursor.execute(
            "UPDATE trainer.knowledge_status_revisions SET effective_at=clock_timestamp()-interval '2 days' "
            "WHERE conclusion_version_id=%s AND status='outdated'",
            (support,),
        )
        cursor.execute(
            "INSERT INTO trainer.verification_protocol_runs(protocol_run_id,conclusion_version_id,reviewer_profile_id,protocol_version,checklist_result,decision,reason,performed_at,confirmation_id,created_at) "
            "VALUES (%s,%s,%s,'synthetic-dg05','{}','verified','restore isolated support after D-G04 outdated test',now(),%s,now())",
            (protocol_id, support, profile["profile_id"], confirmation_id),
        )
        cursor.execute(
            "INSERT INTO trainer.knowledge_status_revisions(status_revision_id,conclusion_version_id,revision_no,status,effective_at,reason,protocol_run_id,created_at) "
            "VALUES (%s,%s,%s,'verified',clock_timestamp()-interval '1 hour','isolated D-G05 snapshot fixture',%s,now())",
            (status_id, support, next_revision, protocol_id),
        )
        cursor.execute("SET session_replication_role=origin")
    with ENGINE.connect() as db:
        current_status = db.execute(
            text(
                "SELECT status FROM trainer.knowledge_status_revisions "
                "WHERE conclusion_version_id=:version ORDER BY effective_at DESC,revision_no DESC LIMIT 1"
            ),
            {"version": support},
        ).scalar_one()
        assert current_status == "verified", current_status


def assert_program_plan_snapshot() -> None:
    with ENGINE.connect() as db:
        program_row = (
            db.execute(
                text(
                    "SELECT program_id,current_version_id FROM trainer.programs "
                    "WHERE archived_at IS NULL AND current_version_id IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
    program_id = str(program_row["program_id"])
    version_id = str(program_row["current_version_id"])
    query = {
        "context": context("plan-from-program-read"),
        "mode": "current",
        "program_id": program_id,
    }
    source = post("/api/v1/queries/programs/get", query)["data"]
    template = source["version"]["templates"][0]
    planned_date = max(
        datetime.now(UTC).date() + timedelta(days=1),
        datetime.fromisoformat(source["version"]["period_start"]).date(),
    )
    assert planned_date <= datetime.fromisoformat(source["version"]["period_end"]).date()
    body = {
        "context": context("plan-from-program-draft"),
        "plan": {
            "planned_date": planned_date.isoformat(),
            "planned_time": "18:00:00",
            "program_id": program_id,
            "program_version_id": version_id,
            "day_template_id": template["day_template_id"],
            "plan_type": "training",
            "name": "Synthetic plan from published program",
            "purpose": template["purpose"],
            "comment": None,
            "temporary_conditions": {
                "place": None,
                "equipment_additions": [],
                "equipment_removals": [],
            },
            "blocks": plan_blocks_from_program(template),
        },
        "recommendations": insufficient_recommendations(ENGINE),
    }
    draft = post("/api/v1/commands/plan-drafts", body, "plan-from-program-draft")["data"]
    assert not draft["gaps"], draft
    committed = show_and_confirm(draft, "plan-from-program")
    plan_ref = next(
        item for item in committed["created_revisions"] if item["object_type"] == "workout_plan"
    )
    with ENGINE.connect() as db:
        plan_confirmation_id = db.execute(
            text(
                "SELECT confirmation_id FROM trainer.workout_plan_revisions "
                "WHERE plan_revision_id=:revision"
            ),
            {"revision": plan_ref["revision_id"]},
        ).scalar_one()
    restore_synthetic_program_support(program_id, str(plan_confirmation_id))
    exact_query = {
        "context": context("plan-from-program-exact-before"),
        "mode": "exact",
        "plan_id": plan_ref["object_id"],
        "revision_id": plan_ref["revision_id"],
    }
    before = post("/api/v1/queries/plans/get", exact_query)["data"]
    assert before["tree"]["program_version_id"] == version_id

    next_body = {
        "context": context("plan-program-next-draft"),
        "intent": "new_version",
        "program_id": program_id,
        "expected_current_version_id": version_id,
        "goal_revision_id": source["version"]["goal_revision_ref"]["revision_id"],
        "program": None,
        "version": program_version_input(source["version"]),
    }
    next_body["version"]["version_label"] += " successor"
    program_draft = post("/api/v1/commands/program-drafts", next_body, "plan-program-next-draft")[
        "data"
    ]
    assert not program_draft["gaps"], program_draft
    publication = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context("plan-program-publication"),
            "source_draft_id": program_draft["draft_id"],
            "source_revision_id": program_draft["revision_id"],
            "expected_current_version_id": version_id,
            "publication_reason": "synthetic snapshot immutability check",
        },
        "plan-program-publication",
    )["data"]
    assert not publication["gaps"], publication
    show_and_confirm(publication, "plan-program-publication")

    after = post(
        "/api/v1/queries/plans/get",
        {**exact_query, "context": context("plan-from-program-exact-after")},
    )["data"]
    assert after["tree"] == before["tree"]
    assert after["content_digest"] == before["content_digest"]
    assert after["context_snapshot"] == before["context_snapshot"]
    assert after["context_snapshot_digest"] == before["context_snapshot_digest"]
    assert after["tree"]["program_version_id"] == version_id


def assert_plan_tree_insert_guards(plan: dict[str, str]) -> None:
    with ENGINE.connect() as db:
        ids = (
            db.execute(
                text("""
            SELECT b.planned_block_id,e.planned_exercise_id,s.planned_set_id,
                   m.planned_metric_value_id,r.recommendation_id
            FROM trainer.planned_blocks b
            JOIN trainer.planned_exercises e USING(planned_block_id)
            JOIN trainer.planned_sets s USING(planned_exercise_id)
            JOIN trainer.planned_metric_values m USING(planned_set_id)
            JOIN trainer.recommendations r ON r.plan_revision_id=b.plan_revision_id
            WHERE b.plan_revision_id=:revision
            LIMIT 1
        """),
                {"revision": plan["revision_id"]},
            )
            .mappings()
            .one()
        )

    statements = [
        (
            "planned_blocks",
            """INSERT INTO trainer.planned_blocks(planned_block_id,plan_revision_id,position,block_type,name,
                declared_rounds,rest_seconds,comment,created_at)
                SELECT :new,b.plan_revision_id,b.position,b.block_type,b.name,b.declared_rounds,b.rest_seconds,b.comment,now()
                FROM trainer.planned_blocks b WHERE b.planned_block_id=:parent""",
            {"new": str(uuid4()), "parent": ids["planned_block_id"]},
        ),
        (
            "planned_exercises",
            """INSERT INTO trainer.planned_exercises(planned_exercise_id,planned_block_id,exercise_id,position,
                circuit_position,equipment_snapshot,comment,created_at)
                SELECT :new,e.planned_block_id,e.exercise_id,e.position,e.circuit_position,e.equipment_snapshot,e.comment,now()
                FROM trainer.planned_exercises e WHERE e.planned_exercise_id=:parent""",
            {"new": str(uuid4()), "parent": ids["planned_exercise_id"]},
        ),
        (
            "planned_sets",
            """INSERT INTO trainer.planned_sets(planned_set_id,planned_exercise_id,position,round_no,logical_set_no,side,
                set_type,target_rpe_min,target_rpe_max,target_rir_min,target_rir_max,tempo,pause_seconds,rest_after_seconds,created_at)
                SELECT :new,s.planned_exercise_id,s.position,s.round_no,s.logical_set_no,s.side,s.set_type,s.target_rpe_min,
                s.target_rpe_max,s.target_rir_min,s.target_rir_max,s.tempo,s.pause_seconds,s.rest_after_seconds,now()
                FROM trainer.planned_sets s WHERE s.planned_set_id=:parent""",
            {"new": str(uuid4()), "parent": ids["planned_set_id"]},
        ),
        (
            "planned_metric_values",
            """INSERT INTO trainer.planned_metric_values(planned_metric_value_id,planned_set_id,metric_kind,exact_value,
                min_value,max_value,unit_code,custom_definition,created_at)
                SELECT :new,m.planned_set_id,m.metric_kind,m.exact_value,m.min_value,m.max_value,m.unit_code,m.custom_definition,now()
                FROM trainer.planned_metric_values m WHERE m.planned_metric_value_id=:parent""",
            {"new": str(uuid4()), "parent": ids["planned_metric_value_id"]},
        ),
        (
            "planned_load_components",
            """INSERT INTO trainer.planned_load_components(planned_load_component_id,planned_set_id,position,load_kind,
                equipment_id,equipment_snapshot,item_count,mass,unit_code,mass_meaning,direction,raw_ambiguous_text,created_at)
                VALUES (:new,:set,999,:kind,NULL,'{}',NULL,NULL,NULL,NULL,NULL,NULL,now())""",
            {
                "new": str(uuid4()),
                "set": ids["planned_set_id"],
                "kind": "synthetic_invalid_kind",
            },
        ),
        (
            "recommendations",
            """INSERT INTO trainer.recommendations(recommendation_id,owner_type,program_version_id,plan_revision_id,
                recommendation_kind,decision_text,confidence,assumptions,alternatives,revisit_condition,confirmation_id,created_at)
                SELECT :new,'plan_revision',NULL,r.plan_revision_id,r.recommendation_kind,'forbidden', 'low','{}','{}',
                       'forbidden',r.confirmation_id,now()
                FROM trainer.recommendations r WHERE r.recommendation_id=:parent""",
            {"new": str(uuid4()), "parent": ids["recommendation_id"]},
        ),
        (
            "recommendation_fact_links",
            """INSERT INTO trainer.recommendation_fact_links(recommendation_id,object_type,object_id,revision_id,role,reason,snapshot_digest,created_at)
                SELECT f.recommendation_id,f.object_type,f.object_id,f.revision_id,f.role,f.reason,f.snapshot_digest,now()
                FROM trainer.recommendation_fact_links f WHERE f.recommendation_id=:parent LIMIT 1""",
            {"parent": ids["recommendation_id"]},
        ),
        (
            "recommendation_calculation_links",
            """INSERT INTO trainer.recommendation_calculation_links(recommendation_id,derived_value_id,role,created_at)
                VALUES (:recommendation,:derived,'input',now())""",
            {"recommendation": ids["recommendation_id"], "derived": str(uuid4())},
        ),
        (
            "recommendation_conclusion_links",
            """INSERT INTO trainer.recommendation_conclusion_links(recommendation_id,conclusion_version_id,status_revision_id,
                role,applicability_assessment,status_at_decision,created_at)
                VALUES (:recommendation,:conclusion,:status,'supports','{}','draft',now())""",
            {
                "recommendation": ids["recommendation_id"],
                "conclusion": str(uuid4()),
                "status": str(uuid4()),
            },
        ),
        (
            "recommendation_explanations",
            """INSERT INTO trainer.recommendation_explanations(explanation_id,recommendation_id,detail_level,content,
                generated_at,is_reasoning,created_at)
                SELECT :new,e.recommendation_id,e.detail_level,e.content,now(),e.is_reasoning,now()
                FROM trainer.recommendation_explanations e WHERE e.recommendation_id=:parent LIMIT 1""",
            {"new": str(uuid4()), "parent": ids["recommendation_id"]},
        ),
    ]
    for table, statement, params in statements:
        with ENGINE.connect() as db:
            before = db.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            try:
                db.execute(text(statement), params)
                db.commit()
            except Exception as error:
                db.rollback()
                assert "confirmed plan tree cannot be extended" in str(error), (table, error)
            else:
                raise AssertionError(f"trainer_app inserted into confirmed plan table {table}")
            after = db.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            assert after == before, (table, before, after)


def assert_equipment_exact_unusable_rejected(
    exact_plan: dict[str, Any], equipment_item: dict[str, Any]
) -> None:
    future_at = datetime.now(UTC) + timedelta(minutes=10)
    plan_tree = deepcopy(exact_plan["tree"])
    plan_tree["planned_date"] = (future_at.date() + timedelta(days=1)).isoformat()
    for block in plan_tree["blocks"]:
        block.pop("planned_block_id")
        for planned_exercise in block["exercises"]:
            planned_exercise.pop("planned_exercise_id")
            planned_exercise.pop("equipment_snapshot")
            for planned_set in planned_exercise["sets"]:
                planned_set.pop("planned_set_id")
                for load in planned_set["loads"]:
                    load.pop("planned_load_component_id", None)
                    load.pop("equipment_snapshot", None)
    target_set = plan_tree["blocks"][0]["exercises"][0]["sets"][0]
    target_set["loads"] = [
        {
            "position": 1,
            "load_kind": "external",
            "equipment_id": equipment_item["ref"]["object_id"],
            "item_count": None,
            "mass": "20",
            "unit_code": "kilogram",
            "mass_meaning": "total_external_mass",
            "direction": None,
            "raw_ambiguous_text": None,
        }
    ]
    payload = PlanDraftRequest.model_validate(
        {
            "context": {
                **context("exact-equipment-availability"),
                "effective_at": future_at.isoformat(),
            },
            "request_digest": "0" * 64,
            "plan": plan_tree,
            "recommendations": insufficient_recommendations(ENGINE),
        }
    ).model_dump(mode="json")
    payload.pop("request_digest")
    payload["request_digest"] = digest_payload(payload)
    with Session(ENGINE) as session:
        try:
            ContextRepository(session).create_plan_draft(
                payload=payload,
                key="exact-equipment-availability-" + uuid4().hex,
                correlation_id=uuid4(),
            )
        except ContextRepositoryError as error:
            assert error.code == "validation_error"
            assert "equipment item is not explicitly usable" in str(error)
            session.rollback()
        else:
            raise AssertionError("unknown/unusable inventory item was accepted as usable")


def assert_reschedule_link_rejected(
    source_plan_id: str, replacement_plan_id: str, sqlstate: str, message: str
) -> None:
    with ENGINE.connect() as db:
        before = db.execute(text("SELECT count(*) FROM trainer.plan_reschedule_links")).scalar_one()
        confirmation_id = db.execute(
            text("""
            SELECT r.confirmation_id FROM trainer.workout_plans p
            JOIN trainer.workout_plan_revisions r
              ON r.plan_id=p.plan_id AND r.plan_revision_id=p.current_revision_id
            WHERE p.plan_id=:source
        """),
            {"source": source_plan_id},
        ).scalar_one()
        try:
            db.execute(
                text("""
                INSERT INTO trainer.plan_reschedule_links(source_plan_id,replacement_plan_id,reason,confirmation_id,created_at)
                VALUES (:source,:replacement,'synthetic negative link',:confirmation,now())
            """),
                {
                    "source": source_plan_id,
                    "replacement": replacement_plan_id,
                    "confirmation": confirmation_id,
                },
            )
            db.commit()
        except DBAPIError as error:
            database_error = error.orig
            db.rollback()
            assert getattr(database_error, "sqlstate", None) == sqlstate, database_error
            assert message in str(database_error), database_error
        else:
            raise AssertionError(
                f"invalid reschedule link was accepted: {source_plan_id} -> {replacement_plan_id}"
            )
        after = db.execute(text("SELECT count(*) FROM trainer.plan_reschedule_links")).scalar_one()
        assert after == before, (before, after)


def create_plan(
    prefix: str,
    exercise_id: str,
    unilateral: bool,
    metrics: list[dict[str, str]],
    day: int,
    inject_fault: bool = False,
    numeric_scale_and_load: bool = False,
) -> dict[str, Any]:
    with ENGINE.connect() as db:
        exercise_fact = db.execute(
            text("""
            SELECT to_jsonb(r) FROM trainer.exercise_revisions r
            JOIN trainer.exercises e ON e.current_revision_id=r.exercise_revision_id
            WHERE e.exercise_id=:exercise
        """),
            {"exercise": exercise_id},
        ).scalar_one()
        recommendation_kind = db.execute(
            text(
                "SELECT code FROM trainer.recommendation_kind_catalog WHERE archived_at IS NULL ORDER BY code LIMIT 1"
            )
        ).scalar_one()
    name = "Synthetic plan " + prefix
    blocks = [
        {
            "position": 1,
            "block_type_code": "straight",
            "name": "Synthetic block",
            "declared_rounds": None,
            "rest_seconds": 60,
            "comment": None,
            "exercises": [
                {
                    "position": 1,
                    "exercise_id": exercise_id,
                    "circuit_position": None,
                    "comment": None,
                    "sets": [
                        {
                            "position": 1,
                            "round_no": None,
                            "logical_set_no": 1,
                            "side": "left" if unilateral else "both",
                            "set_type_code": "working",
                            "target_rpe_min": "7.0" if numeric_scale_and_load else None,
                            "target_rpe_max": "8.0" if numeric_scale_and_load else None,
                            "target_rir_min": None,
                            "target_rir_max": None,
                            "tempo": None,
                            "pause_seconds": "1.0" if numeric_scale_and_load else None,
                            "rest_after_seconds": None,
                            "metrics": metrics,
                            "loads": [
                                {
                                    "position": 1,
                                    "load_kind": "external",
                                    "equipment_id": None,
                                    "item_count": None,
                                    "mass": "30.0",
                                    "unit_code": "kilogram",
                                    "mass_meaning": "total_external_mass",
                                    "direction": None,
                                    "raw_ambiguous_text": None,
                                }
                            ] if numeric_scale_and_load else [],
                        }
                    ],
                }
            ],
        }
    ]
    body = {
        "context": context(prefix + "-draft"),
        "plan": {
            "planned_date": (datetime.now(UTC).date() + timedelta(days=day)).isoformat(),
            "planned_time": "18:00:00",
            "program_id": None,
            "program_version_id": None,
            "day_template_id": None,
            "plan_type": "training",
            "name": name,
            "purpose": "synthetic gate",
            "comment": None,
            "temporary_conditions": {
                "place": {
                    "kind": "ad_hoc",
                    "name": "Synthetic alternate place",
                    "place_type": "home_gym",
                },
                "equipment_additions": ["barbell"],
                "equipment_removals": [],
            },
            "blocks": blocks,
        },
        "recommendations": [
            {
                "recommendation_kind": recommendation_kind,
                "decision_text": "Synthetic gate-only rationale",
                "confidence": "insufficient",
                "revisit_condition": "End of synthetic gate",
                "fact_links": [
                    {
                        "ref": {
                            "object_type": "exercise",
                            "object_id": exercise_id,
                            "revision_id": str(exercise_fact["exercise_revision_id"]),
                        },
                        "role": "context",
                        "reason": "Exact exercise revision used by synthetic plan",
                        "snapshot_digest": digest_payload(exercise_fact),
                    }
                ],
                "calculation_links": [],
                "conclusion_links": [],
                "assumptions": {
                    "synthetic_gate": "Provisional synthetic choice; no verified support"
                },
                "alternatives": {},
                "short_explanation": "No verified scientific support; provisional synthetic gate choice",
                "detailed_explanation": None,
            }
        ],
    }
    path = "/api/v1/commands/plan-drafts"
    draft = post(path, deepcopy(body), prefix + "-draft")["data"]
    assert post(path, deepcopy(body), prefix + "-draft")["data"] == draft
    assert not draft["gaps"], draft
    if inject_fault:
        confirm_with_fault(draft, prefix + "-fault")
    committed = show_and_confirm(draft, prefix)
    created = next(
        item for item in committed["created_revisions"] if item["object_type"] == "workout_plan"
    )
    return {"plan_id": created["object_id"], "revision_id": created["revision_id"], "name": name}


def lifecycle_request(
    source: dict[str, str], intent: str, prefix: str, replacement: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "context": context(prefix + "-draft"),
        "intent": intent,
        "source_ref": {
            "object_type": "workout_plan",
            "object_id": source["plan_id"],
            "revision_id": source["revision_id"],
        },
        "expected_current_revision_id": source["revision_id"],
        "reason": "synthetic lifecycle check",
    }
    if replacement is not None:
        body["replacement_plan"] = replacement["plan"]
        body["replacement_recommendations"] = insufficient_recommendations(ENGINE)
    return body


def transition(
    source: dict[str, str],
    intent: str,
    prefix: str,
    replacement: dict[str, Any] | None = None,
    inject_fault: bool = False,
) -> dict[str, Any]:
    body = lifecycle_request(source, intent, prefix, replacement)
    path = "/api/v1/commands/plan-lifecycle-drafts"
    draft = post(path, deepcopy(body), prefix + "-draft")["data"]
    assert post(path, deepcopy(body), prefix + "-draft")["data"] == draft
    if inject_fault:
        confirm_with_fault(draft, prefix + "-fault")
    committed = show_and_confirm(draft, prefix)
    source_change = next(
        item for item in committed["created_revisions"] if item["object_id"] == source["plan_id"]
    )
    return {"commit": committed, "source_revision_id": source_change["revision_id"]}


def main() -> None:
    effective_at = datetime.now(UTC).replace(microsecond=0)
    with ENGINE.connect() as db:
        usual_place_id = db.execute(
            text("""
            SELECT r.usual_place_id FROM trainer.profiles p
            JOIN trainer.profile_revisions r ON r.profile_revision_id=p.current_revision_id
            WHERE p.archived_at IS NULL
        """)
        ).scalar_one()
    base_context = context("planning-context-base")
    base_context["effective_at"] = effective_at.isoformat()
    planning_base = post(
        "/api/v1/queries/planning-context",
        {
            "context": base_context,
            "target_date": (effective_at.date() + timedelta(days=2)).isoformat(),
        },
    )["data"]
    overlay_context = context("planning-context-overlay")
    overlay_context["effective_at"] = effective_at.isoformat()
    planning = post(
        "/api/v1/queries/planning-context",
        {
            "context": overlay_context,
            "target_date": (effective_at.date() + timedelta(days=2)).isoformat(),
            "temporary_conditions": {
                "place": {
                    "kind": "ad_hoc",
                    "name": "Synthetic alternate place",
                    "place_type": "home_gym",
                },
                "equipment_additions": ["barbell"],
                "equipment_removals": [],
            },
        },
    )["data"]
    assert planning["timezone"] == "Asia/Novokuznetsk"
    assert "barbell" in planning["effective_equipment_types"]
    assert planning["equipment_inventory_digest"] == digest_payload(
        planning["equipment_inventory_snapshot"]
    )
    assert "equipment_availability_snapshot" not in planning
    assert usual_place_id is not None
    assert planning["equipment_inventory_snapshot"] == planning_base["equipment_inventory_snapshot"]
    assert planning["facts"] == sorted(
        planning["facts"],
        key=lambda item: (-item["priority"], item["ref"]["object_type"], item["ref"]["object_id"]),
    )
    fact_tables = {
        "profile": ("profile_revisions", "profile_revision_id"),
        "goal": ("goal_revisions", "goal_revision_id"),
        "equipment_item": ("equipment_item_revisions", "equipment_revision_id"),
        "program_version": ("program_versions", "program_version_id"),
    }
    with ENGINE.connect() as db:
        for fact in planning["facts"]:
            table, revision_column = fact_tables[fact["ref"]["object_type"]]
            exact_revision = db.execute(
                text(f"SELECT to_jsonb(r) FROM trainer.{table} r WHERE {revision_column}=:id"),
                {"id": fact["ref"]["revision_id"]},
            ).scalar_one()
            assert fact["snapshot_digest"] == digest_payload(exact_revision)
    assert_program_plan_snapshot()
    with ENGINE.connect() as db:
        exercise = (
            db.execute(
                text("""
            SELECT e.exercise_id,r.unilateral,r.exercise_revision_id
            FROM trainer.exercises e JOIN trainer.exercise_revisions r
              ON r.exercise_revision_id=e.current_revision_id
            WHERE r.status_code='active'
              AND EXISTS(SELECT 1 FROM trainer.exercise_measurements m
                         WHERE m.exercise_revision_id=r.exercise_revision_id AND m.metric_kind='repetitions'
                           AND m.requiredness='required')
              AND NOT EXISTS(SELECT 1 FROM trainer.exercise_equipment_requirements q
                             WHERE q.exercise_revision_id=r.exercise_revision_id AND q.optionality='required'
                               AND q.equipment_type <> ALL(:types))
            ORDER BY e.created_at DESC LIMIT 1
        """),
                {"types": planning["effective_equipment_types"]},
            )
            .mappings()
            .one()
        )
        metric_rows = list(
            db.execute(
                text("""
            SELECT metric_kind,unit_family FROM trainer.exercise_measurements
            WHERE exercise_revision_id=:revision AND requiredness='required' ORDER BY metric_kind
        """),
                {"revision": exercise["exercise_revision_id"]},
            ).mappings()
        )
        alt_groups = list(
            db.execute(
                text("""
            SELECT alternative_group,array_agg(equipment_type) AS types
            FROM trainer.exercise_equipment_requirements
            WHERE exercise_revision_id=:revision AND optionality='alternative'
            GROUP BY alternative_group
        """),
                {"revision": exercise["exercise_revision_id"]},
            ).mappings()
        )
        assert all(
            set(group["types"]) & set(planning["effective_equipment_types"]) for group in alt_groups
        )
        before_sessions = db.execute(
            text("SELECT count(*) FROM trainer.workout_sessions")
        ).scalar_one()
    units = {"count": "repetition", "time": "second", "length": "meter"}
    metrics = [
        {
            "metric_kind": item["metric_kind"],
            "exact_value": "5",
            "min_value": None,
            "max_value": None,
            "unit_code": units[item["unit_family"]],
            "custom_definition": None,
        }
        for item in metric_rows
    ]
    exercise_id = str(exercise["exercise_id"])
    plan = create_plan(
        "reschedule-source", exercise_id, exercise["unilateral"], metrics, 2,
        inject_fault=True, numeric_scale_and_load=True,
    )
    assert_plan_tree_insert_guards(plan)
    conflict_plan = create_plan("stale-pointer", exercise_id, exercise["unilateral"], metrics, 6)
    lifecycle_path = "/api/v1/commands/plan-lifecycle-drafts"
    cancel_draft = post(
        lifecycle_path,
        lifecycle_request(conflict_plan, "cancel", "stale-cancel"),
        "stale-cancel-draft",
    )["data"]
    miss_draft = post(
        lifecycle_path,
        lifecycle_request(conflict_plan, "miss", "stale-miss"),
        "stale-miss-draft",
    )["data"]
    cancel_preview, cancel_confirm, cancel_key = prepare_confirmation(cancel_draft, "stale-cancel")
    miss_preview, miss_confirm, miss_key = prepare_confirmation(miss_draft, "stale-miss")
    before_conflict_effects = table_row_counts(
        (
            "workout_plans",
            "workout_plan_revisions",
            "plan_reschedule_links",
            "workout_sessions",
            "confirmations",
            "commit_results",
            "domain_changes",
        )
    )
    race_barrier = Barrier(2)

    def race_confirmation(
        preview_id: str, confirm: dict[str, Any], key: str
    ) -> tuple[int, dict[str, Any]]:
        race_barrier.wait(timeout=10)
        return post_with_status(f"/api/v1/commands/previews/{preview_id}/confirm", confirm, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future = pool.submit(race_confirmation, cancel_preview, cancel_confirm, cancel_key)
        miss_future = pool.submit(race_confirmation, miss_preview, miss_confirm, miss_key)
        cancel_status, cancel_result = cancel_future.result()
        miss_status, miss_result = miss_future.result()

    race_results = {"cancel": (cancel_status, cancel_result), "miss": (miss_status, miss_result)}
    winners = [
        intent
        for intent, (status, result) in race_results.items()
        if status == 200 and result["data"]["status"] == "committed"
    ]
    losers = [
        (status, result)
        for status, result in race_results.values()
        if status != 200 or result.get("data", {}).get("status") != "committed"
    ]
    assert len(winners) == 1, race_results
    assert len(losers) == 1, race_results
    loser_status, loser_result = losers[0]
    assert loser_status == 409, loser_result
    assert loser_result["error"]["code"] in {"stale_preview", "already_superseded"}
    conflict_now = current_plan(conflict_plan["plan_id"])
    assert conflict_now["lifecycle"] == {"cancel": "cancelled", "miss": "missed"}[winners[0]]
    after_conflict_effects = table_row_counts(
        (
            "workout_plans",
            "workout_plan_revisions",
            "plan_reschedule_links",
            "workout_sessions",
            "confirmations",
            "commit_results",
            "domain_changes",
        )
    )
    assert after_conflict_effects == (
        before_conflict_effects[0],
        before_conflict_effects[1] + 1,
        before_conflict_effects[2],
        before_conflict_effects[3],
        before_conflict_effects[4] + 1,
        before_conflict_effects[5] + 1,
        before_conflict_effects[6] + 1,
    ), (before_conflict_effects, after_conflict_effects, race_results)
    exact = post(
        "/api/v1/queries/plans/get",
        {
            "context": context("exact-before"),
            "mode": "exact",
            "plan_id": plan["plan_id"],
            "revision_id": plan["revision_id"],
        },
    )["data"]
    assert exact["tree"]["blocks"][0]["exercises"][0]["exercise_id"] == exercise_id
    first_block = exact["tree"]["blocks"][0]
    first_exercise = first_block["exercises"][0]
    first_set = first_exercise["sets"][0]
    first_load = first_set["loads"][0]
    with ENGINE.connect() as db:
        stored_child_ids = db.execute(
            text("""
                SELECT b.planned_block_id,e.planned_exercise_id,s.planned_set_id,
                       l.planned_load_component_id
                FROM trainer.planned_blocks b
                JOIN trainer.planned_exercises e USING (planned_block_id)
                JOIN trainer.planned_sets s USING (planned_exercise_id)
                JOIN trainer.planned_load_components l USING (planned_set_id)
                WHERE b.plan_revision_id=:revision AND b.position=1
                  AND e.position=1 AND s.position=1 AND l.position=1
            """),
            {"revision": plan["revision_id"]},
        ).mappings().one()
    assert first_block["planned_block_id"] == str(stored_child_ids["planned_block_id"])
    assert first_exercise["planned_exercise_id"] == str(stored_child_ids["planned_exercise_id"])
    assert first_set["planned_set_id"] == str(stored_child_ids["planned_set_id"])
    assert first_load["planned_load_component_id"] == str(
        stored_child_ids["planned_load_component_id"]
    )
    assert first_set["target_rpe_min"] == "7.00"
    assert first_set["pause_seconds"] == "1.000"
    assert set(exact["tree"]["blocks"][0]["exercises"][0]["equipment_snapshot"]) == {"requirements"}
    assert exact["context_snapshot"]["timezone"] == "Asia/Novokuznetsk"
    assert exact["context_snapshot"]["inventory_snapshot_version"] == "global_inventory_v2"
    assert "availability_place_id" not in exact["context_snapshot"]
    assert (
        exact["context_snapshot"]["equipment_inventory_snapshot"]
        == planning["equipment_inventory_snapshot"]
    )
    assert exact["context_snapshot_digest"] and exact["content_digest"]
    unknown_equipment = next(
        item
        for item in planning["equipment_inventory_snapshot"]
        if item["equipment"]["condition"] is None
    )
    assert_equipment_exact_unusable_rejected(exact, unknown_equipment)

    # Replacement tree is complete and independently materialized.
    replacement_body = {
        "plan": {
            "planned_date": (effective_at.date() + timedelta(days=3)).isoformat(),
            "planned_time": "18:30:00",
            "program_id": None,
            "program_version_id": None,
            "day_template_id": None,
            "plan_type": "training",
            "name": "Synthetic replacement",
            "purpose": "synthetic reschedule",
            "comment": None,
            "temporary_conditions": {
                "place": None,
                "equipment_additions": ["barbell"],
                "equipment_removals": [],
            },
            "blocks": deepcopy(exact["tree"]["blocks"]),
        }
    }
    for block in replacement_body["plan"]["blocks"]:
        block.pop("planned_block_id")
        for planned_exercise in block["exercises"]:
            planned_exercise.pop("planned_exercise_id")
            planned_exercise.pop("equipment_snapshot")
            for planned_set in planned_exercise["sets"]:
                planned_set.pop("planned_set_id")
                for load in planned_set["loads"]:
                    load.pop("planned_load_component_id", None)
                    load.pop("equipment_snapshot", None)
    result = transition(plan, "reschedule", "reschedule", replacement_body, inject_fault=True)
    source_now = current_plan(plan["plan_id"])
    assert source_now["lifecycle"] == "rescheduled"
    assert source_now["plan_ref"]["revision_id"] == result["source_revision_id"]
    assert source_now["transitions"][-1]["replacement_plan_ref"]
    replacement_ref = source_now["transitions"][-1]["replacement_plan_ref"]
    replacement_now = current_plan(replacement_ref["object_id"])
    assert replacement_now["lifecycle"] == "planned"
    assert replacement_now["transitions"][0]["source_plan_ref"]["object_id"] == plan["plan_id"]
    assert (
        exact["lifecycle"] == "planned" and exact["plan_ref"]["revision_id"] == plan["revision_id"]
    )

    cycle_a = create_plan("cycle-a", exercise_id, exercise["unilateral"], metrics, 8)
    transition(cycle_a, "reschedule", "cycle-a-reschedule", replacement_body)
    cycle_b_view = current_plan(
        current_plan(cycle_a["plan_id"])["transitions"][-1]["replacement_plan_ref"]["object_id"]
    )
    cycle_b = {
        "plan_id": cycle_b_view["plan_ref"]["object_id"],
        "revision_id": cycle_b_view["plan_ref"]["revision_id"],
    }
    transition(cycle_b, "reschedule", "cycle-b-reschedule", replacement_body)
    cycle_c_view = current_plan(
        current_plan(cycle_b["plan_id"])["transitions"][-1]["replacement_plan_ref"]["object_id"]
    )
    cycle_c = {
        "plan_id": cycle_c_view["plan_ref"]["object_id"],
        "revision_id": cycle_c_view["plan_ref"]["revision_id"],
    }
    transition(cycle_c, "reschedule", "cycle-c-reschedule", replacement_body)
    cycle_d_id = current_plan(cycle_c["plan_id"])["transitions"][-1]["replacement_plan_ref"][
        "object_id"
    ]
    assert_reschedule_link_rejected(
        cycle_d_id, cycle_d_id, "23514", "reschedule link cannot reference itself"
    )
    assert_reschedule_link_rejected(
        cycle_d_id, cycle_a["plan_id"], "23514", "reschedule link would create a cycle"
    )

    reuse_source = create_plan("reuse-target", exercise_id, exercise["unilateral"], metrics, 9)
    transition(reuse_source, "reschedule", "reuse-target-reschedule", replacement_body)
    assert_reschedule_link_rejected(
        reuse_source["plan_id"],
        replacement_ref["object_id"],
        "23514",
        "replacement plan is already linked",
    )

    for action, offset in (("cancel", 4), ("miss", 5)):
        untouched = create_plan(action, exercise_id, exercise["unilateral"], metrics, offset)
        terminal = transition(untouched, action, action + "-lifecycle")
        current = current_plan(untouched["plan_id"])
        assert current["lifecycle"] == {"cancel": "cancelled", "miss": "missed"}[action]
        assert current["plan_ref"]["revision_id"] == terminal["source_revision_id"]
        assert current["tree"]["blocks"] == exact["tree"]["blocks"]

    with ENGINE.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM trainer.workout_sessions")).scalar_one()
            == before_sessions
        )
        assert (
            db.execute(text("SELECT count(*) FROM trainer.plan_reschedule_links")).scalar_one() == 5
        )
        assert (
            db.execute(
                text("""
            SELECT count(*) FROM trainer.workout_plan_revisions
            WHERE lifecycle IN ('rescheduled','cancelled','missed')
        """)
            ).scalar_one()
            == 8
        )
    print(
        "PASS: D-G05 planning context, create-only plan, exact/current read, lifecycle, reschedule provenance, idempotency, and no workout fact"
    )


if __name__ == "__main__":
    main()
