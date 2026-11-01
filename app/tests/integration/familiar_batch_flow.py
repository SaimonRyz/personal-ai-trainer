"""Synthetic end-to-end gate for one familiar-exercise confirmation."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from context_flow import save_exercise_revision
from trainer.infrastructure.db.context_repository import ContextDatabaseExecutor
from trainer.transport.http.contracts import (
    ConfirmRequest, ExerciseCatalogDraftRequest, FamiliarExerciseBatchDraftRequest,
    PreviewRequest, PreviewShownRequest,
)

BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def context(message: str) -> dict[str, str]:
    return {"conversation_id": "synthetic-familiar-batch", "message_id": message,
            "effective_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "timezone": "Asia/Novokuznetsk", "locale": "ru"}


def post(path: str, body: dict, *, key: str | None = None, model=None, expected: int = 200) -> dict:
    if key is not None:
        normalized = model.model_validate({**body, "request_digest": "0" * 64}).model_dump(mode="json")
        normalized.pop("request_digest")
        body = {**normalized, "request_digest": digest(normalized)}
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Authorization": f"Bearer {TOKEN}", "X-Correlation-ID": str(uuid4()),
               "X-Causation-ID": str(uuid4())}
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == expected
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        result = json.loads(error.read())
        if error.code != expected:
            raise AssertionError(f"{path}: {error.code} {result}") from error
        return result


def new_card(name: str) -> dict:
    return {"exercise_id": None, "canonical_name": name, "description": None,
            "category_code": "strength", "unilateral": False, "status_code": "active",
            "comment": None, "aliases": [],
            "muscle_roles": [{"kind": "create_by_name", "name": "Тестовая общая группа",
                              "status_code": "active", "role": "primary"}],
            "measurements": [{"metric_kind": "repetitions", "requiredness": "required",
                              "unit_family": "count"}],
            "equipment_requirements": [{"equipment_type": "barbell", "optionality": "required",
                                        "alternative_group": None}]}


def main() -> None:
    profile = post("/api/v1/queries/profile-context",
                   {"context": context("profile-before"), "purpose": "synthetic test"})["data"]
    profile_ref = profile["profile_ref"]
    assert profile_ref is not None
    child_revisions = []
    for index in (1, 2):
        draft = post("/api/v1/commands/exercise-catalog-drafts", {
            "context": context(f"child-{index}"), "intent": "create",
            "exercise": new_card(f"Синтетическое пакетное упражнение {index}"),
            "expected_revision_id": None, "reason": None,
        }, key=f"familiar-child-{index}", model=ExerciseCatalogDraftRequest)["data"]
        child_revisions.append(draft["revision_id"])
    catalog_ref = save_exercise_revision(
        name="Синтетическое упражнение из каталога", alias="синтетическая альтернатива",
        prefix="familiar-batch-existing", intent="create",
    )
    items = [
        {"source_name": f"Упражнение {index}", "exercise_ref": None,
         "new_draft_revision_id": revision}
        for index, revision in enumerate(child_revisions, 1)
    ]
    if profile["familiar_exercises"]:
        items.append({"source_name": "Уже знакомое упражнение",
                      "exercise_ref": profile["familiar_exercises"][0]["exercise_ref"],
                      "new_draft_revision_id": None})
    items.append({"source_name": "Уже есть в каталоге", "exercise_ref": catalog_ref,
                  "new_draft_revision_id": None})
    body = {"context": context("batch"), "profile_id": profile_ref["object_id"],
            "expected_profile_revision_id": profile_ref["revision_id"], "items": items,
            "unresolved_names": ["Неоднозначное синтетическое упражнение"]}
    overlap = post("/api/v1/commands/familiar-exercise-batches", {
        **body, "unresolved_names": [items[0]["source_name"]],
    }, key="familiar-overlap", model=FamiliarExerciseBatchDraftRequest, expected=422)
    assert overlap["error"]["code"] == "validation_error"
    draft = post("/api/v1/commands/familiar-exercise-batches", body,
                 key="familiar-batch", model=FamiliarExerciseBatchDraftRequest)["data"]
    assert post("/api/v1/commands/familiar-exercise-batches", body,
                key="familiar-batch", model=FamiliarExerciseBatchDraftRequest)["data"] == draft
    preview = post("/api/v1/commands/previews", {
        "context": context("preview"), "revision_id": draft["revision_id"], "detail_level": "full",
    }, key="familiar-preview", model=PreviewRequest)["data"]
    assert "Новые карточки: 2" in preview["canonical_text"]
    assert "Уже есть в каталоге → Синтетическое упражнение из каталога: найдено; будет отмечено знакомым." in preview["canonical_text"]
    assert "Не будут записаны до уточнения: Неоднозначное синтетическое упражнение." in preview["canonical_text"]
    assert preview["canonical_text"].count("Тестовая общая группа") == 1
    assert "служебные ID" not in preview["canonical_text"]
    post(f"/api/v1/commands/previews/{preview['preview_id']}/shown", {
        "context": context("shown"), "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"], "shown_message_id": "shown",
        "displayed_text_digest": hashlib.sha256(preview["canonical_text"].encode()).hexdigest(),
        "cross_dialog": False,
    }, key="familiar-shown", model=PreviewShownRequest)
    confirmation = {"context": context("confirm"), "revision_id": draft["revision_id"],
                    "preview_digest": preview["digest"], "confirmation_kind": "normal",
                    "confirmation_message_id": "confirm", "explicit_intent": "подтверждаю пакет",
                    "confirmation_mode": "same_dialog", "referenced_preview_id": None,
                    "reshown_message_id": None}
    committed = post(f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
                     confirmation, key="familiar-confirm", model=ConfirmRequest)["data"]
    assert committed["status"] == "committed"
    replay = post(f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
                  confirmation, key="familiar-confirm", model=ConfirmRequest)["data"]
    assert replay["status"] == "already_committed"
    after = post("/api/v1/queries/profile-context",
                 {"context": context("profile-after"), "purpose": "synthetic test"})["data"]
    names = {item["name"] for item in after["familiar_exercises"]}
    assert {"Синтетическое пакетное упражнение 1",
            "Синтетическое пакетное упражнение 2",
            "Синтетическое упражнение из каталога"} <= names
    engine = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    with engine.connect() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM trainer.muscle_groups
            WHERE normalized_name='тестовая общая группа'
        """)).scalar_one() == 1
    print("PASS: one familiar batch confirmation, shared new group, profile read-back")

    rollback_child = post("/api/v1/commands/exercise-catalog-drafts", {
        "context": context("rollback-child"), "intent": "create",
        "exercise": new_card("Синтетическое упражнение для отката"),
        "expected_revision_id": None, "reason": None,
    }, key="familiar-rollback-child", model=ExerciseCatalogDraftRequest)["data"]
    rollback_batch = post("/api/v1/commands/familiar-exercise-batches", {
        "context": context("rollback-batch"), "profile_id": after["profile_ref"]["object_id"],
        "expected_profile_revision_id": after["profile_ref"]["revision_id"],
        "items": [{"source_name": "Для отката", "exercise_ref": None,
                   "new_draft_revision_id": rollback_child["revision_id"]}],
    }, key="familiar-rollback-batch", model=FamiliarExerciseBatchDraftRequest)["data"]
    rollback_preview = post("/api/v1/commands/previews", {
        "context": context("rollback-preview"), "revision_id": rollback_batch["revision_id"],
        "detail_level": "full",
    }, key="familiar-rollback-preview", model=PreviewRequest)["data"]
    post(f"/api/v1/commands/previews/{rollback_preview['preview_id']}/shown", {
        "context": context("rollback-shown"), "revision_id": rollback_batch["revision_id"],
        "preview_digest": rollback_preview["digest"], "shown_message_id": "rollback-shown",
        "displayed_text_digest": hashlib.sha256(rollback_preview["canonical_text"].encode()).hexdigest(),
        "cross_dialog": False,
    }, key="familiar-rollback-shown", model=PreviewShownRequest)
    rollback_confirmation = {"context": context("rollback-confirm"),
                             "revision_id": rollback_batch["revision_id"],
                             "preview_digest": rollback_preview["digest"],
                             "confirmation_kind": "normal", "confirmation_message_id": "rollback-confirm",
                             "explicit_intent": "подтверждаю пакет", "confirmation_mode": "same_dialog",
                             "referenced_preview_id": None, "reshown_message_id": None}
    normalized = ConfirmRequest.model_validate({
        **rollback_confirmation, "request_digest": "0" * 64,
    }).model_dump(mode="json")
    normalized.pop("request_digest")
    normalized["request_digest"] = digest(normalized)
    try:
        ContextDatabaseExecutor(engine)(
            action="confirm", preview_id=UUID(rollback_preview["preview_id"]),
            payload=normalized, key="familiar-fault", correlation_id=uuid4(),
            causation_id=uuid4(), fail_before_commit=True,
        )
    except RuntimeError as error:
        assert "synthetic fault" in str(error)
    else:
        raise AssertionError("synthetic fault did not abort the transaction")
    after_fault = post("/api/v1/queries/profile-context", {
        "context": context("profile-after-fault"), "purpose": "synthetic test",
    })["data"]
    assert after_fault["profile_ref"] == after["profile_ref"]
    with engine.connect() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM trainer.exercises WHERE active_normalized_name='синтетическое упражнение для отката'
        """)).scalar_one() == 0
    print("PASS: fault before commit rolls back exercise and familiarity together")

    stale_child = post("/api/v1/commands/exercise-catalog-drafts", {
        "context": context("stale-child"), "intent": "create",
        "exercise": new_card("Синтетическое устаревшее упражнение"),
        "expected_revision_id": None, "reason": None,
    }, key="familiar-stale-child", model=ExerciseCatalogDraftRequest)["data"]
    stale_batch = post("/api/v1/commands/familiar-exercise-batches", {
        "context": context("stale-batch"), "profile_id": after["profile_ref"]["object_id"],
        "expected_profile_revision_id": after["profile_ref"]["revision_id"],
        "items": [{"source_name": "Устареет", "exercise_ref": None,
                   "new_draft_revision_id": stale_child["revision_id"]}],
    }, key="familiar-stale-batch", model=FamiliarExerciseBatchDraftRequest)["data"]
    stale_preview = post("/api/v1/commands/previews", {
        "context": context("stale-preview"), "revision_id": stale_batch["revision_id"],
        "detail_level": "full",
    }, key="familiar-stale-preview", model=PreviewRequest)["data"]
    post(f"/api/v1/commands/previews/{stale_preview['preview_id']}/shown", {
        "context": context("stale-shown"), "revision_id": stale_batch["revision_id"],
        "preview_digest": stale_preview["digest"], "shown_message_id": "stale-shown",
        "displayed_text_digest": hashlib.sha256(stale_preview["canonical_text"].encode()).hexdigest(),
        "cross_dialog": False,
    }, key="familiar-stale-shown", model=PreviewShownRequest)
    assert post(f"/api/v1/commands/previews/{rollback_preview['preview_id']}/confirm",
                rollback_confirmation, key="familiar-rollback-confirm",
                model=ConfirmRequest)["data"]["status"] == "committed"
    stale_confirmation = {"context": context("stale-confirm"),
                          "revision_id": stale_batch["revision_id"],
                          "preview_digest": stale_preview["digest"],
                          "confirmation_kind": "normal", "confirmation_message_id": "stale-confirm",
                          "explicit_intent": "подтверждаю пакет", "confirmation_mode": "same_dialog",
                          "referenced_preview_id": None, "reshown_message_id": None}
    rejected = post(f"/api/v1/commands/previews/{stale_preview['preview_id']}/confirm",
                    stale_confirmation, key="familiar-stale-confirm",
                    model=ConfirmRequest, expected=409)
    assert rejected["error"]["code"] == "stale_preview"
    with engine.connect() as connection:
        assert connection.execute(text("""
            SELECT count(*) FROM trainer.exercises WHERE active_normalized_name='синтетическое устаревшее упражнение'
        """)).scalar_one() == 0
    print("PASS: changed profile revision rejects old batch without partial writes")


if __name__ == "__main__":
    main()
