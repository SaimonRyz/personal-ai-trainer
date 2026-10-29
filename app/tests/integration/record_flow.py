"""E-G02 synthetic typed record candidate and historical-import flow."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import create_engine, text

from trainer.domain.context import digest_payload
from trainer.transport.http.contracts import (
    ConfirmRequest,
    HistoricalRecordDraftRequest,
    PreviewRequest,
    PreviewShownRequest,
    RecordCandidatesRequest,
    RecordDecisionDraftRequest,
    RecordQueryRequest,
    RecordResultItem,
    WorkoutCorrectionRequest,
)

BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]
ENGINE = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])


def context(message_id: str) -> dict[str, Any]:
    return {
        "conversation_id": "synthetic-eg02-records",
        "message_id": message_id,
        "effective_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "timezone": "Asia/Novokuznetsk",
        "locale": "ru",
    }


def post(path: str, payload: dict[str, Any], key: str | None = None, expected: int = 200):
    if key:
        model = (
            RecordDecisionDraftRequest
            if path.endswith("record-decision-drafts")
            else HistoricalRecordDraftRequest
            if path.endswith("historical-record-drafts")
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
        checked = {**normalized, "request_digest": digest_payload(normalized)}
        payload = checked
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
            assert response.status == expected
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        response = json.loads(error.read())
        if error.code != expected:
            raise AssertionError(f"{path}: {error.code}: {response}") from error
        return response


def prepare_preview(draft: dict[str, Any], prefix: str) -> dict[str, Any]:
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        prefix + "-preview-key",
    )["data"]
    assert preview["complete"]
    text_digest = hashlib.sha256(preview["canonical_text"].encode()).hexdigest()
    shown = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context(prefix + "-shown"),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": prefix + "-shown",
            "displayed_text_digest": text_digest,
            "cross_dialog": False,
        },
        prefix + "-shown-key",
    )["data"]
    assert shown["shown_message_id"] == prefix + "-shown"
    return preview


def confirm_preview(
    draft: dict[str, Any],
    preview: dict[str, Any],
    prefix: str,
    intent: str,
    *,
    expected: int = 200,
) -> dict[str, Any]:
    response = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        {
            "context": context(prefix + "-confirm"),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": prefix + "-confirm",
            "explicit_intent": intent,
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        },
        prefix + "-confirm-key",
        expected=expected,
    )
    return response["data"] if expected == 200 else response


def confirm_draft(draft: dict[str, Any], prefix: str, intent: str) -> dict[str, Any]:
    preview = prepare_preview(draft, prefix)
    return confirm_preview(draft, preview, prefix, intent)


def record_items(
    exercise_id: str,
    metric_kind: str,
    view: str = "all",
    condition_id: str | None = None,
) -> list[dict[str, Any]]:
    return post(
        "/api/v1/queries/records",
        RecordQueryRequest(
            context=context(f"records-{view}-{uuid4()}"),
            exercise_id=exercise_id,
            metric_kind=metric_kind,
            condition_id=condition_id,
            view=view,
            limit=100,
        ).model_dump(mode="json"),
    )["data"]["items"]


def historical_input(
    candidate: dict[str, Any],
    condition: dict[str, Any],
    value: Decimal,
    reason: str,
    body_mass_kg: str | None = None,
) -> dict[str, Any]:
    return {
        "exercise_id": str(candidate["exercise_id"]),
        "exercise_revision_id": str(candidate["exercise_revision_id"]),
        "metric_kind": candidate["metric_kind"],
        "value": str(value),
        "unit_code": candidate["unit_code"],
        "achieved_on": date(2026, 9, 1).isoformat(),
        "condition": {
            key: condition[key]
            for key in ("side", "loads", "fixed_reps", "fixed_distance_m", "unknown_fields")
        },
        "body_mass_kg": body_mass_kg,
        "historical_import": True,
        "pre_system_assertion": True,
        "technique_self_confirmation": True,
        "reason": reason,
        "comment": None,
    }


def create_historical(
    candidate: dict[str, Any],
    condition: dict[str, Any],
    value: Decimal,
    prefix: str,
    body_mass_kg: str | None = None,
) -> dict[str, Any]:
    draft = post(
        "/api/v1/commands/historical-record-drafts",
        {
            "context": context(prefix + "-draft"),
            "intent": "create",
            "historical": historical_input(
                candidate, condition, value, prefix, body_mass_kg=body_mass_kg
            ),
        },
        prefix + "-key",
    )["data"]
    return confirm_draft(draft, prefix, "Подтверждаю исторический результат")


def changed_report(report: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    corrected = json.loads(json.dumps(report))
    corrected["comment"] += " E-G02 source correction"
    for candidate in candidates:
        item = corrected["blocks"][candidate["block_position"] - 1]["exercises"][
            candidate["exercise_position"] - 1
        ]["sets"][candidate["set_position"] - 1]
        if candidate["metric_kind"] in {"record_hold_duration", "record_cardio_time"}:
            duration = next(
                metric for metric in item["metrics"] if metric["metric_kind"] == "duration"
            )
            delta = (
                Decimal(1) if candidate["metric_kind"] == "record_hold_duration" else Decimal(-1)
            )
            duration["value"] = str(Decimal(duration["value"]) + delta)
        elif candidate["metric_kind"] == "record_external_load":
            external_load = next(load for load in item["loads"] if load["load_kind"] == "external")
            external_load["mass"] = str(Decimal(external_load["mass"]) + Decimal(1))
        else:
            item["full_reps"] = (item.get("full_reps") or 0) + 1
    return corrected


def created_record_id(result: dict[str, Any]) -> str:
    """Resolve the inserted record, not an arbitrary updated record in the change list."""
    with ENGINE.connect() as db:
        return str(
            db.execute(
                text("SELECT record_id FROM trainer.actual_records WHERE confirmation_id=:id"),
                {"id": result["confirmation_id"]},
            ).scalar_one()
        )


def correction_draft(session_ref: dict[str, str], report: dict[str, Any], prefix: str):
    return post(
        "/api/v1/commands/correction-drafts",
        {
            "context": context(prefix + "-draft"),
            "target": {"object_type": "workout_session", **session_ref},
            "patch": [{"op": "replace", "path": "/report", "value": report}],
            "reason": "Synthetic E-G02 record correction",
        },
        prefix + "-key",
    )["data"]


def run_after_workout(
    session_ref: dict[str, str], report: dict[str, Any]
) -> tuple[dict[str, str], dict[str, Any]]:
    with ENGINE.connect() as db:
        candidates = (
            db.execute(
                text("""
            SELECT c.candidate_id,c.exercise_id,c.metric_kind,c.value,c.unit_code,
                   c.exercise_revision_id,c.condition_id,c.actual_set_id,
                   cc.readable_snapshot AS condition_snapshot,
                   b.position AS block_position,e.position AS exercise_position,
                   aset.position AS set_position
            FROM trainer.record_candidates c
            JOIN trainer.actual_sets aset ON aset.actual_set_id=c.actual_set_id
            JOIN trainer.exercise_executions e ON e.execution_id=aset.execution_id
            JOIN trainer.actual_blocks b ON b.actual_block_id=e.actual_block_id
            JOIN trainer.comparison_conditions cc ON cc.condition_id=c.condition_id
            WHERE c.status='pending' AND c.session_revision_id=:revision
            ORDER BY c.created_at DESC,c.candidate_id DESC
        """),
                {"revision": session_ref["revision_id"]},
            )
            .mappings()
            .all()
        )
        ineligible_sets = (
            db.execute(
                text("""
            SELECT aset.actual_set_id,aset.status,aset.partial_reps
            FROM trainer.actual_sets aset
            JOIN trainer.exercise_executions e USING(execution_id)
            JOIN trainer.actual_blocks b USING(actual_block_id)
            WHERE b.session_revision_id=:revision
              AND (aset.status <> 'performed'
                   OR (aset.partial_reps IS NOT NULL AND aset.partial_reps > 0))
        """),
                {"revision": session_ref["revision_id"]},
            )
            .mappings()
            .all()
        )
    assert any(item["status"] == "interrupted" for item in ineligible_sets), (
        "synthetic workout must include an interrupted set for AC-A-027 negative coverage",
        ineligible_sets,
    )
    assert any(item["status"] == "skipped" for item in ineligible_sets), (
        "synthetic workout must include a skipped set for AC-A-027 negative coverage",
        ineligible_sets,
    )
    assert any(
        item["partial_reps"] is not None and item["partial_reps"] > 0 for item in ineligible_sets
    ), (
        "synthetic workout must include partial repetitions for AC-A-027 negative coverage",
        ineligible_sets,
    )
    ineligible_ids = {str(item["actual_set_id"]) for item in ineligible_sets}
    candidate_source_ids = {str(item["actual_set_id"]) for item in candidates}
    assert ineligible_ids.isdisjoint(candidate_source_ids), (
        "skipped/interrupted sets must not produce record candidates",
        ineligible_sets,
        candidates,
    )
    bodyweight_candidate = next(
        item
        for item in candidates
        if item["metric_kind"] == "record_full_reps"
        and len(item["condition_snapshot"]["load_signature"]) == 1
        and item["condition_snapshot"]["load_signature"][0]["load_kind"] == "bodyweight"
    )
    groups: dict[tuple[str, str, str], Any] = {}
    for item in candidates:
        groups.setdefault(
            (str(item["exercise_id"]), item["metric_kind"], str(item["condition_id"])), item
        )
    assert len(groups) >= 3, (
        "synthetic E-G02 fixture requires three distinct candidate groups; "
        f"found {[(key, item['metric_kind']) for key, item in groups.items()]}"
    )
    expected_metrics = {
        "record_full_reps",
        "record_external_load",
        "record_hold_duration",
        "record_cardio_time",
    }
    present_metrics = {item["metric_kind"] for item in groups.values()}
    assert present_metrics >= expected_metrics, (
        "one synthetic workout must produce all four AC-A-027 metric kinds",
        expected_metrics - present_metrics,
    )
    reps_candidate = next(
        item for item in groups.values() if item["metric_kind"] == "record_full_reps"
    )
    cardio_candidate = next(
        item for item in groups.values() if item["metric_kind"] == "record_cardio_time"
    )
    cardio_query = post(
        "/api/v1/queries/record-candidates",
        RecordCandidatesRequest(
            context=context("cardio-candidate-query"),
            exercise_id=str(cardio_candidate["exercise_id"]),
            metric_kind="record_cardio_time",
            status="pending",
            limit=100,
        ).model_dump(mode="json"),
    )["data"]
    cardio_view = next(
        item
        for item in cardio_query["items"]
        if item["candidate_id"] == str(cardio_candidate["candidate_id"])
    )
    assert Decimal(cardio_view["candidate_value"]["exact_value"]) == Decimal(600)
    assert Decimal(cardio_view["condition"]["fixed_distance_m"]) == Decimal(2000)
    assert cardio_view["source_set_ref"]["object_id"] == str(cardio_candidate["actual_set_id"])
    external_candidate = next(
        item for item in groups.values() if item["metric_kind"] == "record_external_load"
    )
    bench_reps_candidate = next(
        item
        for item in groups.values()
        if item["metric_kind"] == "record_full_reps"
        and item["exercise_id"] == external_candidate["exercise_id"]
    )
    all_pending = post(
        "/api/v1/queries/record-candidates",
        RecordCandidatesRequest(
            context=context("all-pending-candidates-query"),
            exercise_id=None,
            metric_kind=None,
            status="pending",
            limit=100,
        ).model_dump(mode="json"),
    )["data"]["items"]
    bench_reps_view = next(
        item
        for item in all_pending
        if item["candidate_id"] == str(bench_reps_candidate["candidate_id"])
    )
    bench_external_load = next(
        load for load in bench_reps_view["condition"]["loads"] if load["load_kind"] == "external"
    )
    assert Decimal(bench_external_load["mass_kg"]) == Decimal("20")
    hold_candidate = next(
        item for item in groups.values() if item["metric_kind"] == "record_hold_duration"
    )
    selected = [reps_candidate, external_candidate, hold_candidate, cardio_candidate]
    selected_ids = {str(item["candidate_id"]) for item in selected}
    pending_candidate = next(
        item for item in candidates if str(item["candidate_id"]) not in selected_ids
    )
    candidate = selected[0]
    query = post(
        "/api/v1/queries/record-candidates",
        RecordCandidatesRequest(
            context=context("candidate-query"),
            exercise_id=str(candidate["exercise_id"]),
            metric_kind=candidate["metric_kind"],
            status="pending",
            limit=100,
        ).model_dump(mode="json"),
    )["data"]
    candidate_view = next(
        item for item in query["items"] if item["candidate_id"] == str(candidate["candidate_id"])
    )
    for index, picked in enumerate(selected, start=1):
        draft = post(
            "/api/v1/commands/record-decision-drafts",
            {
                "context": context(f"candidate-decision-{index}"),
                "candidate_id": str(picked["candidate_id"]),
                "decision": "accept",
                "technique_self_confirmation": True,
            },
            f"eg02-candidate-accept-{index}",
        )["data"]
        result = confirm_draft(
            draft, f"eg02-candidate-accept-{index}", "Подтверждаю кандидат рекорда"
        )
        assert result["status"] == "committed"
    for picked in selected:
        accepted_records = record_items(
            str(picked["exercise_id"]),
            picked["metric_kind"],
            "current",
            condition_id=str(picked["condition_id"]),
        )
        assert any(
            item["is_current"]
            and item["source_candidate_ref"]
            and item["source_candidate_ref"]["object_id"] == str(picked["candidate_id"])
            for item in accepted_records
        ), ("accepted candidate must materialize a current ActualRecord", picked, accepted_records)
    from record_comparison_boundaries import run as run_comparison_boundaries

    run_comparison_boundaries(report, selected, candidates)
    bodyweight_query = post(
        "/api/v1/queries/record-candidates",
        RecordCandidatesRequest(
            context=context("bodyweight-mass-candidate-query"),
            exercise_id=str(bodyweight_candidate["exercise_id"]),
            metric_kind="record_full_reps",
            status=(
                "accepted"
                if str(bodyweight_candidate["candidate_id"]) in selected_ids
                else "pending"
            ),
            limit=100,
        ).model_dump(mode="json"),
    )["data"]
    bodyweight_view = next(
        item
        for item in bodyweight_query["items"]
        if item["candidate_id"] == str(bodyweight_candidate["candidate_id"])
    )
    assert "body_mass_kg" not in bodyweight_view["condition"]
    bodyweight_history_ids = {
        created_record_id(
            create_historical(
                bodyweight_candidate,
                bodyweight_view["condition"],
                Decimal(str(value)),
                f"eg02-bodyweight-mass-{index}",
                body_mass_kg=mass,
            )
        )
        for index, (value, mass) in enumerate(((2, "70"), (3, "85"), (4, None)), start=1)
    }
    bodyweight_history = record_items(
        str(bodyweight_candidate["exercise_id"]),
        "record_full_reps",
        "all",
        condition_id=str(bodyweight_candidate["condition_id"]),
    )
    matched_bodyweight_records = [
        item for item in bodyweight_history if item["record_id"] in bodyweight_history_ids
    ]
    assert len(matched_bodyweight_records) == 3
    assert {item["condition"]["condition_id"] for item in matched_bodyweight_records} == {
        str(bodyweight_candidate["condition_id"])
    }
    observed_masses = {
        Decimal(item["body_mass_kg"]) if item["body_mass_kg"] is not None else None
        for item in matched_bodyweight_records
    }
    assert observed_masses == {Decimal("70"), Decimal("85"), None}
    base_value = Decimal(str(candidate_view["candidate_value"]["exact_value"]))
    better_value = (
        max(base_value - Decimal(5), Decimal("0.001"))
        if candidate["metric_kind"] == "record_cardio_time"
        else base_value + Decimal(5000)
    )
    worse_value = (
        base_value + Decimal(5)
        if candidate["metric_kind"] == "record_cardio_time"
        else max(base_value - Decimal(5), Decimal("0.001"))
    )
    create_historical(candidate, candidate_view["condition"], better_value, "eg02-history-high")
    current = record_items(
        str(candidate["exercise_id"]),
        candidate["metric_kind"],
        "current",
        condition_id=str(candidate["condition_id"]),
    )
    current_high = next(
        item
        for item in current
        if Decimal(item["value"]["exact_value"]) == better_value and item["is_current"]
    )
    high_id = current_high["record_id"]

    correction_payload = historical_input(
        candidate, candidate_view["condition"], worse_value, "eg02-history-corrected"
    )
    correction = post(
        "/api/v1/commands/historical-record-drafts",
        {
            "context": context("eg02-history-correct-draft"),
            "intent": "correct",
            "historical": correction_payload,
            "target_record_id": high_id,
            "expected_current": True,
        },
        "eg02-history-correct-key",
    )["data"]
    correction_result = confirm_draft(
        correction, "eg02-history-correct", "Подтверждаю исправление исторического результата"
    )
    assert correction_result["status"] == "committed"
    records = record_items(
        str(candidate["exercise_id"]),
        candidate["metric_kind"],
        condition_id=str(candidate["condition_id"]),
    )
    candidate_record = next(
        item
        for item in records
        if item["source_candidate_ref"]
        and item["source_candidate_ref"]["object_id"] == str(candidate["candidate_id"])
    )
    assert candidate_record["is_current"], (
        "CMD-24 correction must restore the previous current candidate",
        candidate,
        candidate_record,
        records,
    )
    assert any(item["record_id"] == high_id and not item["is_current"] for item in records)

    weaker = create_historical(
        candidate, candidate_view["condition"], worse_value, "eg02-history-weak"
    )
    weaker_id = created_record_id(weaker)
    stale_hist = post(
        "/api/v1/commands/historical-record-drafts",
        {
            "context": context("eg02-history-stale-draft"),
            "intent": "correct",
            "historical": historical_input(
                candidate, candidate_view["condition"], worse_value, "stale target"
            ),
            "target_record_id": weaker_id,
            "expected_current": False,
        },
        "eg02-history-stale-key",
    )["data"]
    stale_hist_preview = prepare_preview(stale_hist, "eg02-history-stale")
    extra_value = (
        worse_value + Decimal(1)
        if candidate["metric_kind"] == "record_cardio_time"
        else max(worse_value - Decimal(1), Decimal("0.001"))
    )
    create_historical(
        candidate, candidate_view["condition"], extra_value, "eg02-history-added-after-preview"
    )
    stale_hist_error = confirm_preview(
        stale_hist,
        stale_hist_preview,
        "eg02-history-stale",
        "Подтверждаю устаревшее историческое исправление",
        expected=409,
    )
    assert stale_hist_error["error"]["code"] == "stale_dependency"

    corrected_report = changed_report(report, selected)
    stale_correction = correction_draft(session_ref, corrected_report, "eg02-workout-stale")
    stale_correction_preview = prepare_preview(stale_correction, "eg02-workout-stale")
    plan = stale_correction_preview["canonical_payload"]["record_effects"]["preview_plan"]
    assert any(item.get("candidate_id") == str(pending_candidate["candidate_id"]) for item in plan)
    for picked in selected:
        assert any(
            item.get("record_id")
            and item.get("candidate_id") == str(picked["candidate_id"])
            and item["outcome"] == "invalidated_source"
            for item in plan
        )
    stale_fallback_value = (
        extra_value + Decimal(1)
        if candidate["metric_kind"] == "record_cardio_time"
        else max(extra_value - Decimal(1), Decimal("0.001"))
    )
    create_historical(
        candidate, candidate_view["condition"], stale_fallback_value, "eg02-stale-fallback-change"
    )
    stale_error = confirm_preview(
        stale_correction,
        stale_correction_preview,
        "eg02-workout-stale",
        "Подтверждаю устаревшее исправление тренировки",
        expected=409,
    )
    assert stale_error["error"]["code"] == "stale_dependency"

    final_draft = correction_draft(session_ref, corrected_report, "eg02-workout-correct")
    final_preview = prepare_preview(final_draft, "eg02-workout-correct")
    final_plan = final_preview["canonical_payload"]["record_effects"]["preview_plan"]
    corrected_values: dict[str, str] = {}
    for picked in selected:
        corrected_set = corrected_report["blocks"][picked["block_position"] - 1]["exercises"][
            picked["exercise_position"] - 1
        ]["sets"][picked["set_position"] - 1]
        if picked["metric_kind"] in {"record_hold_duration", "record_cardio_time"}:
            corrected_value = next(
                metric["value"]
                for metric in corrected_set["metrics"]
                if metric["metric_kind"] == "duration"
            )
        elif picked["metric_kind"] == "record_external_load":
            corrected_value = str(
                Decimal(
                    next(
                        load["mass"]
                        for load in corrected_set["loads"]
                        if load["load_kind"] == "external"
                    )
                )
            )
        else:
            corrected_value = str(corrected_set["full_reps"])
        corrected_values[str(picked["candidate_id"])] = corrected_value
        assert any(
            item.get("outcome") == "candidate_if_confirmed"
            and item.get("exercise_id") == str(picked["exercise_id"])
            and item.get("metric_kind") == picked["metric_kind"]
            and item.get("value") == corrected_value
            for item in final_plan
        ), (
            "CMD-07 preview must show each improved metric as a candidate against its fallback",
            picked,
            corrected_value,
            final_plan,
        )
    groups_with_current = [
        item for item in final_plan if item.get("outcome") == "recompute_current"
    ]
    assert any(item.get("fallback_winner_record_id") for item in groups_with_current)
    assert any(item.get("fallback_winner_record_id") is None for item in groups_with_current)
    final_commit = confirm_preview(
        final_draft,
        final_preview,
        "eg02-workout-correct",
        "Подтверждаю исправление тренировки с пересчётом рекордов",
    )
    assert final_commit["status"] == "committed"
    new_revision = next(
        item["revision_id"]
        for item in final_commit["created_revisions"]
        if item["object_type"] == "workout_session"
    )
    for picked in selected:
        new_candidates = post(
            "/api/v1/queries/record-candidates",
            RecordCandidatesRequest(
                context=context("candidate-corrected-query-" + str(picked["candidate_id"])),
                exercise_id=str(picked["exercise_id"]),
                metric_kind=picked["metric_kind"],
                status="pending",
                limit=100,
            ).model_dump(mode="json"),
        )["data"]["items"]
        assert any(
            item["source_session_ref"]["revision_id"] == new_revision
            and Decimal(item["candidate_value"]["exact_value"])
            == Decimal(corrected_values[str(picked["candidate_id"])])
            for item in new_candidates
        ), (
            "CMD-07 correction must create each improved candidate on its new session revision",
            picked,
            new_revision,
            corrected_values,
            new_candidates,
        )
    query_after = post(
        "/api/v1/queries/record-candidates",
        RecordCandidatesRequest(
            context=context("candidate-invalidated-query"),
            exercise_id=None,
            metric_kind=None,
            status="invalidated",
            limit=100,
        ).model_dump(mode="json"),
    )["data"]
    for picked in selected:
        assert any(
            item["candidate_id"] == str(picked["candidate_id"])
            and item["invalidated_reason"] == "source_corrected"
            for item in query_after["items"]
        )
    assert any(
        item["candidate_id"] == str(pending_candidate["candidate_id"])
        and item["invalidated_reason"] == "source_revision_superseded"
        for item in query_after["items"]
    )
    for entry in groups_with_current:
        group_records = record_items(
            entry["exercise_id"],
            entry["metric_kind"],
            "current",
            condition_id=entry["condition_id"],
        )
        winner_id = entry.get("fallback_winner_record_id")
        if winner_id:
            assert any(
                item["record_id"] == winner_id and item["is_current"] for item in group_records
            ), (entry, group_records)
        else:
            assert not group_records
    for picked in selected:
        all_group_records = record_items(
            str(picked["exercise_id"]),
            picked["metric_kind"],
            condition_id=str(picked["condition_id"]),
        )
        source_record = next(
            item
            for item in all_group_records
            if item["source_candidate_ref"]
            and item["source_candidate_ref"]["object_id"] == str(picked["candidate_id"])
        )
        assert not source_record["is_current"]
        assert source_record["validity"] == "source_invalidated"
    assert new_revision != session_ref["revision_id"]

    unknown = historical_input(
        candidate, candidate_view["condition"], worse_value, "unknown conditions"
    )
    unknown["condition"]["loads"] = []
    unknown["condition"]["unknown_fields"] = list(
        dict.fromkeys([*unknown["condition"]["unknown_fields"], "load"])
    )
    unknown_draft = post(
        "/api/v1/commands/historical-record-drafts",
        {"context": context("eg02-unknown-history"), "intent": "create", "historical": unknown},
        "eg02-unknown-history-key",
    )["data"]
    unknown_result = confirm_draft(
        unknown_draft,
        "eg02-unknown-history",
        "Подтверждаю историческую запись с неизвестными условиями",
    )
    unknown_id = created_record_id(unknown_result)
    all_records = record_items(str(candidate["exercise_id"]), candidate["metric_kind"])
    unknown_record = next(item for item in all_records if item["record_id"] == unknown_id)
    assert (
        not unknown_record["is_current"]
        and unknown_record["condition"]["scope"] == "historical_unknown"
    )
    assert all(
        item["record_id"] != unknown_id
        for item in record_items(str(candidate["exercise_id"]), candidate["metric_kind"], "current")
    )
    assert all(RecordResultItem.model_validate(item) for item in all_records)
    print("PASS: E-G02 candidate, CMD-07 source correction/fallback/stale")
    print("PASS: E-G02 CMD-24 correction/history and group-state stale checks")
    return {"object_id": session_ref["object_id"], "revision_id": new_revision}, corrected_report


if __name__ == "__main__":
    raise SystemExit("record_flow is called by workout_flow after a synthetic workout commit")
