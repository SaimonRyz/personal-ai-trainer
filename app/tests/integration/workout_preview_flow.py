"""Synthetic HTTP gate for a readable, unconfirmed workout report preview."""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from context_flow import save_exercise_revision
from trainer.domain.context import digest_payload
from trainer.transport.http.contracts import PreviewRequest, WorkoutReportDraftRequest


BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]


def context(message: str) -> dict[str, str]:
    return {"conversation_id": "synthetic-workout-preview", "message_id": message,
            "effective_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "timezone": "Asia/Novokuznetsk", "locale": "ru"}


def post(path: str, body: dict, model: type, key: str) -> dict:
    normalized = model.model_validate({**body, "request_digest": "0" * 64}).model_dump(mode="json")
    normalized.pop("request_digest")
    if model is PreviewRequest:
        normalized.pop("entry_name", None)
        normalized.pop("entry_key", None)
    normalized["request_digest"] = digest_payload(normalized)
    request = urllib.request.Request(
        BASE + path, data=json.dumps(normalized).encode(), method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key,
                 "X-Correlation-ID": str(uuid4()), "X-Causation-ID": str(uuid4())},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())["data"]


def actual_set(position: int, mass: str, technique: str) -> dict:
    return {"position": position, "logical_set_no": position, "round_no": None,
            "side": None, "set_type_code": "working", "full_reps": 1,
            "partial_reps": None, "failed_attempts": None, "reported_rpe": None,
            "reported_rir": None, "failure_reached": None, "rom_state": None,
            "technique_state": technique, "tempo": None, "pause_seconds": None,
            "rest_after_seconds": None, "status": "performed", "comment": None,
            "metrics": [{"metric_kind": "repetitions", "value": "1",
                         "unit_code": "repetition", "source_kind": "user"}],
            "loads": [{"position": 1, "load_kind": "external", "mass": mass,
                       "unit_code": "kilogram", "mass_meaning": "total_external_mass"}]}


def main() -> None:
    ref = save_exercise_revision(
        name="Синтетический жим для предпросмотра", alias="синтетический жим",
        prefix="workout-preview-exercise", intent="create",
    )
    report = {"plan_id": None, "plan_revision_id": None, "started_at": None,
              "ended_at": None, "local_start_date": "2026-10-14",
              "duration_seconds": None, "session_type": "training", "name": None,
              "lifecycle": "completed", "effort": None, "comment": None,
              "workout_metrics": [], "confirmed_unknown_planned_exercise_ids": [],
              "confirmed_unknown_planned_set_ids": [],
              "confirmed_unknown_planned_load_component_ids": [],
              "confirmed_unknown_metrics": [], "blocks": [{
                  "position": 1, "planned_block_id": None, "block_type_code": "straight",
                  "declared_rounds": None, "rest_seconds": None, "comment": None,
                  "exercises": [{"position": 1, "exercise_id": ref["object_id"],
                                 "planned_exercise_id": None, "result": "added",
                                 "replacement_source": None, "reason": None,
                                 "sets": [actual_set(1, "85", "строго"),
                                          actual_set(2, "87", "отличалась техника")] }],
              }]}
    draft = post("/api/v1/commands/workout-report-drafts", {
        "context": context("draft"), "report": report,
    }, WorkoutReportDraftRequest, "workout-preview-draft")
    preview = post("/api/v1/commands/previews", {
        "context": context("preview"), "revision_id": draft["revision_id"],
        "detail_level": "full",
    }, PreviewRequest, "workout-preview-full")
    text = preview["canonical_text"]
    assert preview["complete"] is True
    assert "Сохранить тренировку за 14.10.2026?" in text
    assert "Синтетический жим для предпросмотра" in text
    assert "85 кг" in text and "87 кг" in text
    assert "строго" in text and "отличалась техника" in text
    assert "Block type code" not in text and "Mass meaning" not in text
    assert "profile timezone for workout local date" not in text
    print("PASS: readable synthetic workout report preview; report not confirmed")


if __name__ == "__main__":
    main()
