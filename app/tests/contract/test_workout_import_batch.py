from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.infrastructure.db.workout_import_batch import WorkoutImportBatchRepository
from trainer.infrastructure.db.context_repository import ContextRepositoryError
from trainer.transport.http.contracts import PreviewRequest, WorkoutImportBatchDraftRequest


def _entry(*, decision: str = "create", date: str | None = "2026-10-14") -> dict:
    return {
        "entry_key": "source-1",
        "name": "Тренировка 14 октября",
        "local_start_date": date,
        "decision": decision,
        "report_draft_revision_id": str(uuid4()) if decision in {"create", "correct"} else None,
        "reason": "нет точной даты" if decision == "unresolved" else None,
        "fact_sources": [{"field_path": "/report/local_start_date" if date else "/source",
                          "source_locator": "строка 1", "source_excerpt": "14 октября"}],
    }


def _request(entries: list[dict]) -> dict:
    return {"context": {"conversation_id": "conversation-1", "message_id": "message-1",
                        "effective_at": "2026-11-01T00:00:00+07:00",
                        "timezone": "Asia/Novokuznetsk"},
            "profile_id": str(uuid4()), "expected_profile_revision_id": str(uuid4()),
            "part_label": "октябрь", "entries": entries, "request_digest": "0" * 64}


def test_batch_requires_exact_dates_sources_and_unique_keys() -> None:
    WorkoutImportBatchDraftRequest.model_validate(_request([_entry()]))
    with pytest.raises(ValidationError):
        WorkoutImportBatchDraftRequest.model_validate(_request([_entry(date=None)]))
    with pytest.raises(ValidationError):
        WorkoutImportBatchDraftRequest.model_validate(_request([_entry(), _entry()]))
    with pytest.raises(ValidationError):
        WorkoutImportBatchDraftRequest.model_validate(_request([_entry(decision="unresolved", date=None)]))


def test_workout_detail_requires_entry_key() -> None:
    base = {"context": {"conversation_id": "conversation-1", "message_id": "message-1",
                        "effective_at": "2026-11-01T00:00:00+07:00",
                        "timezone": "Asia/Novokuznetsk"},
            "revision_id": str(uuid4()), "detail_level": "workout", "request_digest": "0" * 64}
    with pytest.raises(ValidationError):
        PreviewRequest.model_validate(base)
    PreviewRequest.model_validate({**base, "entry_key": "source-1"})


def test_overview_lists_every_entry_and_result() -> None:
    text = WorkoutImportBatchRepository.render({
        "part_label": "октябрь", "entries": [
            {"entry_key": "a", "local_start_date": "2026-10-14", "name": "Жим",
             "decision": "create", "reason": None,
             "exercise_summaries": ["Жим лёжа (1 повт. × 85 кг)"]},
            {"entry_key": "b", "local_start_date": None, "name": "Запись без даты",
             "decision": "unresolved", "reason": "дата не указана"},
        ],
    })
    assert "2026-10-14" in text and "Жим лёжа (1 повт. × 85 кг)" in text
    assert "Запись без даты" in text and "Не записывать: 1" in text


def test_reported_values_need_individual_source_paths() -> None:
    report = {"name": "Ходьба", "blocks": [{"position": 1, "exercises": [{
        "position": 1, "sets": [{"position": 1, "full_reps": None,
        "metrics": [{"metric_kind": "distance", "value": "1000", "source_kind": "user"}],
        "loads": []}],
    }]}]}
    with pytest.raises(ContextRepositoryError, match="/report/name"):
        WorkoutImportBatchRepository._check_fact_sources({"name": "Ходьба", "blocks": []}, set())
    with pytest.raises(ContextRepositoryError, match="distance/value"):
        WorkoutImportBatchRepository._check_fact_sources(report, {"/report/name"})
    WorkoutImportBatchRepository._check_fact_sources(report, {
        "/report/name", "/report/blocks/1/exercises/1/sets/1/metrics/distance/value",
    })


def test_batch_summary_keeps_duration_and_distance() -> None:
    class Result:
        def scalar_one_or_none(self) -> str:
            return "Ходьба"

    class Session:
        def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    class Repository:
        session = Session()

    summary = WorkoutImportBatchRepository(Repository())._exercise_summaries({
        "blocks": [{"exercises": [{"exercise_id": str(uuid4()),
            "exercise_revision_id": str(uuid4()), "sets": [{"full_reps": None,
                "metrics": [{"metric_kind": "duration", "value": "600", "unit_code": "second"},
                            {"metric_kind": "distance", "value": "1000", "unit_code": "meter"}],
                "loads": []}]}]}],
    })
    assert summary == ["Ходьба (600 с × 1000 м)"]
