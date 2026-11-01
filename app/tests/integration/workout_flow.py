# ruff: noqa: E501
"""D-G06 synthetic CMD-06 confirmation and exact saved-tree read-back."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from evidence_fixture import insufficient_recommendations
from pydantic import TypeAdapter
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.evidence import EvidenceRepository as ContextRepository
from trainer.infrastructure.db.workouts import WorkoutRepository
from trainer.transport.http.contracts import (
    ConfirmRequest,
    ExerciseCatalogDraftRequest,
    InventoryChangeDraftRequest,
    PlanDraftRequest,
    PreviewRequest,
    PreviewShownRequest,
    WorkoutCorrectionRequest,
    WorkoutDraftRevisionRequest,
    WorkoutReportDraftRequest,
)

BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]
ENGINE = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
ROLLBACK_SNAPSHOT_TABLES = (
    "workout_plans",
    "workout_plan_revisions",
    "planned_blocks",
    "planned_exercises",
    "planned_sets",
    "planned_metric_values",
    "planned_load_components",
    "plan_reschedule_links",
    "workout_sessions",
    "workout_session_revisions",
    "actual_blocks",
    "exercise_executions",
    "replacement_links",
    "actual_sets",
    "actual_metric_values",
    "actual_load_components",
    "workout_metrics",
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
    "derived_values",
    "derived_value_inputs",
)


def context(message_id: str) -> dict[str, Any]:
    return {
        "conversation_id": "synthetic-dg06-workout",
        "message_id": message_id,
        "effective_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "timezone": "Asia/Novokuznetsk",
        "locale": "ru",
    }


def post(
    path: str,
    payload: dict[str, Any],
    key: str | None = None,
    *,
    expected: int = 200,
    compute_digest: bool = True,
) -> dict[str, Any]:
    if key is not None and compute_digest:
        if path.endswith("inventory-change-drafts"):
            normalized = (
                TypeAdapter(InventoryChangeDraftRequest)
                .validate_python({**payload, "request_digest": "0" * 64})
                .model_dump(mode="json")
            )
            normalized.pop("request_digest", None)
            if model is PreviewRequest and normalized.get("entry_name") is None:
                normalized.pop("entry_name")
            payload = {**normalized, "request_digest": digest_payload(normalized)}
        else:
            model = (
                WorkoutReportDraftRequest
                if path.endswith("workout-report-drafts")
                else PlanDraftRequest
                if path.endswith("plan-drafts")
                else ConfirmRequest
                if path.endswith("/confirm")
                else WorkoutCorrectionRequest
                if path.endswith("correction-drafts")
                else WorkoutDraftRevisionRequest
                if path.endswith("/revisions") and "drafts/" in path
                else PreviewShownRequest
                if path.endswith("/shown")
                else ExerciseCatalogDraftRequest
                if path.endswith("exercise-catalog-drafts")
                else PreviewRequest
            )
            normalized = model.model_validate({**payload, "request_digest": "0" * 64}).model_dump(
                mode="json"
            )
            normalized.pop("request_digest", None)
            payload = {**normalized, "request_digest": digest_payload(normalized)}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == expected
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        result = json.loads(error.read())
        if error.code != expected:
            raise AssertionError(f"{path}: {error.code}: {result}") from error
        return result


def show_preview(
    draft: dict[str, Any], prefix: str, *, expected_complete: bool = True
) -> dict[str, Any]:
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        prefix + "-preview-key",
    )["data"]
    assert preview["complete"] is expected_complete
    shown = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context(prefix + "-shown-message"),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": prefix + "-shown-message",
            "displayed_text_digest": hashlib.sha256(preview["canonical_text"].encode()).hexdigest(),
            "cross_dialog": False,
        },
        prefix + "-shown-key",
    )["data"]
    assert shown["shown_message_id"] == prefix + "-shown-message"
    return preview


def confirmation_body(
    draft: dict[str, Any], preview: dict[str, Any], prefix: str, intent: str
) -> dict[str, Any]:
    return {
        "context": context(prefix + "-confirm"),
        "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"],
        "confirmation_kind": "normal",
        "confirmation_message_id": prefix + "-confirm",
        "explicit_intent": intent,
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }


def confirm(
    draft: dict[str, Any], prefix: str, intent: str = "Сохрани фактический отчёт тренировки"
) -> dict[str, Any]:
    preview = show_preview(draft, prefix)
    path = f"/api/v1/commands/previews/{preview['preview_id']}/confirm"
    body = confirmation_body(draft, preview, prefix, intent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: post(path, body, prefix + "-confirm-key")["data"], range(2))
        )
    assert len({item["commit_result_id"] for item in results}) == 1
    assert {item["status"] for item in results} == {"committed", "already_committed"}
    return next(item for item in results if item["status"] == "committed")


def create_synthetic_inventory_item(equipment_type: str) -> str:
    prefix = f"dg06-record-equipment-{equipment_type}"
    equipment = {
        "equipment_type": equipment_type,
        "name": f"Synthetic D-G06 {equipment_type}",
        "maker": None,
        "brand": None,
        "model": None,
        "quantity": None,
        "quantity_unit_code": None,
        "condition": None,
        "comment": None,
        "characteristics": {
            "mass_per_item_kg": None,
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": None,
        },
    }
    draft = post(
        "/api/v1/commands/inventory-change-drafts",
        {"context": context(prefix + "-draft"), "intent": "create", "equipment": equipment},
        prefix + "-draft-key",
    )["data"]
    committed = confirm(draft, prefix, "Сохрани синтетическую карточку оборудования")
    equipment_item = next(
        item for item in committed["created_revisions"] if item["object_type"] == "equipment_item"
    )
    return str(equipment_item["object_id"])


def row_content_snapshot(
    *, include_audit: bool = True, include_failed_operations: bool = True
) -> tuple[tuple[str, ...], ...]:
    with ENGINE.connect() as db:
        return tuple(
            tuple(
                db.execute(
                    text(
                        f"SELECT to_jsonb(item)::text FROM trainer.{table} item"
                        + (
                            " WHERE item.state <> 'failed'"
                            if table == "idempotent_operations" and not include_failed_operations
                            else ""
                        )
                        + " ORDER BY to_jsonb(item)::text"
                    )
                ).scalars()
            )
            for table in ROLLBACK_SNAPSHOT_TABLES
            if include_audit or table != "agent_operation_audit"
        )


def assert_failed_audit(correlation_id: str, method_id: str) -> None:
    with ENGINE.connect() as db:
        assert db.execute(
            text("""
            SELECT result_kind,error_code FROM trainer.agent_operation_audit
            WHERE correlation_id=:correlation AND method_id=:method
        """),
            {"correlation": correlation_id, "method": method_id},
        ).all() == [("failed", "validation_error")]


def assert_failed_operation(key: str, method_id: str, error_code: str = "validation_error") -> None:
    with ENGINE.connect() as db:
        assert db.execute(
            text("""
            SELECT state,error_code FROM trainer.idempotent_operations
            WHERE scope='local-owner:'||:method AND idempotency_key=:key
        """),
            {"method": method_id, "key": key},
        ).all() == [("failed", error_code)]


def _operation_status(locator: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    payload = {
        "context": context("qry10-locator-" + str(uuid4())),
        "locator": locator,
    }
    request = urllib.request.Request(
        BASE + "/api/v1/queries/operations/status",
        data=json.dumps(payload).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "X-Correlation-ID": str(uuid4()),
            "X-Causation-ID": str(uuid4()),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _without_request_correlation(value: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(value))
    if "meta" in normalized:
        normalized["meta"].pop("correlation_id", None)
    if "error" in normalized:
        normalized["error"].pop("correlation_id", None)
    return normalized


def assert_qry10_foreign_and_missing_are_indistinguishable() -> None:
    """Integration-only proof using one synthetic foreign-scope failed operation."""
    admin = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"])
    operation_id, audit_id, foreign_correlation = uuid4(), uuid4(), uuid4()
    foreign_key = "g03-foreign-" + str(uuid4())
    created_at = datetime.now(UTC).replace(microsecond=0)
    side_effect_tables = (
        "drafts",
        "previews",
        "confirmations",
        "commit_results",
        "domain_changes",
        "record_candidates",
        "actual_records",
    )
    try:
        with admin.begin() as db:
            db.execute(
                text("""
                INSERT INTO trainer.idempotent_operations(
                    operation_id,scope,idempotency_key,command_kind,request_digest,state,
                    lease_expires_at,commit_result_id,error_code,received_at,updated_at,created_at
                ) VALUES (:id,'synthetic-foreign-owner:CMD-00',:key,'CMD-00',:digest,'failed',
                    NULL,NULL,'synthetic_failure',:now,:now,:now)
            """),
                {
                    "id": operation_id,
                    "key": foreign_key,
                    "digest": "0" * 64,
                    "now": created_at,
                },
            )
            db.execute(
                text("""
                INSERT INTO trainer.agent_operation_audit(
                    audit_id,operation_id,method_id,object_refs,attempt_no,result_kind,
                    correlation_id,causation_id,idempotency_key_digest,error_code,
                    started_at,finished_at,latency_ms,safe_metadata,created_at
                ) VALUES (:audit,:operation,'CMD-00','[]'::jsonb,1,'failed',:correlation,
                    NULL,NULL,'synthetic_failure',:now,:now,0,'{}'::jsonb,:now)
            """),
                {
                    "audit": audit_id,
                    "operation": operation_id,
                    "correlation": foreign_correlation,
                    "now": created_at,
                },
            )

        with ENGINE.connect() as db:
            before = {
                table: db.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
                for table in side_effect_tables
            }
        locators = (
            (
                {"kind": "operation_id", "operation_id": str(operation_id)},
                {"kind": "operation_id", "operation_id": str(uuid4())},
            ),
            (
                {
                    "kind": "idempotency",
                    "method_id": "CMD-00",
                    "idempotency_key": foreign_key,
                },
                {
                    "kind": "idempotency",
                    "method_id": "CMD-00",
                    "idempotency_key": "g03-missing-" + str(uuid4()),
                },
            ),
            (
                {
                    "kind": "correlation",
                    "method_id": "CMD-00",
                    "correlation_id": str(foreign_correlation),
                },
                {
                    "kind": "correlation",
                    "method_id": "CMD-00",
                    "correlation_id": str(uuid4()),
                },
            ),
        )
        for foreign_locator, missing_locator in locators:
            foreign_status, foreign_body = _operation_status(foreign_locator)
            missing_status, missing_body = _operation_status(missing_locator)
            assert foreign_status == missing_status == 404
            assert _without_request_correlation(foreign_body) == _without_request_correlation(
                missing_body
            )
            assert foreign_body["error"]["code"] == "not_found"
            assert foreign_body["error"]["message_safe"] == "operation not found"
        with ENGINE.connect() as db:
            after = {
                table: db.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
                for table in side_effect_tables
            }
        assert after == before, ("QRY-10 read leaked domain/commit side effects", before, after)
    finally:
        with admin.begin() as db:
            db.execute(
                text("DELETE FROM trainer.agent_operation_audit WHERE audit_id=:id"),
                {"id": audit_id},
            )
            db.execute(
                text("DELETE FROM trainer.idempotent_operations WHERE operation_id=:id"),
                {"id": operation_id},
            )
        admin.dispose()


def confirm_with_rollback(draft: dict[str, Any], prefix: str) -> None:
    preview = show_preview(draft, prefix)
    payload = ConfirmRequest.model_validate(
        {
            **confirmation_body(draft, preview, prefix, "Сохрани синтетический отчёт тренировки"),
            "request_digest": "0" * 64,
        }
    ).model_dump(mode="json")
    payload.pop("request_digest")
    payload["request_digest"] = digest_payload(payload)
    before = row_content_snapshot()
    with Session(ENGINE) as session:
        try:
            ContextRepository(session).confirm(
                preview_id=UUID(preview["preview_id"]),
                payload=payload,
                key=prefix + "-rollback-confirm-key",
                correlation_id=uuid4(),
                fail_before_commit=True,
            )
        except RuntimeError as error:
            assert "synthetic fault before transaction commit" in str(error)
            session.rollback()
        else:
            raise AssertionError("injected workout confirm fault did not fire")
    assert row_content_snapshot() == before


def correction_request(
    session_ref: dict[str, str], report: dict[str, Any], prefix: str
) -> dict[str, Any]:
    corrected = json.loads(json.dumps(report))
    corrected["comment"] = (corrected.get("comment") or "") + " corrected " + prefix
    corrected["blocks"][0]["exercises"][0]["sets"][0]["full_reps"] += 1
    return {
        "context": context(prefix + "-draft"),
        "target": {"object_type": "workout_session", **session_ref},
        "patch": [{"op": "replace", "path": "/report", "value": corrected}],
        "reason": "Synthetic D-G07 correction reason",
    }


def create_synthetic_derived_values(session_ref: dict[str, str]) -> tuple[str, str]:
    admin = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"])
    affected_id, unrelated_id = uuid4(), uuid4()
    with admin.begin() as db:
        session = (
            db.execute(
                text("""
            SELECT s.profile_id,r.session_revision_id
            FROM trainer.workout_sessions s JOIN trainer.workout_session_revisions r
              ON r.session_revision_id=s.current_revision_id WHERE s.session_id=:session
        """),
                {"session": session_ref["object_id"]},
            )
            .mappings()
            .one()
        )
        unrelated = (
            db.execute(
                text("""
            SELECT s.session_id,r.session_revision_id FROM trainer.workout_sessions s
            JOIN trainer.workout_session_revisions r ON r.session_revision_id=s.current_revision_id
            WHERE s.session_id<>:session ORDER BY s.created_at LIMIT 1
        """),
                {"session": session_ref["object_id"]},
            )
            .mappings()
            .one()
        )
        db.execute(
            text("""
            INSERT INTO trainer.derived_method_catalog(code,description,seed_version,created_at)
            VALUES ('dg07_synthetic_test','D-G07 isolated invalidation fixture',1,now())
            ON CONFLICT(code) DO NOTHING
        """)
        )
        for identity, revision, value in (
            (affected_id, session["session_revision_id"], "42"),
            (unrelated_id, unrelated["session_revision_id"], "24"),
        ):
            db.execute(
                text("""
                INSERT INTO trainer.derived_values(derived_value_id,profile_id,method_id,method_version,
                    result_value,unit_code,calculated_at,assumptions,state,stale_reason,
                    supersedes_derived_value_id,persistence_reason,created_at)
                VALUES (:id,:profile,'dg07_synthetic_test','1','0', 'kilogram',now(),'{}'::jsonb,
                    'current',NULL,NULL,'history',now())
            """),
                {"id": identity, "profile": session["profile_id"]},
            )
            db.execute(
                text("""
                INSERT INTO trainer.derived_value_inputs(derived_value_id,source_kind,source_object_type,
                    source_object_id,source_revision_id,input_role,source_value,source_unit,snapshot,created_at)
                VALUES (:id,'user','workout_session',:session,:revision,'synthetic_test',:value,
                    'kilogram',jsonb_build_object('source_revision_id',:revision),now())
            """),
                {
                    "id": identity,
                    "session": session_ref["object_id"]
                    if identity == affected_id
                    else unrelated["session_id"],
                    "revision": revision,
                    "value": value,
                },
            )
    return str(affected_id), str(unrelated_id)


def run_dg07(session_ref: dict[str, str], report: dict[str, Any], plan_digest: str) -> None:
    # Advance a referenced exercise after the workout was confirmed. Correction must retain the exact old revision.
    correct_exercise(report["blocks"][2]["exercises"][1]["exercise_id"], "dg07-historical-exercise")
    invalidated_id, unrelated_id = create_synthetic_derived_values(session_ref)
    history = post(
        "/api/v1/queries/workouts/history",
        {
            "context": context("dg07-history"),
            "date_from": "2026-09-01",
            "date_to": "2026-09-30",
            "limit": 1,
        },
    )["data"]
    assert history["snapshot_id"] is None
    assert history["snapshot_confirmed_at"] is None
    assert history["total_count"] is None
    history_items = list(history["items"])
    cursor = history["next_cursor"]
    while cursor:
        page = post(
            "/api/v1/queries/workouts/history",
            {
                "context": context("dg07-history-page" + str(len(history_items))),
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
                "limit": 1,
                "cursor": cursor,
            },
        )["data"]
        history_items.extend(page["items"])
        cursor = page["next_cursor"]
    original = next(
        item
        for item in history_items
        if item["session_ref"]["revision_id"] == session_ref["revision_id"]
    )
    assert original["current"] and original["plan_content_digest"] == plan_digest
    assert original["workout"]["blocks"] and original["provenance"]
    replaced_exercise_id = report["blocks"][2]["exercises"][1]["exercise_id"]
    historical_exercise_ref = next(
        item["ref"]
        for item in original["provenance"]
        if item["kind"] == "exercise_revision" and item["ref"]["object_id"] == replaced_exercise_id
    )

    body = correction_request(session_ref, report, "dg07-correction")
    invalid_date = json.loads(json.dumps(body))
    invalid_date["patch"][0]["value"]["local_start_date"] = "2026-09-22"
    invalid = post(
        "/api/v1/commands/correction-drafts",
        invalid_date,
        "dg07-invalid-local-date-key",
        expected=422,
    )
    assert invalid["error"]["code"] == "validation_error"
    assert post(
        "/api/v1/queries/workouts/history",
        {
            "context": context("dg07-history-after-invalid"),
            "date_from": "2026-09-01",
            "date_to": "2026-09-30",
            "limit": 100,
        },
    )["data"]["items"]
    draft = post("/api/v1/commands/correction-drafts", body, "dg07-correction-key")["data"]
    replay = post("/api/v1/commands/correction-drafts", body, "dg07-correction-key")["data"]
    assert replay["revision_id"] == draft["revision_id"]
    preview = show_preview(draft, "dg07-correction")
    preview_text = preview["canonical_text"]
    assert "Текущее состояние" in preview_text and "Новое состояние" in preview_text
    before_reps = preview["canonical_payload"]["correction"]["before"]["blocks"][0]["exercises"][0][
        "sets"
    ][0]["full_reps"]
    after_reps = preview["canonical_payload"]["report"]["blocks"][0]["exercises"][0]["sets"][0][
        "full_reps"
    ]
    assert after_reps == before_reps + 1
    before_text = preview_text.split("Текущее состояние:", 1)[1].split("Новое состояние:", 1)[0]
    after_text = preview_text.split("Новое состояние:", 1)[1]
    assert f"Full reps: {before_reps}" in before_text
    assert f"Full reps: {after_reps}" in after_text
    assert "Synthetic D-G07 correction reason" in preview_text
    assert "Зависимые расчёты" in preview_text
    assert "сохранённый расчёт" in preview_text
    assert invalidated_id not in preview_text
    assert draft["gaps"]
    assert preview["canonical_payload"]["gaps"] == draft["gaps"]
    assert "Неизвестные значения и их решения" in preview_text
    for gap in draft["gaps"]:
        assert gap["message_safe"] in preview_text
        assert gap["path"].startswith("/report/")
        assert gap["disposition"] == "leave_unknown"
    rollback_draft = post(
        "/api/v1/commands/correction-drafts",
        correction_request(session_ref, report, "dg07-rollback"),
        "dg07-rollback-key",
    )["data"]
    confirm_with_rollback(rollback_draft, "dg07-correction-rollback")

    stale_body = correction_request(session_ref, report, "dg07-stale")
    stale_draft = post("/api/v1/commands/correction-drafts", stale_body, "dg07-stale-key")["data"]
    stale_preview = show_preview(stale_draft, "dg07-stale")
    confirmed = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        confirmation_body(draft, preview, "dg07-correction", "Подтверждаю исправление факта"),
        "dg07-correction-confirm-key",
    )["data"]
    replay = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        confirmation_body(draft, preview, "dg07-correction", "Подтверждаю исправление факта"),
        "dg07-correction-confirm-key",
    )["data"]
    assert replay["commit_result_id"] == confirmed["commit_result_id"]
    status = post(
        "/api/v1/queries/operations/status",
        {
            "context": context("dg07-operation-status"),
            "locator": {
                "kind": "idempotency",
                "method_id": "CMD-00",
                "idempotency_key": "dg07-correction-confirm-key",
            },
        },
    )["data"]
    assert status["state"] == "committed"
    assert status["commit_result"]["commit_result_id"] == confirmed["commit_result_id"]
    assert any(
        item["object_id"] == invalidated_id
        for item in status["commit_result"]["invalidated_calculations"]
    )
    assert any(
        item["object_id"] == invalidated_id for item in confirmed["invalidated_calculations"]
    )
    with ENGINE.connect() as db:
        first_corrected_revision_id = str(
            db.execute(
                text(
                    "SELECT current_revision_id FROM trainer.workout_sessions WHERE session_id=:id"
                ),
                {"id": session_ref["object_id"]},
            ).scalar_one()
        )
    snapshot_page = post(
        "/api/v1/queries/workouts/history",
        {"context": context("dg07-all-history-first"), "mode": "all_confirmed", "limit": 1},
    )["data"]
    assert snapshot_page["total_count"] > len(snapshot_page["items"])
    snapshot_id = snapshot_page["snapshot_id"]
    snapshot_time = snapshot_page["snapshot_confirmed_at"]
    snapshot_total = snapshot_page["total_count"]
    snapshot_items = list(snapshot_page["items"])
    snapshot_cursor = snapshot_page["next_cursor"]

    # Change the current pointer after page one; the remaining pages must retain the old snapshot.
    next_draft = post(
        "/api/v1/commands/correction-drafts",
        correction_request(
            {"object_id": session_ref["object_id"], "revision_id": first_corrected_revision_id},
            report,
            "dg07-after-history-snapshot",
        ),
        "dg07-after-history-snapshot-key",
    )["data"]
    confirm(next_draft, "dg07-after-history-snapshot")

    seen_cursors: set[str] = set()
    while snapshot_cursor:
        assert snapshot_cursor not in seen_cursors
        seen_cursors.add(snapshot_cursor)
        page = post(
            "/api/v1/queries/workouts/history",
            {
                "context": context("dg07-all-history-page" + str(len(snapshot_items))),
                "mode": "all_confirmed",
                "limit": 1,
                "cursor": snapshot_cursor,
            },
        )["data"]
        assert page["snapshot_id"] == snapshot_id
        assert page["snapshot_confirmed_at"] == snapshot_time
        assert page["total_count"] == snapshot_total
        snapshot_items.extend(page["items"])
        snapshot_cursor = page["next_cursor"]
    snapshot_pairs = [
        (item["session_ref"]["object_id"], item["session_ref"]["revision_id"])
        for item in snapshot_items
    ]
    assert len(snapshot_pairs) == len(set(snapshot_pairs)) == snapshot_total
    pinned_current = next(
        item
        for item in snapshot_items
        if item["session_ref"]["object_id"] == session_ref["object_id"]
        and item["session_ref"]["revision_id"] == first_corrected_revision_id
    )
    assert pinned_current["current"]
    with ENGINE.connect() as db:
        latest_revision_id = str(
            db.execute(
                text(
                    "SELECT current_revision_id FROM trainer.workout_sessions WHERE session_id=:id"
                ),
                {"id": session_ref["object_id"]},
            ).scalar_one()
        )
    assert latest_revision_id != first_corrected_revision_id
    assert all(item["session_ref"]["revision_id"] != latest_revision_id for item in snapshot_items)

    error = post(
        f"/api/v1/commands/previews/{stale_preview['preview_id']}/confirm",
        confirmation_body(
            stale_draft, stale_preview, "dg07-stale", "Подтверждаю устаревшее исправление"
        ),
        "dg07-stale-confirm-key",
        expected=409,
    )
    assert error["error"]["code"] == "stale_preview"
    failed_status = post(
        "/api/v1/queries/operations/status",
        {
            "context": context("dg07-failed-status"),
            "locator": {
                "kind": "idempotency",
                "method_id": "CMD-00",
                "idempotency_key": "dg07-stale-confirm-key",
            },
        },
    )["data"]
    assert failed_status["state"] == "failed" and failed_status["error"]["code"] == "stale_preview"
    updated_history = post(
        "/api/v1/queries/workouts/history",
        {
            "context": context("dg07-history-after"),
            "date_from": "2026-09-01",
            "date_to": "2026-09-30",
            "limit": 100,
        },
    )["data"]["items"]
    current = next(
        item
        for item in updated_history
        if item["current"] and item["session_ref"]["object_id"] == session_ref["object_id"]
    )
    previous = next(
        item
        for item in updated_history
        if item["session_ref"]["revision_id"] == session_ref["revision_id"]
    )
    first_correction = next(
        item
        for item in updated_history
        if item["session_ref"]["revision_id"] == first_corrected_revision_id
    )
    assert current["session_ref"]["revision_id"] != session_ref["revision_id"]
    assert current["session_ref"]["revision_id"] == latest_revision_id
    assert current["supersedes_revision_ref"]["revision_id"] == first_corrected_revision_id
    assert previous["workout"] == original["workout"] and not previous["current"]
    assert not first_correction["current"]
    assert current["plan_content_digest"] == plan_digest
    corrected_exercise_ref = next(
        item["ref"]
        for item in current["provenance"]
        if item["kind"] == "exercise_revision" and item["ref"]["object_id"] == replaced_exercise_id
    )
    assert corrected_exercise_ref["revision_id"] == historical_exercise_ref["revision_id"]
    with ENGINE.connect() as db:
        states = dict(
            db.execute(
                text("""
            SELECT derived_value_id::text,state FROM trainer.derived_values
            WHERE derived_value_id IN (:affected,:unrelated)
        """),
                {"affected": invalidated_id, "unrelated": unrelated_id},
            ).all()
        )
    assert states == {invalidated_id: "stale", unrelated_id: "current"}
    assert current["workout"]["blocks"][0]["exercises"][0]["sets"][0]["full_reps"] == (
        original["workout"]["blocks"][0]["exercises"][0]["sets"][0]["full_reps"] + 1
    )
    print("PASS: D-G07 history, correction, stale preview and QRY-10 recovery")


def assert_workout_immutable(session_revision_id: str) -> None:
    before = row_content_snapshot()
    with Session(ENGINE) as session:
        try:
            session.execute(
                text("""
                INSERT INTO trainer.actual_blocks(actual_block_id,session_revision_id,planned_block_id,
                    position,block_type,declared_rounds,rest_seconds,comment,created_at)
                VALUES (:id,:revision,NULL,99,'straight',NULL,NULL,NULL,now())
            """),
                {"id": uuid4(), "revision": session_revision_id},
            )
            session.commit()
        except IntegrityError as error:
            assert "confirmed workout tree cannot be extended" in str(error.orig)
            session.rollback()
        else:
            raise AssertionError("database accepted a late workout child insert")
    assert row_content_snapshot() == before


def correct_exercise(exercise_id: str, prefix: str) -> None:
    current = post(
        "/api/v1/queries/exercises/search",
        {
            "context": context(prefix + "-exact"),
            "mode": "exact",
            "exercise_id": exercise_id,
            "revision_id": None,
        },
    )["data"]["aggregate"]
    exercise = {
        "exercise_id": exercise_id,
        "canonical_name": current["canonical_name"],
        "description": current["description"],
        "category_code": current["category_code"],
        "unilateral": current["unilateral"],
        "status_code": current["status_code"],
        "comment": (current["comment"] or "") + " D-G06 dependency correction",
        "aliases": current["aliases"],
        "muscle_roles": [
            {
                "kind": "existing",
                "muscle_group_id": role["muscle_group"]["muscle_group_id"],
                "role": role["role"],
            }
            for role in current["muscle_roles"]
        ],
        "measurements": current["measurements"],
        "equipment_requirements": current["equipment_requirements"],
    }
    draft = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context(prefix + "-draft"),
            "intent": "correct",
            "exercise": exercise,
            "expected_revision_id": current["exercise_ref"]["revision_id"],
            "reason": "D-G06 stale dependency regression",
        },
        prefix + "-draft-key",
    )["data"]
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        prefix + "-preview-key",
    )["data"]
    shown_message = prefix + "-shown-message"
    post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context(shown_message),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": shown_message,
            "displayed_text_digest": hashlib.sha256(preview["canonical_text"].encode()).hexdigest(),
            "cross_dialog": False,
        },
        prefix + "-shown-key",
    )
    body = {
        "context": context(prefix + "-confirm"),
        "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"],
        "confirmation_kind": "normal",
        "confirmation_message_id": prefix + "-confirm",
        "explicit_intent": "Подтверждаю синтетическую коррекцию",
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }
    post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm", body, prefix + "-confirm-key"
    )
    revised = post(
        "/api/v1/queries/exercises/search",
        {
            "context": context(prefix + "-exact-after"),
            "mode": "exact",
            "exercise_id": exercise_id,
            "revision_id": None,
        },
    )["data"]["aggregate"]
    assert revised["exercise_ref"]["revision_id"] != current["exercise_ref"]["revision_id"]


def metric_target(metric_kind: str, value: str, unit_code: str) -> dict[str, Any]:
    return {
        "metric_kind": metric_kind,
        "exact_value": value,
        "min_value": None,
        "max_value": None,
        "unit_code": unit_code,
        "custom_definition": None,
    }


def planned_set(
    position: int,
    metric_kind: str,
    value: str,
    unit_code: str,
    *,
    side: str | None = "both",
    logical_set_no: int | None = None,
    round_no: int | None = None,
    loads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "position": position,
        "round_no": round_no,
        "logical_set_no": logical_set_no or position,
        "side": side,
        "set_type_code": "working",
        "target_rpe_min": None,
        "target_rpe_max": None,
        "target_rir_min": None,
        "target_rir_max": None,
        "tempo": None,
        "pause_seconds": None,
        "rest_after_seconds": None,
        "metrics": [metric_target(metric_kind, value, unit_code)],
        "loads": loads or [],
    }


def create_dg06_plan(exercise_ids: dict[str, str], g02_pack: dict[str, Any]) -> dict[str, Any]:
    planned_cardio = next(
        block for block in g02_pack["plan"]["input"]["blocks"] if block["name"] == "Cardio"
    )["exercises"][0]["sets"][0]
    planned_cardio_metrics = planned_cardio["metrics"]
    cardio_plan_set = planned_set(
        1,
        planned_cardio_metrics[0]["metric_kind"],
        planned_cardio_metrics[0]["exact_value"],
        planned_cardio_metrics[0]["unit_code"],
    )
    cardio_plan_set["metrics"].extend(
        metric_target(metric["metric_kind"], metric["exact_value"], metric["unit_code"])
        for metric in planned_cardio_metrics[1:]
    )
    plan = {
        "planned_date": (datetime.now(UTC).date() + timedelta(days=1)).isoformat(),
        "planned_time": "18:00:00",
        "program_id": None,
        "program_version_id": None,
        "day_template_id": None,
        "plan_type": "training",
        "name": "Deterministic D-G06 sports fixture",
        "purpose": "Synthetic coverage for D-500",
        "comment": None,
        "temporary_conditions": {
            "place": {"kind": "ad_hoc", "name": "D-G06 synthetic gym", "place_type": "home_gym"},
            "equipment_additions": ["barbell"],
            "equipment_removals": [],
        },
        "blocks": [
            {
                "position": 1,
                "block_type_code": "straight",
                "name": "Barbell and dumbbell work",
                "declared_rounds": None,
                "rest_seconds": 120,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": exercise_ids["bench"],
                        "circuit_position": None,
                        "comment": None,
                        "sets": [
                            planned_set(i, "repetitions", str(value), "repetition")
                            for i, value in enumerate((8, 6, 4, 3, 2), 1)
                        ],
                    },
                    {
                        "position": 2,
                        "exercise_id": exercise_ids["dumbbell"],
                        "circuit_position": None,
                        "comment": None,
                        "sets": [
                            planned_set(
                                1,
                                "repetitions",
                                "10",
                                "repetition",
                                loads=[
                                    {
                                        "position": 1,
                                        "load_kind": "external",
                                        "equipment_id": None,
                                        "item_count": "2",
                                        "mass": "10",
                                        "unit_code": "kilogram",
                                        "mass_meaning": "per_item_mass",
                                        "direction": None,
                                        "raw_ambiguous_text": None,
                                    }
                                ],
                            )
                        ],
                    },
                    {
                        "position": 3,
                        "exercise_id": exercise_ids["unilateral"],
                        "circuit_position": None,
                        "comment": None,
                        "sets": [
                            planned_set(
                                1, "repetitions", "10", "repetition", side="left", logical_set_no=1
                            ),
                            planned_set(
                                2, "repetitions", "10", "repetition", side="right", logical_set_no=1
                            ),
                            planned_set(
                                3,
                                "repetitions",
                                "7",
                                "repetition",
                                side="left",
                                logical_set_no=2,
                                loads=[{"position": 1, "load_kind": "bodyweight"}],
                            ),
                        ],
                    },
                ],
            },
            {
                "position": 2,
                "block_type_code": "superset",
                "name": "Pull-up and timed hold",
                "declared_rounds": 1,
                "rest_seconds": 90,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": exercise_ids["pullup"],
                        "circuit_position": 1,
                        "comment": None,
                        "sets": [planned_set(1, "repetitions", "8", "repetition", round_no=1)],
                    },
                    {
                        "position": 2,
                        "exercise_id": exercise_ids["timed"],
                        "circuit_position": 2,
                        "comment": None,
                        "sets": [
                            planned_set(1, "duration", "30", "second", round_no=1),
                            planned_set(2, "duration", "30", "second", round_no=1),
                        ],
                    },
                ],
            },
            {
                "position": 3,
                "block_type_code": "straight",
                "name": "Replacement source",
                "declared_rounds": None,
                "rest_seconds": None,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": exercise_ids["source"],
                        "circuit_position": None,
                        "comment": None,
                        "sets": [planned_set(1, "repetitions", "8", "repetition")],
                    },
                ],
            },
            {
                "position": 4,
                "block_type_code": "circuit",
                "name": "Circuit round",
                "declared_rounds": 1,
                "rest_seconds": 60,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": exercise_ids["circuit_lunge"],
                        "circuit_position": 1,
                        "comment": None,
                        "sets": [planned_set(1, "repetitions", "10", "repetition", round_no=1)],
                    },
                    {
                        "position": 2,
                        "exercise_id": exercise_ids["circuit_pushup"],
                        "circuit_position": 2,
                        "comment": None,
                        "sets": [planned_set(1, "repetitions", "10", "repetition", round_no=1)],
                    },
                ],
            },
            {
                "position": 5,
                "block_type_code": "straight",
                "name": "G02 fixed-distance cardio",
                "declared_rounds": None,
                "rest_seconds": None,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": exercise_ids["cardio"],
                        "circuit_position": None,
                        "comment": None,
                        "sets": [cardio_plan_set],
                    }
                ],
            },
        ],
    }
    draft = post(
        "/api/v1/commands/plan-drafts",
        {
            "context": context("dg06-plan-draft"),
            "plan": plan,
            "recommendations": insufficient_recommendations(ENGINE),
        },
        "dg06-plan-draft-key",
    )["data"]
    assert not draft["gaps"], draft
    committed = confirm(draft, "dg06-plan", "Сохрани синтетический план D-G06")
    plan_ref = next(
        item for item in committed["created_revisions"] if item["object_type"] == "workout_plan"
    )
    with ENGINE.connect() as db:
        plan_row = (
            db.execute(
                text("""
            SELECT p.profile_id,r.plan_revision_id FROM trainer.workout_plans p
            JOIN trainer.workout_plan_revisions r ON r.plan_id=p.plan_id AND r.plan_revision_id=p.current_revision_id
            WHERE p.plan_id=:id
        """),
                {"id": plan_ref["object_id"]},
            )
            .mappings()
            .one()
        )
        block_rows = list(
            db.execute(
                text("""
            SELECT planned_block_id,position,block_type FROM trainer.planned_blocks
            WHERE plan_revision_id=:revision ORDER BY position
        """),
                {"revision": plan_row["plan_revision_id"]},
            ).mappings()
        )
        exercise_rows = list(
            db.execute(
                text("""
            SELECT pe.planned_exercise_id,pe.exercise_id,pe.position,b.planned_block_id,b.position AS block_position,
                   er.canonical_name
            FROM trainer.planned_exercises pe JOIN trainer.planned_blocks b USING(planned_block_id)
            JOIN trainer.exercises e USING(exercise_id)
            JOIN trainer.exercise_revisions er ON er.exercise_revision_id=e.current_revision_id
            WHERE b.plan_revision_id=:revision ORDER BY b.position,pe.position
        """),
                {"revision": plan_row["plan_revision_id"]},
            ).mappings()
        )
        set_rows = list(
            db.execute(
                text("""
            SELECT s.planned_set_id,s.planned_exercise_id,s.position,s.logical_set_no,s.side,s.round_no
            FROM trainer.planned_sets s
            JOIN trainer.planned_exercises pe USING(planned_exercise_id)
            JOIN trainer.planned_blocks b USING(planned_block_id)
            WHERE b.plan_revision_id=:revision ORDER BY s.position
        """),
                {"revision": plan_row["plan_revision_id"]},
            ).mappings()
        )
        load_rows = list(
            db.execute(
                text("""
            SELECT l.planned_load_component_id,l.planned_set_id,l.position
            FROM trainer.planned_load_components l JOIN trainer.planned_sets s USING(planned_set_id)
            JOIN trainer.planned_exercises pe USING(planned_exercise_id)
            JOIN trainer.planned_blocks b USING(planned_block_id)
            WHERE b.plan_revision_id=:revision ORDER BY l.position
        """),
                {"revision": plan_row["plan_revision_id"]},
            ).mappings()
        )
    by_name = {
        row["canonical_name"]: {
            "planned_exercise_id": str(row["planned_exercise_id"]),
            "exercise_id": str(row["exercise_id"]),
            "planned_block_id": str(row["planned_block_id"]),
            "block_position": row["block_position"],
            "position": row["position"],
            "sets": [],
        }
        for row in exercise_rows
    }
    for row in set_rows:
        for exercise in by_name.values():
            if exercise["planned_exercise_id"] == str(row["planned_exercise_id"]):
                exercise["sets"].append(
                    {
                        "planned_set_id": str(row["planned_set_id"]),
                        "position": row["position"],
                        "logical_set_no": row["logical_set_no"],
                        "side": row["side"],
                        "round_no": row["round_no"],
                    }
                )
                break
    for row in load_rows:
        for exercise in by_name.values():
            for planned_row in exercise["sets"]:
                if planned_row["planned_set_id"] == str(row["planned_set_id"]):
                    planned_row.setdefault("loads", []).append(
                        {
                            "planned_load_component_id": str(row["planned_load_component_id"]),
                            "position": row["position"],
                        }
                    )
                    break
    return {
        "plan_id": str(plan_ref["object_id"]),
        "plan_revision_id": str(plan_row["plan_revision_id"]),
        "profile_id": str(plan_row["profile_id"]),
        "blocks": {
            row["position"]: {
                "planned_block_id": str(row["planned_block_id"]),
                "block_type": row["block_type"],
            }
            for row in block_rows
        },
        "exercises": by_name,
    }


def main() -> None:
    assert_qry10_foreign_and_missing_are_indistinguishable()
    g02_pack = json.loads(Path("/g02-fixtures/g02_synthetic_pack.json").read_text(encoding="utf-8"))
    assert g02_pack["classification"] == "synthetic_test_data_only"
    barbell_id = create_synthetic_inventory_item("barbell")
    resistance_band_id = create_synthetic_inventory_item("resistance_band")

    with ENGINE.connect() as db:
        exercise_rows = (
            db.execute(
                text("""
            SELECT e.exercise_id,r.canonical_name,r.exercise_revision_id
            FROM trainer.exercises e JOIN trainer.exercise_revisions r
              ON r.exercise_revision_id=e.current_revision_id
            WHERE e.archived_at IS NULL AND e.logically_deleted_at IS NULL
              AND r.status_code='active'
        """)
            )
            .mappings()
            .all()
        )
        exercise_ids = {row["canonical_name"]: str(row["exercise_id"]) for row in exercise_rows}
        required_names = {
            "bench": "Жим лёжа",
            "pullup": "Synthetic pull-up",
            "unilateral": "Synthetic unilateral curl",
            "timed": "Synthetic timed hold",
            "cardio": "Synthetic fixed-distance cardio",
            "dumbbell": "Synthetic dumbbell press",
            "source": "Synthetic replacement source",
            "replacement": "Synthetic replacement exercise",
            "circuit_lunge": "Synthetic circuit lunge",
            "circuit_pushup": "Synthetic circuit push-up",
        }
        assert all(name in exercise_ids for name in required_names.values()), required_names
        fixture_plan = create_dg06_plan(
            {key: exercise_ids[name] for key, name in required_names.items()}, g02_pack
        )
        plan_row = (
            db.execute(
                text("""
            SELECT context_snapshot FROM trainer.workout_plan_revisions
            WHERE plan_revision_id=:revision
        """),
                {"revision": fixture_plan["plan_revision_id"]},
            )
            .mappings()
            .one()
        )
        exercise_revision_ids = {
            item["object_id"]: item["revision_id"]
            for item in plan_row["context_snapshot"]["dependencies"]
            if item["dependency_type"] == "exercise"
        }
        plan_digest = db.execute(
            text("""
            SELECT content_digest FROM trainer.workout_plan_revisions WHERE plan_revision_id=:revision
        """),
            {"revision": fixture_plan["plan_revision_id"]},
        ).scalar_one()

    by_name = fixture_plan["exercises"]
    named = {key: by_name[name] for key, name in required_names.items() if key != "replacement"}
    cardio_input = next(
        block
        for block in g02_pack["workout_report"]["input"]["blocks"]
        if block["block_type_code"] == "straight"
        and block["exercises"][0]["exercise_id"]
        == g02_pack["exercise_refs"]["treadmill"]["object_id"]
    )["exercises"][0]["sets"][0]
    cardio_metrics = cardio_input["metrics"]
    blocks = []

    def actual_set(
        position: int,
        planned: dict[str, Any],
        *,
        reps: int | None,
        status: str = "performed",
        partial_reps: int | None = None,
        loads: list[dict[str, Any]] | None = None,
        side: str | None = None,
        round_no: int | None = None,
        metrics: list[dict[str, Any]] | None = None,
        reported_rpe: str | None = None,
        reported_rir: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "position": position,
            "planned_set_id": planned["planned_set_id"],
            "logical_set_no": planned["logical_set_no"],
            "set_type_code": "working",
            "status": status,
            "metrics": metrics or [],
            "loads": loads or [],
        }
        if reps is not None:
            result["full_reps"] = reps
        if partial_reps is not None:
            result["partial_reps"] = partial_reps
        if side is not None:
            result["side"] = side
        if round_no is not None:
            result["round_no"] = round_no
        if reported_rpe is not None:
            result["reported_rpe"] = reported_rpe
        if reported_rir is not None:
            result["reported_rir"] = reported_rir
        return result

    for block_position, block in fixture_plan["blocks"].items():
        entries: list[dict[str, Any]] = []
        plan_exercises = sorted(
            (
                (name, item)
                for name, item in by_name.items()
                if item["block_position"] == block_position
            ),
            key=lambda pair: pair[1]["position"],
        )
        for name, item in plan_exercises:
            key = next(k for k, canonical in required_names.items() if canonical == name)
            if key == "source":
                entries.append(
                    {
                        "position": item["position"],
                        "exercise_id": item["exercise_id"],
                        "planned_exercise_id": item["planned_exercise_id"],
                        "result": "skipped",
                        "reason": "replacement",
                        "sets": [],
                    }
                )
                continue
            planned_sets = sorted(item["sets"], key=lambda row: row["position"])
            sets: list[dict[str, Any]] = []
            for position, planned in enumerate(planned_sets, 1):
                loads = []
                reps = 5
                status = "performed"
                side = None
                round_no = 1 if block["block_type"] in {"superset", "circuit"} else None
                if key == "bench":
                    reps = (8, 6, 4, 0, None)[position - 1]
                    status = (
                        "skipped"
                        if reps is None
                        else "interrupted"
                        if position == 4
                        else "performed"
                    )
                    if reps is not None:
                        loads = [
                            {
                                "position": 1,
                                "load_kind": "external",
                                "equipment_id": barbell_id,
                                "item_count": 1,
                                "mass": "20",
                                "unit_code": "kilogram",
                                "mass_meaning": "total_external_mass",
                            }
                        ]
                elif key == "dumbbell":
                    reps = 10
                    loads = [
                        {
                            "position": 1,
                            "planned_load_component_id": planned["loads"][0][
                                "planned_load_component_id"
                            ],
                            "load_kind": "external",
                            "item_count": 2,
                            "mass": "8",
                            "unit_code": "kilogram",
                            "mass_meaning": "per_item_mass",
                        }
                    ]
                elif key == "unilateral":
                    reps = (10, 8, 7)[position - 1]
                    side = "left" if position in (1, 3) else "right"
                    loads = (
                        [
                            {
                                "position": 1,
                                "planned_load_component_id": planned["loads"][0][
                                    "planned_load_component_id"
                                ],
                                "load_kind": "bodyweight",
                            }
                        ]
                        if position == 3
                        else [
                            {
                                "position": 1,
                                "load_kind": "external",
                                "item_count": 1,
                                "mass": "10",
                                "unit_code": "kilogram",
                                "mass_meaning": "per_item_mass",
                            }
                        ]
                    )
                elif key == "pullup":
                    reps = 5
                    loads = [{"position": 1, "load_kind": "bodyweight"}]
                    round_no = 1
                elif key == "cardio":
                    reps = None
                    metrics = cardio_metrics
                elif key == "timed":
                    reps = None
                    round_no = 1
                    loads = [{"position": 1, "load_kind": "bodyweight"}] if position == 1 else []
                    metrics = (
                        [
                            {
                                "metric_kind": "duration",
                                "value": "30",
                                "unit_code": "second",
                                "source_kind": "user",
                            }
                        ]
                        if position == 1
                        else []
                    )
                executed = actual_set(
                    position,
                    planned,
                    reps=reps,
                    status=status,
                    partial_reps=2 if key == "bench" and position == 3 else None,
                    loads=loads,
                    side=side,
                    round_no=round_no,
                    metrics=metrics if key in {"timed", "cardio"} else None,
                    reported_rpe="8" if key == "bench" and position == 1 else None,
                    reported_rir="2" if key == "dumbbell" and position == 1 else None,
                )
                sets.append(executed)
            result = "partial" if key in {"bench", "unilateral"} else "performed"
            entries.append(
                {
                    "position": item["position"],
                    "exercise_id": item["exercise_id"],
                    "planned_exercise_id": item["planned_exercise_id"],
                    "result": result,
                    "reason": None,
                    "sets": sets,
                }
            )
        if block_position == 3:
            entries.append(
                {
                    "position": 2,
                    "exercise_id": exercise_ids[required_names["replacement"]],
                    "planned_exercise_id": None,
                    "result": "added",
                    "replacement_source": {
                        "kind": "skipped_execution",
                        "block_position": 3,
                        "exercise_position": 1,
                    },
                    "reason": "synthetic replacement",
                    "sets": [
                        {
                            "position": 1,
                            "logical_set_no": 1,
                            "set_type_code": "working",
                            "full_reps": 5,
                            "status": "performed",
                            "metrics": [],
                            "loads": [],
                        }
                    ],
                }
            )
        blocks.append(
            {
                "position": block_position,
                "planned_block_id": block["planned_block_id"],
                "block_type_code": block["block_type"],
                "declared_rounds": 1 if block_position in {2, 4} else None,
                "rest_seconds": None,
                "comment": None,
                "exercises": entries,
            }
        )

    start = datetime(2026, 9, 23, 16, 30, tzinfo=UTC)
    report = {
        "plan_id": fixture_plan["plan_id"],
        "plan_revision_id": fixture_plan["plan_revision_id"],
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(hours=1)).isoformat(),
        "local_start_date": "2026-09-23",
        "duration_seconds": None,
        "session_type": "training",
        "name": "Synthetic D-G06 planned workout",
        "lifecycle": "completed",
        "effort": "8",
        "comment": "multiple loads, sides, skipped/interrupted sets and required unknown duration",
        "workout_metrics": [
            {
                "metric_kind": "heart_rate_min",
                "value": "80",
                "unit_code": "beats_per_minute",
                "source_kind": "user",
            },
            {
                "metric_kind": "heart_rate_avg",
                "value": "120",
                "unit_code": "beats_per_minute",
                "source_kind": "user",
            },
            {
                "metric_kind": "heart_rate_max",
                "value": "160",
                "unit_code": "beats_per_minute",
                "source_kind": "user",
            },
            {
                "metric_kind": "calories_active",
                "value": "400",
                "unit_code": "kilocalorie",
                "source_kind": "user",
            },
        ],
        "blocks": blocks,
        "confirmed_unknown_planned_exercise_ids": [],
        "confirmed_unknown_planned_set_ids": [],
        "confirmed_unknown_planned_load_component_ids": [],
        "confirmed_unknown_metrics": [
            {
                "block_position": 2,
                "exercise_position": 2,
                "set_position": 2,
                "metric_kind": "duration",
            }
        ],
    }
    draft_request = {"context": context("workout-draft"), "report": report}
    draft = post("/api/v1/commands/workout-report-drafts", draft_request, "workout-draft-key")[
        "data"
    ]
    assert draft["gaps"] and all(gap["disposition"] == "leave_unknown" for gap in draft["gaps"]), (
        draft
    )
    assert (
        post("/api/v1/commands/workout-report-drafts", draft_request, "workout-draft-key")["data"]
        == draft
    )

    # CMD-19 replaces the complete report in the same open draft and invalidates old preview.
    old_preview = show_preview(draft, "workout-revision-old")
    with ENGINE.connect() as db:
        original_draft = (
            db.execute(
                text("SELECT expires_at FROM trainer.drafts WHERE draft_id=:draft"),
                {"draft": draft["draft_id"]},
            )
            .mappings()
            .one()
        )
        workouts_before_revision = db.execute(
            text("SELECT count(*) FROM trainer.workout_sessions")
        ).scalar_one()
    revision_request = {
        "context": context("workout-revision-new"),
        "base_revision_id": draft["revision_id"],
        "changes": [{"op": "replace", "path": "/report", "value": report}],
    }
    revision_request["request_digest"] = digest_payload(revision_request)
    revised_draft = post(
        f"/api/v1/commands/drafts/{draft['draft_id']}/revisions",
        revision_request,
        "workout-revision-key",
    )["data"]
    assert revised_draft["draft_id"] == draft["draft_id"]
    assert revised_draft["revision_no"] == 2
    assert (
        post(
            f"/api/v1/commands/drafts/{draft['draft_id']}/revisions",
            revision_request,
            "workout-revision-key",
        )["data"]
        == revised_draft
    )
    stale_shown = post(
        f"/api/v1/commands/previews/{old_preview['preview_id']}/shown",
        {
            "context": context("workout-revision-stale-shown"),
            "revision_id": draft["revision_id"],
            "preview_digest": old_preview["digest"],
            "shown_message_id": "workout-revision-stale-shown",
            "displayed_text_digest": hashlib.sha256(
                old_preview["canonical_text"].encode()
            ).hexdigest(),
            "cross_dialog": False,
        },
        "workout-revision-stale-shown-key",
        expected=409,
    )
    assert stale_shown["error"]["code"] == "stale_preview"
    stale_confirm = post(
        f"/api/v1/commands/previews/{old_preview['preview_id']}/confirm",
        confirmation_body(draft, old_preview, "workout-revision-old", "Сохрани отчёт"),
        "workout-revision-old-confirm",
        expected=409,
    )
    assert stale_confirm["error"]["code"] == "stale_preview"
    with ENGINE.connect() as db:
        revisions = (
            db.execute(
                text("""
                SELECT dr.draft_revision_id,dr.revision_no,dr.content_digest,
                       dr.typed_payload->>'session_id' AS session_id,
                       d.latest_revision_id,d.expires_at
                FROM trainer.draft_revisions dr
                JOIN trainer.drafts d USING(draft_id)
                WHERE dr.draft_id=:draft ORDER BY dr.revision_no
            """),
                {"draft": draft["draft_id"]},
            )
            .mappings()
            .all()
        )
        workouts_after_revision = db.execute(
            text("SELECT count(*) FROM trainer.workout_sessions")
        ).scalar_one()
    assert len(revisions) == 2
    assert [row["revision_no"] for row in revisions] == [1, 2]
    assert revisions[0]["session_id"] == revisions[1]["session_id"]
    assert str(revisions[1]["draft_revision_id"]) == str(revisions[1]["latest_revision_id"])
    assert revisions[0]["content_digest"] != revisions[1]["content_digest"]
    assert revisions[0]["expires_at"] == original_draft["expires_at"]
    assert revisions[1]["expires_at"] == original_draft["expires_at"]
    assert workouts_after_revision == workouts_before_revision
    draft = revised_draft

    # The skipped replacement source cannot also be reported as performed.
    direct_plan_link_report = json.loads(json.dumps(report))
    direct_plan_link_report["blocks"][2]["exercises"] = [
        {
            "position": 2,
            "exercise_id": exercise_ids[required_names["replacement"]],
            "planned_exercise_id": None,
            "result": "added",
            "replacement_source": {
                "kind": "planned_exercise",
                "planned_exercise_id": named["source"]["planned_exercise_id"],
            },
            "reason": "synthetic replacement",
            "sets": report["blocks"][2]["exercises"][1]["sets"],
        }
    ]
    before_direct_plan_link_error = row_content_snapshot(include_audit=False)
    post(
        "/api/v1/commands/workout-report-drafts",
        {
            "context": context("workout-direct-plan-link"),
            "report": direct_plan_link_report,
            "request_digest": "0" * 64,
        },
        "workout-direct-plan-link-key",
        expected=400,
        compute_digest=False,
    )
    assert row_content_snapshot(include_audit=False) == before_direct_plan_link_error

    conflicting_report = json.loads(json.dumps(report))
    conflicting_report["blocks"][2]["exercises"][0]["result"] = "performed"
    conflicting_report["blocks"][2]["exercises"][0]["reason"] = None
    before_replacement_error = row_content_snapshot(
        include_audit=False, include_failed_operations=False
    )
    replacement_error = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-replacement-conflict"), "report": conflicting_report},
        "workout-replacement-conflict-key",
        expected=422,
    )
    assert replacement_error["error"]["code"] == "validation_error"
    assert (
        row_content_snapshot(include_audit=False, include_failed_operations=False)
        == before_replacement_error
    )
    assert_failed_audit(replacement_error["error"]["correlation_id"], "CMD-06")
    assert_failed_operation("workout-replacement-conflict-key", "CMD-06")

    both_sides_report = json.loads(json.dumps(report))
    both_sides_report["blocks"][0]["exercises"][2]["sets"][0]["side"] = "both"
    before_side_error = row_content_snapshot(include_audit=False, include_failed_operations=False)
    side_error = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-both-sides"), "report": both_sides_report},
        "workout-both-sides-key",
        expected=422,
    )
    assert side_error["error"]["code"] == "validation_error"
    assert (
        row_content_snapshot(include_audit=False, include_failed_operations=False)
        == before_side_error
    )
    assert_failed_audit(side_error["error"]["correlation_id"], "CMD-06")
    assert_failed_operation("workout-both-sides-key", "CMD-06")

    rollback_draft = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-rollback-draft"), "report": report},
        "workout-rollback-draft-key",
    )["data"]
    confirm_with_rollback(rollback_draft, "workout-rollback")
    commit_result = confirm(draft, "workout-main")
    session_ref = next(
        item
        for item in commit_result["created_revisions"]
        if item["object_type"] == "workout_session"
    )
    with Session(ENGINE) as session:
        saved = WorkoutRepository(ContextRepository(session))._load_saved_tree(
            UUID(session_ref["object_id"]), UUID(session_ref["revision_id"])
        )
    assert saved["local_start_date"] == "2026-09-23" and saved["duration_seconds"] is None
    assert (
        datetime.fromisoformat(saved["started_at"])
        .astimezone(ZoneInfo("Asia/Novokuznetsk"))
        .date()
        .isoformat()
        == "2026-09-23"
    )
    assert (
        datetime.fromisoformat(saved["ended_at"])
        .astimezone(ZoneInfo("Asia/Novokuznetsk"))
        .date()
        .isoformat()
        == "2026-09-24"
    )
    assert_workout_immutable(session_ref["revision_id"])
    assert all(gap["disposition"] == "leave_unknown" for gap in saved["report_gaps"])
    if os.environ.get("TRAINER_RUN_EG02") == "1":
        from record_flow import run_after_workout

        session_ref, report = run_after_workout(session_ref, report)
    assert saved["blocks"][0]["exercises"][0]["sets"][3]["status"] == "interrupted"
    assert saved["blocks"][0]["exercises"][0]["sets"][3]["full_reps"] == 0
    assert saved["blocks"][0]["exercises"][0]["sets"][4]["status"] == "skipped"
    assert [row["full_reps"] for row in saved["blocks"][0]["exercises"][0]["sets"]] == [
        8,
        6,
        4,
        0,
        None,
    ]
    dumbbell_saved = next(
        ex
        for block in saved["blocks"]
        for ex in block["exercises"]
        if str(ex["exercise_revision_id"])
        == exercise_revision_ids[named["dumbbell"]["exercise_id"]]
    )
    dumbbell_load = dumbbell_saved["sets"][0]["loads"][0]
    planned_dumbbell_load = named["dumbbell"]["sets"][0]["loads"][0]
    assert (
        Decimal(str(dumbbell_load["item_count"])),
        Decimal(str(dumbbell_load["mass"])),
        dumbbell_load["mass_meaning"],
    ) == (Decimal("2"), Decimal("8"), "per_item_mass")
    assert (
        str(dumbbell_load["planned_load_component_id"])
        == planned_dumbbell_load["planned_load_component_id"]
    )
    with ENGINE.connect() as db:
        planned_mass = db.execute(
            text("""
            SELECT mass FROM trainer.planned_load_components
            WHERE planned_load_component_id=:planned_load
        """),
            {"planned_load": planned_dumbbell_load["planned_load_component_id"]},
        ).scalar_one()
    assert Decimal(str(planned_mass)) == Decimal("10")
    assert Decimal(str(planned_mass)) != Decimal(str(dumbbell_load["mass"]))
    unilateral_saved = next(
        ex
        for block in saved["blocks"]
        for ex in block["exercises"]
        if str(ex["exercise_revision_id"])
        == exercise_revision_ids[named["unilateral"]["exercise_id"]]
    )
    assert [
        (
            row["side"],
            row["full_reps"],
            row["loads"][0]["load_kind"],
            Decimal(str(row["loads"][0]["mass"])) if row["loads"][0]["mass"] else None,
        )
        for row in unilateral_saved["sets"]
    ] == [
        ("left", 10, "external", Decimal("10")),
        ("right", 8, "external", Decimal("10")),
        ("left", 7, "bodyweight", None),
    ]
    assert {row["logical_set_no"] for row in unilateral_saved["sets"]} == {1, 2}
    bodyweight_saved = next(
        ex
        for block in saved["blocks"]
        for ex in block["exercises"]
        if str(ex["exercise_revision_id"]) == exercise_revision_ids[named["pullup"]["exercise_id"]]
    )
    assert bodyweight_saved["sets"][0]["loads"][0]["mass"] is None
    bench_load = saved["blocks"][0]["exercises"][0]["sets"][0]["loads"][0]
    assert bench_load["mass_meaning"] == "total_external_mass"
    assert [
        Decimal(str(row["loads"][0]["mass"]))
        for row in saved["blocks"][0]["exercises"][0]["sets"][:4]
    ] == [Decimal("20") for _ in range(4)]
    source_marker = saved["blocks"][2]["exercises"][0]
    added_replacement = saved["blocks"][2]["exercises"][1]
    assert source_marker["result"] == "skipped" and source_marker["reason"] == "replacement"
    assert str(source_marker["planned_exercise_id"]) == named["source"]["planned_exercise_id"]
    assert str(added_replacement["replacement_source"]["skipped_execution_id"]) == str(
        source_marker["execution_id"]
    )
    timed_saved = saved["blocks"][1]["exercises"][1]["sets"]
    assert timed_saved[0]["metrics"][0]["value"] == Decimal("30")
    assert timed_saved[0]["metrics"][0]["unit_code"] == "second"
    assert timed_saved[1]["metrics"] == []
    assert saved["blocks"][0]["exercises"][0]["sets"][0]["reported_rpe"] == Decimal("8")
    assert saved["blocks"][0]["exercises"][0]["sets"][0]["reported_rir"] is None
    assert saved["blocks"][0]["exercises"][1]["sets"][0]["reported_rir"] == Decimal("2")
    assert saved["blocks"][0]["exercises"][1]["sets"][0]["reported_rpe"] is None
    with ENGINE.connect() as db:
        after_plan = db.execute(
            text(
                "SELECT content_digest FROM trainer.workout_plan_revisions WHERE plan_revision_id=:revision"
            ),
            {"revision": fixture_plan["plan_revision_id"]},
        ).scalar_one()
    assert after_plan == plan_digest

    # Explicit leave-unknown markers cover missing planned exercise, set and load facts.
    unknown_report = json.loads(json.dumps(report))
    unknown_report["blocks"][2]["exercises"].clear()
    unknown_report["blocks"][0]["exercises"][0]["sets"].pop()
    unknown_report["blocks"][0]["exercises"][1]["sets"][0]["loads"].clear()
    unknown_report["confirmed_unknown_planned_exercise_ids"] = [
        named["source"]["planned_exercise_id"]
    ]
    unknown_report["confirmed_unknown_planned_set_ids"] = [
        named["bench"]["sets"][-1]["planned_set_id"]
    ]
    unknown_report["confirmed_unknown_planned_load_component_ids"] = [
        named["dumbbell"]["sets"][0]["loads"][0]["planned_load_component_id"]
    ]
    unknown_draft = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-explicit-unknown-draft"), "report": unknown_report},
        "workout-explicit-unknown-key",
    )["data"]
    assert len(unknown_draft["gaps"]) == 4 and all(
        gap["disposition"] == "leave_unknown" for gap in unknown_draft["gaps"]
    ), unknown_draft["gaps"]
    unknown_commit = confirm(unknown_draft, "workout-explicit-unknown")
    unknown_ref = next(
        item
        for item in unknown_commit["created_revisions"]
        if item["object_type"] == "workout_session"
    )
    with Session(ENGINE) as session:
        unknown_saved = WorkoutRepository(ContextRepository(session))._load_saved_tree(
            UUID(unknown_ref["object_id"]), UUID(unknown_ref["revision_id"])
        )
    unknown_paths = {gap["path"] for gap in unknown_saved["report_gaps"]}
    assert any("planned_exercises" in path for path in unknown_paths)
    assert any("planned_sets" in path for path in unknown_paths)
    assert any("planned_load_components" in path for path in unknown_paths)
    assert any("/metrics/duration" in path for path in unknown_paths)

    # Each confirmed-unknown collection is mandatory on the wire.
    missing_fields_snapshot = row_content_snapshot(include_audit=False)
    for field in (
        "confirmed_unknown_planned_exercise_ids",
        "confirmed_unknown_planned_set_ids",
        "confirmed_unknown_planned_load_component_ids",
        "confirmed_unknown_metrics",
    ):
        missing_collection = json.loads(json.dumps(report))
        missing_collection.pop(field)
        missing_field_error = post(
            "/api/v1/commands/workout-report-drafts",
            {
                "context": context(f"missing-{field}"),
                "report": missing_collection,
                "request_digest": "0" * 64,
            },
            f"missing-{field}-key",
            expected=400,
            compute_digest=False,
        )
        assert missing_field_error["error"]["code"] == "validation_error"
    assert row_content_snapshot(include_audit=False) == missing_fields_snapshot

    # Required metric missing without an explicit marker remains incomplete and cannot be confirmed.
    missing_metric = json.loads(json.dumps(report))
    missing_metric["confirmed_unknown_metrics"] = []
    incomplete = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-metric-gap"), "report": missing_metric},
        "workout-metric-gap-key",
    )["data"]
    assert any(
        gap["disposition"] == "clarify" and "duration" in gap["path"] for gap in incomplete["gaps"]
    )
    incomplete_preview = show_preview(incomplete, "workout-metric-gap", expected_complete=False)
    rejected = post(
        f"/api/v1/commands/previews/{incomplete_preview['preview_id']}/confirm",
        confirmation_body(
            incomplete, incomplete_preview, "workout-metric-gap", "Сохрани неполный отчёт"
        ),
        "workout-metric-gap-confirm",
        expected=400,
    )
    assert rejected["error"]["code"] == "incomplete_preview"

    # Empty no-plan report is a visible incomplete preview.
    empty_report = {
        **report,
        "plan_id": None,
        "plan_revision_id": None,
        "blocks": [],
        "confirmed_unknown_metrics": [],
    }
    empty_draft = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-empty-draft"), "report": empty_report},
        "workout-empty-key",
    )["data"]
    assert empty_draft["gaps"] and not empty_draft["preview_candidate"]["complete"]
    empty_preview = show_preview(empty_draft, "workout-empty", expected_complete=False)
    assert (
        post(
            f"/api/v1/commands/previews/{empty_preview['preview_id']}/confirm",
            confirmation_body(empty_draft, empty_preview, "workout-empty", "Сохрани пустой отчёт"),
            "workout-empty-confirm",
            expected=400,
        )["error"]["code"]
        == "incomplete_preview"
    )

    # Unplanned marker based replacement, with explicit bodyweight and resistance band assistance.
    replacement_id = exercise_ids[required_names["replacement"]]
    source_id = exercise_ids[required_names["source"]]
    no_plan_report = {
        "plan_id": None,
        "plan_revision_id": None,
        "started_at": None,
        "ended_at": None,
        "local_start_date": datetime.now(ZoneInfo("Asia/Novokuznetsk")).date().isoformat(),
        "duration_seconds": 0,
        "session_type": "training",
        "name": "Synthetic no-plan replacement",
        "lifecycle": "interrupted",
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
                "planned_block_id": None,
                "block_type_code": "straight",
                "declared_rounds": None,
                "rest_seconds": None,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": source_id,
                        "result": "skipped",
                        "reason": "replacement",
                        "sets": [],
                    },
                    {
                        "position": 2,
                        "exercise_id": replacement_id,
                        "result": "added",
                        "replacement_source": {
                            "kind": "skipped_execution",
                            "block_position": 1,
                            "exercise_position": 1,
                        },
                        "reason": "explicitly replaced synthetic exercise",
                        "sets": [
                            {
                                "position": 1,
                                "logical_set_no": 1,
                                "set_type_code": "working",
                                "full_reps": 5,
                                "status": "performed",
                                "metrics": [],
                                "loads": [
                                    {"position": 1, "load_kind": "bodyweight"},
                                    {
                                        "position": 2,
                                        "load_kind": "band",
                                        "equipment_id": resistance_band_id,
                                        "direction": "assistance",
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    no_plan_draft = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-no-plan-draft"), "report": no_plan_report},
        "workout-no-plan-key",
    )["data"]
    assert not no_plan_draft["gaps"]
    no_plan_commit = confirm(no_plan_draft, "workout-no-plan")
    no_plan_ref = next(
        item
        for item in no_plan_commit["created_revisions"]
        if item["object_type"] == "workout_session"
    )
    with Session(ENGINE) as session:
        no_plan_saved = WorkoutRepository(ContextRepository(session))._load_saved_tree(
            UUID(no_plan_ref["object_id"]), UUID(no_plan_ref["revision_id"])
        )
    assert no_plan_saved["duration_seconds"] == 0
    saved_no_plan_loads = no_plan_saved["blocks"][0]["exercises"][1]["sets"][0]["loads"]
    assert [row["load_kind"] for row in saved_no_plan_loads] == ["bodyweight", "band"]
    assert (
        saved_no_plan_loads[0]["mass"] is None
        and saved_no_plan_loads[1]["direction"] == "assistance"
    )
    with ENGINE.connect() as db:
        interrupted_lifecycle = db.execute(
            text("""
            SELECT lifecycle FROM trainer.workout_session_revisions
            WHERE session_revision_id=:revision
        """),
            {"revision": no_plan_ref["revision_id"]},
        ).scalar_one()
        interrupted_candidates = (
            db.execute(
                text("""
                SELECT aset.status,aset.full_reps,c.metric_kind,c.value,cc.readable_snapshot
                FROM trainer.actual_sets aset
                JOIN trainer.exercise_executions e USING(execution_id)
                JOIN trainer.actual_blocks b USING(actual_block_id)
                LEFT JOIN trainer.record_candidates c
                  ON c.actual_set_id=aset.actual_set_id
                 AND c.session_revision_id=b.session_revision_id
                LEFT JOIN trainer.comparison_conditions cc USING(condition_id)
                WHERE b.session_revision_id=:revision AND e.exercise_id=:exercise
                ORDER BY c.metric_kind
            """),
                {
                    "revision": no_plan_ref["revision_id"],
                    "exercise": replacement_id,
                },
            )
            .mappings()
            .all()
        )
    assert interrupted_lifecycle == "interrupted"
    assert len(interrupted_candidates) == 1
    interrupted_candidate = interrupted_candidates[0]
    assert interrupted_candidate["status"] == "performed"
    assert interrupted_candidate["full_reps"] == 5
    assert interrupted_candidate["metric_kind"] == "record_full_reps"
    assert Decimal(str(interrupted_candidate["value"])) == Decimal(5)
    interrupted_condition = interrupted_candidate["readable_snapshot"]
    interrupted_loads = interrupted_condition["load_signature"]
    assert {item["load_kind"] for item in interrupted_loads} == {"bodyweight", "band"}
    band_signature = next(item for item in interrupted_loads if item["load_kind"] == "band")
    assert band_signature["equipment_id"] == resistance_band_id
    assert band_signature["direction"] == "assistance"

    # A shown CMD-06 preview becomes stale after its exact added-exercise revision changes.
    stale_draft = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context("workout-stale-draft"), "report": report},
        "workout-stale-draft-key",
    )["data"]
    stale_preview = show_preview(stale_draft, "workout-stale")
    correct_exercise(exercise_ids[required_names["replacement"]], "workout-stale-exercise")
    before_stale_confirm = row_content_snapshot(
        include_audit=False, include_failed_operations=False
    )
    stale_error = post(
        f"/api/v1/commands/previews/{stale_preview['preview_id']}/confirm",
        confirmation_body(
            stale_draft, stale_preview, "workout-stale", "Сохрани отчёт после изменения упражнения"
        ),
        "workout-stale-confirm-key",
        expected=409,
    )
    assert stale_error["error"]["code"] == "stale_preview"
    assert (
        row_content_snapshot(include_audit=False, include_failed_operations=False)
        == before_stale_confirm
    )
    assert_failed_operation("workout-stale-confirm-key", "CMD-00", "stale_preview")
    print(
        "PASS: D-G06 planned-vs-actual per-item load, barbell and unilateral loads, unknown metric, empty report, replacement and exact read-back"
    )
    if os.environ.get("TRAINER_RUN_DG07") == "1":
        run_dg07(session_ref, report, plan_digest)


if __name__ == "__main__":
    main()
