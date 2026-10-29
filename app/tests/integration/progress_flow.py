"""Synthetic E-G03 CMD-17/QRY-06 and correction-fallback integration flow."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import create_engine, text

from trainer.domain.context import digest_payload
from trainer.transport.http.contracts import (
    ConfirmRequest,
    PreviewRequest,
    PreviewShownRequest,
    WorkoutCorrectionRequest,
    WorkoutReportDraftRequest,
    WorkoutReportInput,
)
from trainer.transport.http.progress_contracts import (
    CalculationRequest,
    ProgressQueryRequest,
    ProgressQueryResponse,
)

BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]
ENGINE = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
GRIP_LABEL = "Synthetic neutral grip"


def context(message_id: str) -> dict[str, Any]:
    return {
        "conversation_id": "synthetic-eg03-progress",
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
    expected: int | tuple[int, ...] = 200,
) -> dict[str, Any]:
    expected_statuses = (expected,) if isinstance(expected, int) else expected
    if key is not None:
        model = (
            CalculationRequest
            if path.endswith("/calculations")
            else WorkoutReportDraftRequest
            if path.endswith("workout-report-drafts")
            else WorkoutCorrectionRequest
            if path.endswith("correction-drafts")
            else ConfirmRequest
            if path.endswith("/confirm")
            else PreviewShownRequest
            if path.endswith("/shown")
            else PreviewRequest
        )
        normalized = TypeAdapter(model).dump_python(
            TypeAdapter(model).validate_python({**payload, "request_digest": "0" * 64}),
            mode="json",
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
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status in expected_statuses
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = json.loads(error.read())
        if error.code not in expected_statuses:
            raise AssertionError(f"{path}: {error.code}: {body}") from error
        return body


def confirm_draft(draft: dict[str, Any], prefix: str) -> tuple[dict[str, Any], dict[str, Any]]:
    preview = _shown_preview(draft, prefix)
    committed = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        {
            "context": context(prefix + "-confirm"),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": prefix + "-confirm",
            "explicit_intent": "Подтверждаю сохранение синтетических данных",
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        },
        prefix + "-confirm-key",
    )["data"]
    return preview, committed


def _shown_preview(draft: dict[str, Any], prefix: str) -> dict[str, Any]:
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        prefix + "-preview-key",
    )["data"]
    assert preview["complete"], preview
    shown = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context(prefix + "-shown"),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": prefix + "-shown",
            "displayed_text_digest": hashlib.sha256(preview["canonical_text"].encode()).hexdigest(),
            "cross_dialog": False,
        },
        prefix + "-shown-key",
    )["data"]
    assert shown["shown_message_id"] == prefix + "-shown"
    return preview


def _only(source: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if key in fields}


def _project_report(source: dict[str, Any]) -> dict[str, Any]:
    report_fields = {
        "plan_id",
        "plan_revision_id",
        "started_at",
        "ended_at",
        "local_start_date",
        "duration_seconds",
        "session_type",
        "name",
        "lifecycle",
        "effort",
        "comment",
        "workout_metrics",
        "blocks",
        "confirmed_unknown_planned_exercise_ids",
        "confirmed_unknown_planned_set_ids",
        "confirmed_unknown_planned_load_component_ids",
        "confirmed_unknown_metrics",
    }
    block_fields = {
        "position",
        "planned_block_id",
        "block_type_code",
        "declared_rounds",
        "rest_seconds",
        "comment",
    }
    exercise_fields = {
        "position",
        "exercise_id",
        "planned_exercise_id",
        "result",
        "replacement_source",
        "reason",
    }
    set_fields = {
        "position",
        "planned_set_id",
        "round_no",
        "logical_set_no",
        "side",
        "set_type_code",
        "full_reps",
        "partial_reps",
        "failed_attempts",
        "reported_rpe",
        "reported_rir",
        "failure_reached",
        "rom_state",
        "technique_state",
        "tempo",
        "pause_seconds",
        "rest_after_seconds",
        "status",
        "comment",
    }
    load_fields = {
        "position",
        "planned_load_component_id",
        "load_kind",
        "equipment_id",
        "item_count",
        "mass",
        "unit_code",
        "mass_meaning",
        "direction",
        "raw_ambiguous_text",
    }
    metric_fields = {
        "metric_kind",
        "value",
        "unit_code",
        "custom_definition",
        "source_kind",
        "derived_value_id",
    }
    blocks = []
    for block in source["blocks"]:
        exercises = []
        for exercise in block["exercises"]:
            sets = []
            for actual_set in exercise.get("sets", []):
                sets.append(
                    {
                        **_only(actual_set, set_fields),
                        "metrics": [
                            _only(item, metric_fields) for item in actual_set.get("metrics", [])
                        ],
                        "loads": [_only(item, load_fields) for item in actual_set.get("loads", [])],
                    }
                )
            exercises.append({**_only(exercise, exercise_fields), "sets": sets})
        blocks.append({**_only(block, block_fields), "exercises": exercises})
    report = _only(source, report_fields - {"blocks", "workout_metrics"})
    report["blocks"] = blocks
    report["workout_metrics"] = [
        _only(item, metric_fields) for item in source.get("workout_metrics", [])
    ]
    return WorkoutReportInput.model_validate(report).model_dump(mode="json")


def _template() -> dict[str, Any]:
    with ENGINE.connect() as db:
        report = db.execute(
            text("""
                SELECT dr.typed_payload->'report'
                FROM trainer.workout_session_revisions wr
                JOIN trainer.confirmations c USING(confirmation_id)
                JOIN trainer.draft_revisions dr USING(draft_revision_id)
                WHERE wr.name='Synthetic D-G06 planned workout'
                ORDER BY wr.created_at DESC LIMIT 1
            """)
        ).scalar_one_or_none()
    assert report is not None, "D-G06 synthetic workout fixture is missing"
    return _project_report(report)


def _barbell_equipment_id() -> str:
    with ENGINE.connect() as db:
        value = db.execute(
            text("""
                SELECT i.equipment_id
                FROM trainer.equipment_items i
                JOIN trainer.equipment_item_revisions r
                  ON r.equipment_id=i.equipment_id
                 AND r.equipment_revision_id=i.current_revision_id
                WHERE r.equipment_type='barbell'
                  AND i.archived_at IS NULL AND i.logically_deleted_at IS NULL
                ORDER BY i.created_at,i.equipment_id
                LIMIT 1
            """)
        ).scalar_one_or_none()
    assert value is not None, "synthetic E-G03 requires the onboarding barbell fixture"
    return str(value)


def _make_report(
    template: dict[str, Any],
    day: int,
    mass: str,
    name: str,
    *,
    unit_code: str = "kilogram",
    mass_meaning: str = "total_external_mass",
    item_count: int | None = None,
) -> dict[str, Any]:
    report = copy.deepcopy(template)
    start = datetime.fromisoformat(report["started_at"])
    delta = day - date.fromisoformat(report["local_start_date"]).day
    report["started_at"] = (start + timedelta(days=delta)).isoformat()
    if report.get("ended_at"):
        report["ended_at"] = (
            datetime.fromisoformat(report["ended_at"]) + timedelta(days=delta)
        ).isoformat()
    report["local_start_date"] = f"2026-09-{day:02d}"
    report["name"] = name
    report["comment"] = "Synthetic E-G03 calculation and fallback fixture"
    exercise_id = UUID(os.environ["TRAINER_E1RM_BENCH_EXERCISE_ID"])
    bench = next(
        exercise
        for block in report["blocks"]
        for exercise in block["exercises"]
        if UUID(exercise["exercise_id"]) == exercise_id
    )
    actual_set = next(
        item
        for item in bench["sets"]
        if item["set_type_code"] == "working" and item["status"] == "performed"
    )
    actual_set.update(full_reps=6, partial_reps=0, failure_reached=True)
    load = next(item for item in actual_set["loads"] if item["load_kind"] == "external")
    load.update(
        equipment_id=_barbell_equipment_id(),
        mass=mass,
        unit_code=unit_code,
        mass_meaning=mass_meaning,
    )
    if item_count is not None:
        load["item_count"] = item_count
    return report


def _assert_volume_matches_database(point: Any) -> None:
    if point.value is None:
        assert point.completeness == "insufficient"
        return
    with ENGINE.connect() as db:
        rows = (
            db.execute(
                text("""
                SELECT x.unilateral,e.execution_id,aset.actual_set_id,aset.set_type,aset.status,
                       aset.full_reps,aset.partial_reps,aset.side,aset.logical_set_no,
                       aset.round_no,l.load_kind,l.mass,l.unit_code,l.mass_meaning,
                       l.item_count,l.raw_ambiguous_text
                FROM trainer.actual_blocks b
                JOIN trainer.exercise_executions e USING(actual_block_id)
                JOIN trainer.exercise_revisions x
                  ON x.exercise_id=e.exercise_id
                 AND x.exercise_revision_id=e.exercise_revision_id
                JOIN trainer.actual_sets aset USING(execution_id)
                LEFT JOIN trainer.actual_load_components l USING(actual_set_id)
                WHERE b.session_revision_id=:revision AND e.exercise_id=:exercise
                ORDER BY aset.position,l.position
            """),
                {
                    "revision": point.session_ref.revision_id,
                    "exercise": point.exercise_ref.object_id,
                },
            )
            .mappings()
            .all()
        )
    grouped_sets: dict[Any, list[Any]] = {}
    for row in rows:
        if row["actual_set_id"] is not None:
            grouped_sets.setdefault(row["actual_set_id"], []).append(row)
    expected_sides: dict[tuple[Any, int, int | None], set[str]] = {}
    for set_rows in grouped_sets.values():
        row = set_rows[0]
        if row["unilateral"] and row["set_type"] == "working" and row["side"] in ("left", "right"):
            key = (row["execution_id"], int(row["logical_set_no"]), row["round_no"])
            expected_sides.setdefault(key, set()).add(row["side"])
    expected = Decimal(0)
    for set_rows in grouped_sets.values():
        row = set_rows[0]
        if row["set_type"] != "working":
            continue
        if row["status"] == "skipped" or row["full_reps"] is None:
            continue
        if row["unilateral"] and row["side"] not in ("left", "right"):
            continue
        key = (row["execution_id"], int(row["logical_set_no"]), row["round_no"])
        if row["unilateral"] and not expected_sides.get(key):
            continue
        # Rows are one-per-load-component; sum those components once per set.
        loads = [
            load
            for load in set_rows
            if load["load_kind"] == "external"
            and load["mass"] is not None
            and load["raw_ambiguous_text"] is None
            and load["unit_code"] in ("kilogram", "pound")
            and load["mass_meaning"] in ("total_external_mass", "per_item_mass")
            and (load["mass_meaning"] != "per_item_mass" or load["item_count"] is not None)
        ]
        if not loads:
            continue
        mass = Decimal(0)
        for load in loads:
            component = Decimal(str(load["mass"]))
            if load["unit_code"] == "pound":
                component *= Decimal("0.45359237")
            if load["mass_meaning"] == "per_item_mass":
                component *= Decimal(str(load["item_count"]))
            mass += component
        expected += mass * int(row["full_reps"])
    assert point.value.exact_value == expected


def _assert_ephemeral_volume_idempotency(
    first_session: dict[str, str], second_session: dict[str, str], exercise_id: UUID
) -> None:
    payload = {
        "context": context("eg03-ephemeral-volume-idempotency"),
        "method_id": "external_working_set_volume",
        "method_version": "1",
        "mode": "ephemeral",
        "source": {
            "kind": "exercise_session",
            "session_id": first_session["session_id"],
            "session_revision_id": first_session["revision_id"],
            "exercise_id": str(exercise_id),
        },
    }
    key = "eg03-ephemeral-volume-idempotency-key"
    with ENGINE.connect() as db:
        drafts_before = db.execute(
            text("SELECT count(*) FROM trainer.drafts WHERE intent_type='calculation'")
        ).scalar_one()
    first = post("/api/v1/commands/calculations", payload, key)["data"]
    assert first["state"] == "ephemeral"
    assert first["method_id"] == "external_working_set_volume"
    replay = post("/api/v1/commands/calculations", payload, key)["data"]
    assert replay == first
    conflicting = copy.deepcopy(payload)
    conflicting["source"].update(
        session_id=second_session["session_id"],
        session_revision_id=second_session["revision_id"],
    )
    rejected = post("/api/v1/commands/calculations", conflicting, key, expected=409)
    assert rejected["error"]["code"] == "idempotency_conflict", rejected
    with ENGINE.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM trainer.drafts WHERE intent_type='calculation'")
            ).scalar_one()
            == drafts_before
        )
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM trainer.derived_values "
                    "WHERE method_id='external_working_set_volume'"
                )
            ).scalar_one()
            == 0
        )
        operation = db.execute(
            text("""
                SELECT command_kind,state,response_payload
                FROM trainer.idempotent_operations
                WHERE scope='local-owner:CMD-17' AND idempotency_key=:key
            """),
            {"key": key},
        ).one()
        assert operation.command_kind == "CMD-17"
        assert operation.state == "committed"
        assert operation.response_payload["calculation_id"] == first["calculation_id"]


def _assert_ephemeral_failure_is_idempotently_reserved(
    stale_session: dict[str, str], exercise_id: UUID
) -> None:
    payload = {
        "context": context("eg03-ephemeral-stale-source"),
        "method_id": "external_working_set_volume",
        "method_version": "1",
        "mode": "ephemeral",
        "source": {
            "kind": "exercise_session",
            "session_id": stale_session["session_id"],
            "session_revision_id": stale_session["revision_id"],
            "exercise_id": str(exercise_id),
        },
    }
    key = "eg03-ephemeral-stale-source-key"
    failed = post("/api/v1/commands/calculations", payload, key, expected=409)
    assert failed["error"]["code"] == "source_revision_not_current", failed
    with ENGINE.connect() as db:
        operation = db.execute(
            text("""
                SELECT command_kind,state,error_code
                FROM trainer.idempotent_operations
                WHERE scope='local-owner:CMD-17' AND idempotency_key=:key
            """),
            {"key": key},
        ).one()
        assert operation.command_kind == "CMD-17"
        assert operation.state == "failed"
        assert operation.error_code == "source_revision_not_current"
    retry = post("/api/v1/commands/calculations", payload, key, expected=409)
    assert retry["error"]["code"] == "operation_in_progress", retry


def _assert_previous_best_transition_history(previous_derived_id: str, new_derived_id: str) -> None:
    with ENGINE.connect() as db:
        previous_best_id = db.execute(
            text(
                "SELECT calculated_best_id FROM trainer.calculated_bests WHERE derived_value_id=:id"
            ),
            {"id": previous_derived_id},
        ).scalar_one()
        new_best_id = db.execute(
            text(
                "SELECT calculated_best_id FROM trainer.calculated_bests "
                "WHERE derived_value_id=:id AND is_current"
            ),
            {"id": new_derived_id},
        ).scalar_one()
        change = db.execute(
            text("""
                SELECT dc.commit_result_id,dc.operation,dc.before_snapshot,dc.after_snapshot,
                       cr.result_payload
                FROM trainer.domain_changes dc
                JOIN trainer.commit_results cr USING(commit_result_id)
                WHERE dc.object_type='calculated_best' AND dc.object_id=:id
                ORDER BY dc.occurred_at DESC LIMIT 1
            """),
            {"id": previous_best_id},
        ).one()
        assert change.operation == "correct"
        assert change.before_snapshot["is_current"] is True
        assert change.after_snapshot["is_current"] is False
        assert str(change.before_snapshot["calculated_best_id"]) == str(previous_best_id)
        assert str(change.after_snapshot["calculated_best_id"]) == str(previous_best_id)
        refs = change.result_payload["domain_changes"]
        assert {item["object_id"] for item in refs if item["object_type"] == "calculated_best"} == {
            str(previous_best_id),
            str(new_best_id),
        }


def _create_session(report: dict[str, Any], prefix: str) -> dict[str, str]:
    draft = post(
        "/api/v1/commands/workout-report-drafts",
        {"context": context(prefix + "-draft"), "report": report},
        prefix + "-draft-key",
    )["data"]
    confirm_draft(draft, prefix)
    with ENGINE.connect() as db:
        row = (
            db.execute(
                text("""
                SELECT s.session_id,s.current_revision_id
                FROM trainer.workout_sessions s
                JOIN trainer.workout_session_revisions r
                  ON r.session_id=s.session_id AND r.session_revision_id=s.current_revision_id
                WHERE r.name=:name
            """),
                {"name": report["name"]},
            )
            .mappings()
            .one()
        )
    return {"session_id": str(row["session_id"]), "revision_id": str(row["current_revision_id"])}


def _source(session_ref: dict[str, str], exercise_id: UUID) -> dict[str, str]:
    with ENGINE.connect() as db:
        row = (
            db.execute(
                text("""
                SELECT e.exercise_revision_id,a.actual_set_id
                FROM trainer.actual_blocks b
                JOIN trainer.exercise_executions e USING(actual_block_id)
                JOIN trainer.actual_sets a USING(execution_id)
                WHERE b.session_revision_id=:revision AND e.exercise_id=:exercise
                  AND a.logical_set_no=1 AND a.set_type='working' AND a.status='performed'
                  AND a.failure_reached IS TRUE AND a.partial_reps=0 AND a.full_reps=6
                ORDER BY a.position LIMIT 1
            """),
                {"revision": session_ref["revision_id"], "exercise": exercise_id},
            )
            .mappings()
            .one()
        )
    return {
        "kind": "actual_set",
        "session_id": session_ref["session_id"],
        "session_revision_id": session_ref["revision_id"],
        "exercise_id": str(exercise_id),
        "exercise_revision_id": str(row["exercise_revision_id"]),
        "actual_set_id": str(row["actual_set_id"]),
    }


def _calculate(
    source: dict[str, str],
    prefix: str,
    grip_condition: str = GRIP_LABEL,
    prior_derived_value_id: str | None = None,
) -> str:
    prepared = _create_calculation_draft(source, prefix, grip_condition, prior_derived_value_id)
    draft = prepared["draft"]
    preview, _committed = confirm_draft(draft, prefix + "-calculation")
    if prior_derived_value_id is not None:
        prior = preview["canonical_payload"]["calculated"]["prior_calculation"]
        assert prior["derived_value_id"] == prior_derived_value_id
        assert prior["state"] == "stale"
    return str(prepared["calculation_id"])


def _create_calculation_draft(
    source: dict[str, str],
    prefix: str,
    grip_condition: str = GRIP_LABEL,
    prior_derived_value_id: str | None = None,
    *,
    expected: int = 200,
) -> dict[str, Any]:
    payload = {
        "context": context(prefix + "-calculation"),
        "method_id": "e1rm_brzycki",
        "method_version": "1",
        "mode": "persist",
        "source": source,
        "prior_derived_value_id": prior_derived_value_id,
        "execution_attestation": {
            "source_actual_set_id": source["actual_set_id"],
            "grip_condition": grip_condition,
            "bench_pause_condition": "no_pause",
            "standard_confirmed": True,
            "spotter_released_before_rep": True,
            "no_assistance_during_rep": True,
        },
    }
    response = post(
        "/api/v1/commands/calculations",
        payload,
        prefix + "-calculation-key",
        expected=expected,
    )
    if expected != 200:
        return response
    prepared = response["data"]
    assert prepared["state"] == "draft", prepared
    return prepared


def _race_same_calculation_confirmation(
    source: dict[str, str], prefix: str, grip_condition: str
) -> str:
    payload = {
        "context": context(prefix + "-calculation"),
        "method_id": "e1rm_brzycki",
        "method_version": "1",
        "mode": "persist",
        "source": source,
        "execution_attestation": {
            "source_actual_set_id": source["actual_set_id"],
            "grip_condition": grip_condition,
            "bench_pause_condition": "no_pause",
            "standard_confirmed": True,
            "spotter_released_before_rep": True,
            "no_assistance_during_rep": True,
        },
    }
    draft_response = post("/api/v1/commands/calculations", payload, prefix + "-calculation-key")[
        "data"
    ]
    preview = _shown_preview(draft_response["draft"], prefix + "-calculation")

    def confirm(index: int) -> dict[str, Any]:
        message_id = f"{prefix}-confirm-{index}"
        return post(
            f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
            {
                "context": context(message_id),
                "revision_id": draft_response["draft"]["revision_id"],
                "preview_digest": preview["digest"],
                "confirmation_kind": "normal",
                "confirmation_message_id": message_id,
                "explicit_intent": "Подтверждаю сохранение синтетического расчёта",
                "confirmation_mode": "same_dialog",
                "referenced_preview_id": None,
                "reshown_message_id": None,
            },
            f"{prefix}-confirm-key-{index}",
            expected=(200, 409),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(confirm, (1, 2)))
    committed = [outcome for outcome in outcomes if "data" in outcome]
    conflicts = [outcome for outcome in outcomes if "error" in outcome]
    assert len(committed) == 1, outcomes
    assert len(conflicts) == 1, outcomes
    assert conflicts[0]["error"]["code"] in ("already_superseded", "stale_preview"), outcomes

    duplicate_prefix = prefix + "-duplicate"
    duplicate_request = {
        "context": context(duplicate_prefix + "-calculation"),
        "method_id": "e1rm_brzycki",
        "method_version": "1",
        "mode": "persist",
        "source": source,
        "execution_attestation": {
            "source_actual_set_id": source["actual_set_id"],
            "grip_condition": grip_condition,
            "bench_pause_condition": "no_pause",
            "standard_confirmed": True,
            "spotter_released_before_rep": True,
            "no_assistance_during_rep": True,
        },
    }
    duplicate_draft = post(
        "/api/v1/commands/calculations",
        duplicate_request,
        duplicate_prefix + "-calculation-key",
    )["data"]
    duplicate_preview = _shown_preview(duplicate_draft["draft"], duplicate_prefix)
    duplicate_result = post(
        f"/api/v1/commands/previews/{duplicate_preview['preview_id']}/confirm",
        {
            "context": context(duplicate_prefix + "-confirm"),
            "revision_id": duplicate_draft["draft"]["revision_id"],
            "preview_digest": duplicate_preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": duplicate_prefix + "-confirm",
            "explicit_intent": "Подтверждаю сохранение синтетического расчёта",
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        },
        duplicate_prefix + "-confirm-key",
        expected=409,
    )
    assert duplicate_result["error"]["code"] == "already_calculated", duplicate_result

    with ENGINE.connect() as db:
        row = (
            db.execute(
                text("""
                SELECT derived_value_id,condition_id
                FROM trainer.derived_progress_points
                WHERE source_actual_set_id=:source
            """),
                {"source": source["actual_set_id"]},
            )
            .mappings()
            .one()
        )
        same_source_count = db.execute(
            text("""
                SELECT count(*) FROM trainer.derived_progress_points
                WHERE source_actual_set_id=:source AND condition_id=:condition
            """),
            {"source": source["actual_set_id"], "condition": row["condition_id"]},
        ).scalar_one()
        current_best_count = db.execute(
            text("""
                SELECT count(*) FROM trainer.calculated_bests
                WHERE profile_id=(
                    SELECT profile_id FROM trainer.profiles ORDER BY created_at LIMIT 1
                )
                  AND exercise_id=:exercise AND metric_kind='estimated_1rm'
                  AND condition_id=:condition AND is_current
            """),
            {
                "exercise": source["exercise_id"],
                "condition": row["condition_id"],
            },
        ).scalar_one()
    assert same_source_count == 1
    assert current_best_count == 1
    return str(row["derived_value_id"])


def _correct(
    session_ref: dict[str, str],
    report: dict[str, Any],
    prefix: str,
    *,
    expect_fallback: bool = True,
) -> dict[str, Any]:
    corrected = copy.deepcopy(report)
    exercise_id = UUID(os.environ["TRAINER_E1RM_BENCH_EXERCISE_ID"])
    bench = next(
        exercise
        for block in corrected["blocks"]
        for exercise in block["exercises"]
        if UUID(exercise["exercise_id"]) == exercise_id
    )
    actual_set = next(item for item in bench["sets"] if item.get("failure_reached") is True)
    next(item for item in actual_set["loads"] if item["load_kind"] == "external")["mass"] = "10"
    draft = post(
        "/api/v1/commands/correction-drafts",
        {
            "context": context(prefix + "-draft"),
            "target": {
                "object_type": "workout_session",
                "object_id": session_ref["session_id"],
                "revision_id": session_ref["revision_id"],
            },
            "patch": [{"op": "replace", "path": "/report", "value": corrected}],
            "reason": "Synthetic E-G03 fallback regression",
        },
        prefix + "-draft-key",
    )["data"]
    preview, committed = confirm_draft(draft, prefix)
    effects = preview["canonical_payload"]["calculation_effects"]
    if expect_fallback:
        fallback_creations = [effect for effect in effects if effect["fallback_create"]]
        assert len(fallback_creations) == 1, effects
        assert fallback_creations[0]["fallback_source_actual_set_id"]
        assert any(effect["fallback_derived_value_id"] is None for effect in effects)
    else:
        assert all(effect["fallback_derived_value_id"] is None for effect in effects), effects
        assert all(effect["fallback_best_id"] is None for effect in effects), effects
    return committed


def main() -> None:
    exercise_id = UUID(os.environ["TRAINER_E1RM_BENCH_EXERCISE_ID"])
    template = _template()
    specs = (
        (24, "20", "Synthetic E-G03 lower candidate", {}),
        (
            25,
            "88",
            "Synthetic E-G03 corrected best",
            {"unit_code": "pound"},
        ),
        (
            26,
            "15",
            "Synthetic E-G03 unpromoted fallback",
            {"mass_meaning": "per_item_mass", "item_count": 2},
        ),
    )
    sessions: dict[str, dict[str, Any]] = {}
    for day, mass, name, load_shape in specs:
        report = _make_report(template, day, mass, name, **load_shape)
        session_ref = _create_session(report, f"eg03-{day}")
        derived_id = _calculate(_source(session_ref, exercise_id), f"eg03-{day}")
        with ENGINE.connect() as db:
            attestation = (
                db.execute(
                    text(
                        """
                    SELECT source_kind,source_object_type,source_object_id,source_revision_id,
                           snapshot
                    FROM trainer.derived_value_inputs
                    WHERE derived_value_id=:id AND input_role='execution_attestation'
                    """
                    ),
                    {"id": derived_id},
                )
                .mappings()
                .one()
            )
            assert attestation["source_kind"] == "user"
            assert attestation["source_object_type"] == "actual_set"
            assert (
                str(attestation["source_object_id"])
                == _source(session_ref, exercise_id)["actual_set_id"]
            )
            assert str(attestation["source_revision_id"]) == session_ref["revision_id"]
            assert attestation["snapshot"]["grip_condition_code"] == GRIP_LABEL.lower()
            assert attestation["snapshot"]["standard_confirmed"] is True
        sessions[name] = {"ref": session_ref, "report": report, "derived_id": derived_id}

    race_report = _make_report(template, 27, "20", "Synthetic E-G03 concurrent confirmation")
    race_session = _create_session(race_report, "eg03-concurrent")
    race_derived_id = _race_same_calculation_confirmation(
        _source(race_session, exercise_id),
        "eg03-concurrent",
        "Synthetic race grip",
    )

    lower = sessions["Synthetic E-G03 lower candidate"]
    high = sessions["Synthetic E-G03 corrected best"]
    middle = sessions["Synthetic E-G03 unpromoted fallback"]
    _assert_previous_best_transition_history(lower["derived_id"], high["derived_id"])
    _assert_ephemeral_volume_idempotency(high["ref"], lower["ref"], exercise_id)
    high_source = _source(high["ref"], exercise_id)
    alternate_grip_derived_id = _calculate(
        high_source, "eg03-high-alternate-grip", "Synthetic wide grip"
    )
    with ENGINE.connect() as db:
        assert db.execute(
            text("""
                SELECT count(*),count(DISTINCT condition_id)
                FROM trainer.derived_progress_points WHERE source_actual_set_id=:source
            """),
            {"source": high_source["actual_set_id"]},
        ).one() == (2, 2)
    with ENGINE.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM trainer.calculated_bests WHERE derived_value_id=:id"),
                {"id": middle["derived_id"]},
            ).scalar_one()
            == 0
        )

    _correct(high["ref"], high["report"], "eg03-correct-high")
    with ENGINE.connect() as db:
        corrected_revision_id = db.execute(
            text("SELECT current_revision_id FROM trainer.workout_sessions WHERE session_id=:id"),
            {"id": high["ref"]["session_id"]},
        ).scalar_one()
    corrected_ref = {**high["ref"], "revision_id": str(corrected_revision_id)}
    _assert_ephemeral_failure_is_idempotently_reserved(high["ref"], exercise_id)
    corrected_source = _source(corrected_ref, exercise_id)
    recalculated_id = _calculate(
        corrected_source,
        "eg03-explicit-supersedes",
        prior_derived_value_id=high["derived_id"],
    )
    unlinked_id = _calculate(
        corrected_source,
        "eg03-no-auto-supersedes",
        "Synthetic wide grip",
    )
    for prefix, selected_id in (
        ("eg03-reject-current", middle["derived_id"]),
        ("eg03-reject-incompatible", alternate_grip_derived_id),
        ("eg03-reject-replaced", high["derived_id"]),
        ("eg03-reject-missing", str(uuid4())),
    ):
        rejected = _create_calculation_draft(
            corrected_source,
            prefix,
            prior_derived_value_id=selected_id,
            expected=409,
        )
        assert rejected["error"]["code"] == "invalid_dependency", rejected
    with ENGINE.connect() as db:
        states = dict(
            db.execute(
                text("""
                    SELECT derived_value_id,state FROM trainer.derived_values
                    WHERE derived_value_id IN (:lower,:high,:middle,:alternate)
                """),
                {
                    "lower": lower["derived_id"],
                    "high": high["derived_id"],
                    "middle": middle["derived_id"],
                    "alternate": alternate_grip_derived_id,
                },
            ).all()
        )
        assert states[UUID(high["derived_id"])] == "replaced"
        assert states[UUID(alternate_grip_derived_id)] == "stale"
        assert states[UUID(middle["derived_id"])] == "current"
        assert (
            db.execute(
                text(
                    "SELECT supersedes_derived_value_id FROM trainer.derived_values "
                    "WHERE derived_value_id=:id"
                ),
                {"id": unlinked_id},
            ).scalar_one()
            is None
        )
        supersedes = db.execute(
            text("""
                SELECT new.supersedes_derived_value_id,old.state
                FROM trainer.derived_values new
                JOIN trainer.derived_values old
                  ON old.derived_value_id=new.supersedes_derived_value_id
                WHERE new.derived_value_id=:new
            """),
            {"new": recalculated_id},
        ).one()
        assert str(supersedes[0]) == high["derived_id"]
        assert supersedes[1] == "replaced"
        assert (
            db.execute(
                text("""
                SELECT count(*) FROM trainer.derived_values
                WHERE method_id='e1rm_brzycki' AND supersedes_derived_value_id IS NOT NULL
            """)
            ).scalar_one()
            == 1
        )
        assert (
            db.execute(
                text("""
                SELECT is_current FROM trainer.calculated_bests WHERE derived_value_id=:id
            """),
                {"id": middle["derived_id"]},
            ).scalar_one()
            is True
        )
        current_view_rows = db.execute(
            text("""
                SELECT count(*) FROM trainer.v_best_results
                WHERE is_current AND category='calculated_best'
                  AND exercise_id=:exercise AND condition_id=(
                      SELECT condition_id FROM trainer.derived_progress_points
                      WHERE derived_value_id=:derived
                  )
            """),
            {"exercise": exercise_id, "derived": middle["derived_id"]},
        ).scalar_one()
        assert current_view_rows == 1
        stale_view_state = db.execute(
            text("""
                SELECT is_current,is_historical FROM trainer.v_best_results
                WHERE source_derived_value_id=:id
            """),
            {"id": high["derived_id"]},
        ).one()
        assert stale_view_state == (False, True)
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM trainer.derived_values "
                    "WHERE method_id='external_working_set_volume'"
                )
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                text("""
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema='trainer'
                  AND table_name IN ('actual_sets','exercise_revisions')
                  AND column_name IN ('grip_condition','bench_pause_condition','standard_confirmed')
            """)
            ).scalar_one()
            == 0
        )

    _correct(
        lower["ref"],
        lower["report"],
        "eg03-correct-lower",
        expect_fallback=False,
    )
    with ENGINE.connect() as db:
        lower_corrected_revision_id = db.execute(
            text("SELECT current_revision_id FROM trainer.workout_sessions WHERE session_id=:id"),
            {"id": lower["ref"]["session_id"]},
        ).scalar_one()
    lower_corrected_ref = {**lower["ref"], "revision_id": str(lower_corrected_revision_id)}
    lower_corrected_source = _source(lower_corrected_ref, exercise_id)
    first_replacement = _create_calculation_draft(
        lower_corrected_source,
        "eg03-prior-race-first",
        prior_derived_value_id=lower["derived_id"],
    )
    second_replacement = _create_calculation_draft(
        lower_corrected_source,
        "eg03-prior-race-second",
        prior_derived_value_id=lower["derived_id"],
    )
    second_preview = _shown_preview(second_replacement["draft"], "eg03-prior-race-second")
    confirm_draft(first_replacement["draft"], "eg03-prior-race-first")
    rejected_confirmation = post(
        f"/api/v1/commands/previews/{second_preview['preview_id']}/confirm",
        {
            "context": context("eg03-prior-race-second-confirm"),
            "revision_id": second_replacement["draft"]["revision_id"],
            "preview_digest": second_preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": "eg03-prior-race-second-confirm",
            "explicit_intent": "Подтверждаю сохранение синтетических данных",
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        },
        "eg03-prior-race-second-confirm-key",
        expected=409,
    )
    assert rejected_confirmation["error"]["code"] == "stale_dependency"
    with ENGINE.connect() as db:
        assert (
            db.execute(
                text("SELECT state FROM trainer.derived_values WHERE derived_value_id=:id"),
                {"id": lower["derived_id"]},
            ).scalar_one()
            == "replaced"
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM trainer.derived_values WHERE derived_value_id=:id"),
                {"id": second_replacement["calculation_id"]},
            ).scalar_one()
            == 0
        )

    progress_request = ProgressQueryRequest(
        context=context("eg03-qry-e1rm"),
        metric_kind="estimated_1rm",
        date_from=date(2026, 9, 24),
        date_to=date(2026, 9, 26),
        exercise_id=exercise_id,
        include_history=True,
        limit=50,
    ).model_dump(mode="json")
    history = ProgressQueryResponse.model_validate(
        post("/api/v1/queries/progress", progress_request)["data"]
    )
    derived_points = [item.point for item in history.items if item.kind == "derived"]
    assert any(point.state == "stale" for point in derived_points)
    assert any(
        point.derived_value_ref and str(point.derived_value_ref.object_id) == middle["derived_id"]
        for point in derived_points
    ), {
        "expected_middle": middle["derived_id"],
        "received": [(point.derived_value_ref, point.state) for point in derived_points],
    }
    race_progress = ProgressQueryResponse.model_validate(
        post(
            "/api/v1/queries/progress",
            ProgressQueryRequest(
                context=context("eg03-qry-concurrent-result"),
                metric_kind="estimated_1rm",
                date_from=date(2026, 9, 27),
                date_to=date(2026, 9, 27),
                exercise_id=exercise_id,
                include_history=True,
                limit=50,
            ).model_dump(mode="json"),
        )["data"]
    )
    assert any(
        item.kind == "derived"
        and item.point.derived_value_ref
        and str(item.point.derived_value_ref.object_id) == race_derived_id
        and item.point.state == "current"
        for item in race_progress.items
    ), race_progress.items

    volume_request = ProgressQueryRequest(
        context=context("eg03-qry-volume"),
        metric_kind="external_working_set_volume",
        date_from=date(2026, 9, 24),
        date_to=date(2026, 9, 26),
        exercise_id=exercise_id,
        include_history=True,
        limit=50,
    ).model_dump(mode="json")
    volume = ProgressQueryResponse.model_validate(
        post("/api/v1/queries/progress", volume_request)["data"]
    )
    volume_points = [item.point for item in volume.items if item.kind == "derived"]
    assert len(volume_points) == 3
    assert all(
        point.state == "ephemeral" and point.derived_value_ref is None for point in volume_points
    )
    for point in volume_points:
        _assert_volume_matches_database(point)

    tied_volume_request = ProgressQueryRequest(
        context=context("eg03-qry-volume-page-1"),
        metric_kind="external_working_set_volume",
        date_from=date(2026, 9, 24),
        date_to=date(2026, 9, 24),
        include_history=True,
        limit=1,
    ).model_dump(mode="json")
    tied_items = []
    page_count = 0
    while True:
        page_count += 1
        assert page_count <= 100, "volume pagination did not terminate"
        page = ProgressQueryResponse.model_validate(
            post("/api/v1/queries/progress", tied_volume_request)["data"]
        )
        tied_items.extend(item for item in page.items if item.kind == "derived")
        if page.next_cursor is None:
            break
        tied_volume_request = ProgressQueryRequest(
            context=context(f"eg03-qry-volume-page-{page_count + 1}"),
            metric_kind="external_working_set_volume",
            date_from=date(2026, 9, 24),
            date_to=date(2026, 9, 24),
            include_history=True,
            limit=1,
            cursor=page.next_cursor,
        ).model_dump(mode="json")
    tied_points = [item.point for item in tied_items]
    tied_point_keys = [item.point_key for item in tied_items]
    assert len(tied_points) > 1
    assert len(tied_point_keys) == len(set(tied_point_keys))
    assert all(point.state == "ephemeral" for point in tied_points)
    assert len({point.session_ref.object_id for point in tied_points}) == 1
    full_volume_request = ProgressQueryRequest(
        context=context("eg03-qry-volume-full"),
        metric_kind="external_working_set_volume",
        date_from=date(2026, 9, 24),
        date_to=date(2026, 9, 24),
        include_history=True,
        limit=100,
    ).model_dump(mode="json")
    full_volume_page = ProgressQueryResponse.model_validate(
        post("/api/v1/queries/progress", full_volume_request)["data"]
    )
    full_volume_keys = [item.point_key for item in full_volume_page.items if item.kind == "derived"]
    assert set(tied_point_keys) == set(full_volume_keys)
    assert len(tied_point_keys) == len(full_volume_keys)
    for point in tied_points:
        _assert_volume_matches_database(point)

    with ENGINE.connect() as db:
        skipped = (
            db.execute(
                text("""
                SELECT s.session_id,s.current_revision_id,e.exercise_id
                FROM trainer.workout_sessions s
                JOIN trainer.workout_session_revisions r
                  ON r.session_id=s.session_id AND r.session_revision_id=s.current_revision_id
                JOIN trainer.actual_blocks b USING(session_revision_id)
                JOIN trainer.exercise_executions e USING(actual_block_id)
                WHERE r.name='Synthetic D-G06 planned workout' AND e.result='skipped'
                ORDER BY e.position LIMIT 1
            """)
            )
            .mappings()
            .one()
        )
    skipped_query = ProgressQueryRequest(
        context=context("eg03-qry-empty-execution"),
        metric_kind="external_working_set_volume",
        date_from=date(2026, 9, 23),
        date_to=date(2026, 9, 23),
        exercise_id=skipped["exercise_id"],
        include_history=True,
        limit=10,
    ).model_dump(mode="json")
    skipped_response = ProgressQueryResponse.model_validate(
        post("/api/v1/queries/progress", skipped_query)["data"]
    )
    skipped_points = [item.point for item in skipped_response.items if item.kind == "derived"]
    assert len(skipped_points) == 1
    assert skipped_points[0].completeness == "insufficient"
    assert skipped_points[0].value is None
    print("PASS: E-G03 CMD-17, QRY-06, read-time volume and calculated-best correction fallback")


if __name__ == "__main__":
    main()
