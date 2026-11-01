"""Isolated synthetic gate for exact, atomic historical workout import."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from trainer.infrastructure.db.context_repository import ContextDatabaseExecutor
from trainer.transport.http.contracts import (
    ConfirmRequest, PreviewRequest, PreviewShownRequest, WorkoutImportBatchDraftRequest,
    WorkoutReportDraftRequest, WorkoutCorrectionRequest, WorkoutDraftRevisionRequest,
)

BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]
ENGINE = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
CONVERSATION = "synthetic-workout-import-batch"
RUN_ID = uuid4().hex[:8]


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def context(message: str) -> dict[str, str]:
    return {"conversation_id": CONVERSATION, "message_id": message,
            "effective_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "timezone": "Asia/Novokuznetsk", "locale": "ru"}


def post(path: str, body: dict, *, key: str | None = None, model=None,
         expected: int = 200) -> dict:
    if key is not None:
        normalized = model.model_validate({**body, "request_digest": "0" * 64}).model_dump(mode="json")
        normalized.pop("request_digest")
        if model is PreviewRequest:
            for field in ("entry_name", "entry_key"):
                if normalized.get(field) is None:
                    normalized.pop(field)
        if model is WorkoutImportBatchDraftRequest:
            for field in ("draft_id", "expected_revision_id"):
                if normalized.get(field) is None:
                    normalized.pop(field)
        body = {**normalized, "request_digest": digest(normalized)}
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Authorization": f"Bearer {TOKEN}", "X-Correlation-ID": str(uuid4()),
               "X-Causation-ID": str(uuid4())}
    if key:
        headers["Idempotency-Key"] = key + "-" + RUN_ID
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == expected
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        result = json.loads(error.read())
        if error.code != expected:
            raise AssertionError(f"{path}: {error.code} {result}") from error
        return result


def report(date: str, name: str, exercise_id: str, reps: int) -> dict:
    return {"plan_id": None, "plan_revision_id": None, "started_at": None,
            "ended_at": None, "local_start_date": date, "duration_seconds": None,
            "session_type": "training", "name": name, "lifecycle": "completed",
            "effort": None, "comment": None, "workout_metrics": [],
            "confirmed_unknown_planned_exercise_ids": [],
            "confirmed_unknown_planned_set_ids": [],
            "confirmed_unknown_planned_load_component_ids": [],
            "confirmed_unknown_metrics": [],
            "blocks": [{"position": 1, "planned_block_id": None,
                        "block_type_code": "straight", "declared_rounds": None,
                        "rest_seconds": None, "comment": None,
                        "exercises": [{"position": 1, "exercise_id": exercise_id,
                                       "planned_exercise_id": None, "result": "added",
                                       "replacement_source": None, "reason": None,
                                       "sets": [{"position": 1, "logical_set_no": 1,
                                                 "set_type_code": "working", "full_reps": reps,
                                                 "status": "performed", "metrics": [],
                                                 "loads": []}]}]}]}


def entry(key: str, name: str, date: str, revision: str) -> dict:
    return {"entry_key": key, "name": name, "local_start_date": date,
            "decision": "create", "report_draft_revision_id": revision, "reason": None,
            "fact_sources": [
                {"field_path": "/report/local_start_date", "source_locator": f"{key}:date",
                 "source_excerpt": date},
                {"field_path": "/report/name", "source_locator": f"{key}:name",
                 "source_excerpt": name},
                {"field_path": "/report/lifecycle", "source_locator": f"{key}:session",
                 "source_excerpt": "тренировка завершена"},
                {"field_path": "/report/blocks/1/block_type_code", "source_locator": f"{key}:block",
                 "source_excerpt": "обычный блок"},
                {"field_path": "/report/blocks/1/exercises/1/result", "source_locator": f"{key}:exercise",
                 "source_excerpt": "упражнение выполнено"},
                {"field_path": "/report/blocks/1/exercises/1/sets/1",
                 "source_locator": f"{key}:set", "source_excerpt": "синтетический подход"},
                {"field_path": "/report/blocks/1/exercises/1/sets/1/set_type_code",
                 "source_locator": f"{key}:set", "source_excerpt": "рабочий подход"},
                {"field_path": "/report/blocks/1/exercises/1/sets/1/status",
                 "source_locator": f"{key}:set", "source_excerpt": "подход выполнен"},
                {"field_path": "/report/blocks/1/exercises/1/sets/1/full_reps",
                 "source_locator": f"{key}:set", "source_excerpt": "синтетические повторения"},
            ]}


def show(draft: dict, prefix: str) -> dict:
    preview = post("/api/v1/commands/previews", {
        "context": context(prefix + "-preview"), "revision_id": draft["revision_id"],
        "detail_level": "full"}, key=prefix + "-preview", model=PreviewRequest)["data"]
    post(f"/api/v1/commands/previews/{preview['preview_id']}/shown", {
        "context": context(prefix + "-shown"), "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"], "shown_message_id": prefix + "-shown",
        "displayed_text_digest": hashlib.sha256(preview["canonical_text"].encode()).hexdigest(),
        "cross_dialog": False}, key=prefix + "-shown", model=PreviewShownRequest)
    return preview


def confirm_body(draft: dict, preview: dict, prefix: str) -> dict:
    return {"context": context(prefix + "-confirm"), "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"], "confirmation_kind": "normal",
            "confirmation_message_id": prefix + "-confirm", "explicit_intent": "подтверждаю пакет",
            "confirmation_mode": "same_dialog", "referenced_preview_id": None,
            "reshown_message_id": None}


def main() -> None:
    profile = post("/api/v1/queries/profile-context", {
        "context": context("profile"), "purpose": "synthetic batch gate"})["data"]
    ref = profile["profile_ref"]
    assert ref is not None
    with ENGINE.connect() as db:
        exercise = db.execute(text("""
            SELECT exercise_id FROM trainer.exercises
            WHERE current_revision_id IS NOT NULL AND archived_at IS NULL
            ORDER BY created_at LIMIT 1
        """)).scalar_one()
    first = post("/api/v1/commands/workout-report-drafts", {
        "context": context("report-a"), "report": report("2026-10-14", "Синтетическая A", str(exercise), 5)
    }, key="batch-report-a", model=WorkoutReportDraftRequest)["data"]
    second = post("/api/v1/commands/workout-report-drafts", {
        "context": context("report-b"), "report": report("2026-10-15", "Синтетическая B", str(exercise), 6)
    }, key="batch-report-b", model=WorkoutReportDraftRequest)["data"]
    wrong_name = entry("wrong-name", "Подменённое название", "2026-10-14", first["revision_id"])
    rejected_name = post("/api/v1/commands/workout-import-batches", {
        "context": context("wrong-name"), "profile_id": ref["object_id"],
        "expected_profile_revision_id": ref["revision_id"], "part_label": "ошибка названия",
        "entries": [wrong_name]}, key="batch-wrong-name", model=WorkoutImportBatchDraftRequest,
        expected=422)
    assert rejected_name["error"]["code"] == "validation_error"
    assert rejected_name["error"]["field_errors"][0]["code"] == "report_name_mismatch"
    missing_source = entry("missing-source", "Синтетическая A", "2026-10-14", first["revision_id"])
    missing_source["fact_sources"] = [source for source in missing_source["fact_sources"]
                                      if not source["field_path"].endswith("/full_reps")]
    rejected_source = post("/api/v1/commands/workout-import-batches", {
        "context": context("missing-source"), "profile_id": ref["object_id"],
        "expected_profile_revision_id": ref["revision_id"], "part_label": "ошибка источника",
        "entries": [missing_source]}, key="batch-missing-source",
        model=WorkoutImportBatchDraftRequest, expected=422)
    assert rejected_source["error"]["field_errors"][0]["code"] == "missing_fact_source"
    body = {"context": context("batch"), "profile_id": ref["object_id"],
            "expected_profile_revision_id": ref["revision_id"], "part_label": "октябрь",
            "entries": [entry("a", "Синтетическая A", "2026-10-14", first["revision_id"]),
                        entry("b", "Синтетическая B", "2026-10-15", second["revision_id"]),
                        {"entry_key": "c", "name": "Запись без даты", "local_start_date": None,
                         "decision": "unresolved", "report_draft_revision_id": None,
                         "reason": "дата не указана", "fact_sources": [
                             {"field_path": "/source", "source_locator": "c:line",
                              "source_excerpt": "синтетическая запись"}]}]}
    draft = post("/api/v1/commands/workout-import-batches", body,
                 key="batch-draft", model=WorkoutImportBatchDraftRequest)["data"]
    assert post("/api/v1/commands/workout-import-batches", body,
                key="batch-draft", model=WorkoutImportBatchDraftRequest)["data"] == draft
    detail = post("/api/v1/commands/previews", {
        "context": context("detail"), "revision_id": draft["revision_id"],
        "detail_level": "workout", "entry_key": "a"},
        key="batch-detail", model=PreviewRequest)["data"]
    assert detail["complete"] is False and "5 полных повторений" in detail["canonical_text"]
    before_count = post("/api/v1/queries/workouts/history", {
        "context": context("history-before"), "mode": "range", "date_from": "2026-10-14",
        "date_to": "2026-10-15", "limit": 50})["data"]["items"]
    assert before_count == []
    preview = show(draft, "batch")
    assert detail["canonical_payload"] == preview["canonical_payload"]
    assert "2026-10-14" in preview["canonical_text"]
    assert "2026-10-15" in preview["canonical_text"]
    assert "Запись без даты" in preview["canonical_text"]
    payload = ConfirmRequest.model_validate({**confirm_body(draft, preview, "fault"),
                                             "request_digest": "0" * 64}).model_dump(mode="json")
    payload.pop("request_digest")
    payload["request_digest"] = digest(payload)
    try:
        ContextDatabaseExecutor(ENGINE)(
            action="confirm", preview_id=UUID(preview["preview_id"]), payload=payload,
            key="batch-fault-" + RUN_ID, correlation_id=uuid4(), causation_id=uuid4(),
            fail_before_commit=True)
    except RuntimeError as error:
        assert "synthetic fault" in str(error)
    else:
        raise AssertionError("fault did not roll back batch")
    assert post("/api/v1/queries/workouts/history", {
        "context": context("history-after-fault"), "mode": "range", "date_from": "2026-10-14",
        "date_to": "2026-10-15", "limit": 50})["data"]["items"] == []
    confirmation = confirm_body(draft, preview, "batch")
    committed = post(f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
                     confirmation, key="batch-confirm", model=ConfirmRequest)["data"]
    assert committed["status"] == "committed"
    replay = post(f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
                  confirmation, key="batch-confirm", model=ConfirmRequest)["data"]
    assert replay["status"] == "already_committed"
    history = post("/api/v1/queries/workouts/history", {
        "context": context("history-after"), "mode": "range", "date_from": "2026-10-14",
        "date_to": "2026-10-15", "limit": 50})["data"]["items"]
    assert len(history) == 2
    assert {item["local_start_date"] for item in history} == {"2026-10-14", "2026-10-15"}
    assert all(any(source["kind"] == "historical_import_source"
                   for source in item["provenance"]) for item in history)
    with ENGINE.connect() as db:
        assert db.execute(text("SELECT count(*) FROM trainer.record_candidates")).scalar_one() == 0
    duplicate = post("/api/v1/commands/workout-report-drafts", {
        "context": context("duplicate-child"),
        "report": report("2026-10-14", "Синтетический отдельный отчёт", str(exercise), 3),
    }, key="batch-duplicate-child", model=WorkoutReportDraftRequest)["data"]
    duplicate_body = {"context": context("duplicate-batch"), "profile_id": ref["object_id"],
                      "expected_profile_revision_id": ref["revision_id"],
                      "part_label": "дубль", "entries": [entry(
                          "duplicate", "Синтетический отдельный отчёт", "2026-10-14",
                          duplicate["revision_id"])]}
    rejected = post("/api/v1/commands/workout-import-batches", duplicate_body,
                    key="batch-duplicate-reject", model=WorkoutImportBatchDraftRequest,
                    expected=422)
    assert rejected["error"]["code"] == "validation_error"
    duplicate_body["entries"][0]["reason"] = "явно отдельная синтетическая тренировка"
    allowed = post("/api/v1/commands/workout-import-batches", duplicate_body,
                   key="batch-duplicate-allowed", model=WorkoutImportBatchDraftRequest)["data"]
    assert allowed["revision_id"]

    stale_report = report("2026-10-16", "Синтетическая устаревающая", str(exercise), 8)
    stale_child = post("/api/v1/commands/workout-report-drafts", {
        "context": context("stale-child"), "report": stale_report,
    }, key="batch-stale-child", model=WorkoutReportDraftRequest)["data"]
    stale_batch = post("/api/v1/commands/workout-import-batches", {
        "context": context("stale-batch"), "profile_id": ref["object_id"],
        "expected_profile_revision_id": ref["revision_id"], "part_label": "устаревание",
        "entries": [entry("stale", "Синтетическая устаревающая", "2026-10-16",
                          stale_child["revision_id"])]}, key="batch-stale-batch",
        model=WorkoutImportBatchDraftRequest)["data"]
    revised_report = report("2026-10-16", "Синтетическая устаревающая", str(exercise), 9)
    post(f"/api/v1/commands/drafts/{stale_child['draft_id']}/revisions", {
        "context": context("stale-revision"), "base_revision_id": stale_child["revision_id"],
        "changes": [{"op": "replace", "path": "/report", "value": revised_report}]},
        key="batch-stale-revision", model=WorkoutDraftRevisionRequest)
    stale_error = post("/api/v1/commands/previews", {
        "context": context("stale-preview"), "revision_id": stale_batch["revision_id"],
        "detail_level": "full"}, key="batch-stale-preview", model=PreviewRequest,
        expected=409)
    assert stale_error["error"]["code"] == "stale_preview"

    source = next(item for item in history if item["local_start_date"] == "2026-10-14")
    corrected = post("/api/v1/commands/correction-drafts", {
        "context": context("correction-child"), "target": source["session_ref"],
        "patch": [{"op": "replace", "path": "/report", "value": report(
            "2026-10-14", "Синтетическая A", str(exercise), 7)}],
        "reason": "исправлены синтетические повторения"},
        key="batch-correction-child", model=WorkoutCorrectionRequest)["data"]
    correction_entry = entry("correct", "Синтетическая A", "2026-10-14",
                             corrected["revision_id"])
    correction_entry["decision"] = "correct"
    correction_entry["reason"] = "исправлены синтетические повторения"
    correction_batch = post("/api/v1/commands/workout-import-batches", {
        "context": context("correction-batch"), "profile_id": ref["object_id"],
        "expected_profile_revision_id": ref["revision_id"], "part_label": "исправление",
        "entries": [correction_entry]}, key="batch-correction-batch",
        model=WorkoutImportBatchDraftRequest)["data"]
    correction_preview = show(correction_batch, "correction")
    result = post(f"/api/v1/commands/previews/{correction_preview['preview_id']}/confirm",
                  confirm_body(correction_batch, correction_preview, "correction"),
                  key="batch-correction-confirm", model=ConfirmRequest)["data"]
    assert result["status"] == "committed"
    final_history = post("/api/v1/queries/workouts/history", {
        "context": context("history-final"), "mode": "range", "date_from": "2026-10-14",
        "date_to": "2026-10-15", "limit": 50})["data"]["items"]
    assert len(final_history) == 3
    assert len([item for item in final_history if item["current"]]) == 2

    revision_child = post("/api/v1/commands/workout-report-drafts", {
        "context": context("revision-child"),
        "report": report("2026-10-17", "Синтетическая новая", str(exercise), 4),
    }, key="batch-revision-child", model=WorkoutReportDraftRequest)["data"]
    revision_body = {"context": context("revision-batch"), "profile_id": ref["object_id"],
                     "expected_profile_revision_id": ref["revision_id"],
                     "part_label": "проверка редакции", "entries": [entry(
                         "revision", "Синтетическая новая", "2026-10-17",
                         revision_child["revision_id"])]}
    initial = post("/api/v1/commands/workout-import-batches", revision_body,
                   key="batch-revision-initial", model=WorkoutImportBatchDraftRequest)["data"]
    old_preview = show(initial, "revision-old")
    revision_body["entries"][0]["reason"] = "уточнён источник записи"
    revision_body["draft_id"] = initial["draft_id"]
    revision_body["expected_revision_id"] = initial["revision_id"]
    updated = post("/api/v1/commands/workout-import-batches", revision_body,
                   key="batch-revision-updated", model=WorkoutImportBatchDraftRequest)["data"]
    assert updated["revision_no"] == initial["revision_no"] + 1
    stale_confirmation = post(f"/api/v1/commands/previews/{old_preview['preview_id']}/confirm",
                              confirm_body(initial, old_preview, "revision-old"),
                              key="batch-revision-old-confirm", model=ConfirmRequest,
                              expected=409)
    assert stale_confirmation["error"]["code"] == "stale_preview"
    new_preview = post("/api/v1/commands/previews", {
        "context": context("revision-new-preview"), "revision_id": updated["revision_id"],
        "detail_level": "full"}, key="batch-revision-new-preview",
        model=PreviewRequest)["data"]
    assert "уточнён источник записи" in new_preview["canonical_text"]
    print("PASS: atomic import, rollback, replay, provenance, duplicate choice, stale child, correction, batch revision")


if __name__ == "__main__":
    main()
