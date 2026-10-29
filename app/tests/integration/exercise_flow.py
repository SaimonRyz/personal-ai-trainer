from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from trainer.transport.http.contracts import (
    ConfirmRequest,
    ExerciseCatalogDraftRequest,
    PreviewRequest,
    PreviewShownRequest,
)

BASE_URL = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def context(message: str) -> dict[str, Any]:
    return {
        "conversation_id": "synthetic-d-g03",
        "message_id": message,
        "effective_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "timezone": "Asia/Novokuznetsk",
        "locale": "ru",
    }


def post(
    path: str,
    body: dict[str, Any],
    *,
    key: str | None = None,
    expected: int = 200,
    validate_request: bool = True,
) -> dict[str, Any]:
    if key is not None:
        model = (
            ConfirmRequest
            if path.endswith("/confirm")
            else PreviewShownRequest
            if path.endswith("/shown")
            else PreviewRequest
            if path.endswith("/previews")
            else ExerciseCatalogDraftRequest
        )
        if validate_request:
            normalized = model.model_validate({**body, "request_digest": "0" * 64}).model_dump(
                mode="json"
            )
            normalized.pop("request_digest")
        else:
            normalized = body
        body = {**normalized, "request_digest": digest(normalized)}
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
        BASE_URL + path, data=json.dumps(body).encode(), headers=headers, method="POST"
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


def exercise(
    name: str, *, exercise_id: str | None = None, alias: str = "жим штанги"
) -> dict[str, Any]:
    return {
        "exercise_id": exercise_id,
        "canonical_name": name,
        "description": "Синтетическое упражнение",
        "category_code": "strength",
        "unilateral": False,
        "status_code": "active",
        "comment": None,
        "aliases": [{"alias": alias, "locale": "ru", "context": None}],
        "muscle_roles": [
            {
                "kind": "create_by_name",
                "name": "Грудные мышцы",
                "status_code": "active",
                "role": "primary",
            }
        ],
        "measurements": [
            {"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}
        ],
        "equipment_requirements": [
            {
                "equipment_type": "barbell",
                "optionality": "alternative",
                "alternative_group": "external_load",
            },
            {
                "equipment_type": "dumbbell",
                "optionality": "alternative",
                "alternative_group": "external_load",
            },
        ],
    }


def draft_and_confirm(body: dict[str, Any], prefix: str) -> tuple[dict[str, Any], dict[str, Any]]:
    draft_request = {"context": context(prefix + "-draft"), **body}
    draft = post(
        "/api/v1/commands/exercise-catalog-drafts",
        draft_request,
        key=prefix + "-draft",
    )["data"]
    assert (
        post(
            "/api/v1/commands/exercise-catalog-drafts",
            draft_request,
            key=prefix + "-draft",
        )["data"]
        == draft
    )
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        key=prefix + "-preview",
    )["data"]
    shown = prefix + "-shown"
    post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context(shown),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": shown,
            "displayed_text_digest": hashlib.sha256(preview["canonical_text"].encode()).hexdigest(),
            "cross_dialog": False,
        },
        key=shown,
    )
    confirm_body = {
        "context": context(prefix + "-confirm"),
        "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"],
        "confirmation_kind": "normal",
        "confirmation_message_id": prefix + "-confirm",
        "explicit_intent": "подтверждаю",
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }
    committed = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        confirm_body,
        key=prefix + "-confirm",
    )["data"]
    replay = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        confirm_body,
        key=prefix + "-confirm",
    )["data"]
    assert replay["status"] == "already_committed"
    assert replay["commit_result_id"] == committed["commit_result_id"]
    return preview, committed


def main() -> None:
    engine = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    with engine.begin() as connection:
        category = connection.execute(
            text("""
                SELECT description,seed_version,archived_at
                FROM trainer.exercise_category_catalog WHERE code='strength'
            """)
        ).one()
        assert category == ("Strength exercise", 1, None)
        for table, code in (
            ("metric_kind_catalog", "repetitions"),
            ("unit_family_catalog", "count"),
            ("equipment_type_catalog", "barbell"),
            ("equipment_type_catalog", "dumbbell"),
        ):
            connection.execute(
                text(
                    f"INSERT INTO trainer.{table}(code,description,seed_version,created_at) "
                    "VALUES (:code,'synthetic',1,now()) ON CONFLICT(code) DO NOTHING"
                ),
                {"code": code},
            )

    first_preview, first_commit = draft_and_confirm(
        {
            "intent": "create",
            "exercise": exercise("Жим лёжа"),
            "expected_revision_id": None,
            "reason": "synthetic",
        },
        "exercise-create",
    )
    exercise_ref = next(
        item for item in first_commit["created_revisions"] if item["object_type"] == "exercise"
    )
    exact = post(
        "/api/v1/queries/exercises/search",
        {
            "context": context("exact-1"),
            "mode": "exact",
            "exercise_id": exercise_ref["object_id"],
            "revision_id": None,
        },
    )["data"]
    assert exact["current"] and exact["aggregate"]["canonical_name"] == "Жим лёжа"
    assert exact["aggregate"]["muscle_roles"][0]["muscle_group"]["muscle_group_id"]
    search = post(
        "/api/v1/queries/exercises/search",
        {
            "context": context("search"),
            "mode": "search",
            "text": "штанги",
            "metric_kinds": ["repetitions"],
            "equipment_type": ["barbell"],
            "limit": 20,
            "cursor": None,
        },
    )["data"]
    assert [item["name"] for item in search["items"]] == ["Жим лёжа"]

    approved_measurements = [
        ("repetitions", "required", "count"),
        ("repetitions", "optional", "count"),
        ("duration", "required", "time"),
        ("duration", "optional", "time"),
        ("distance", "required", "length"),
        ("distance", "optional", "length"),
    ]
    for index, (metric_kind, requiredness, unit_family) in enumerate(
        approved_measurements, start=1
    ):
        approved = exercise(f"Проверка метрики {index}", alias=f"метрика {index}")
        approved["measurements"] = [
            {
                "metric_kind": metric_kind,
                "requiredness": requiredness,
                "unit_family": unit_family,
            }
        ]
        response = post(
            "/api/v1/commands/exercise-catalog-drafts",
            {
                "context": context(f"approved-measurement-{index}"),
                "intent": "create",
                "exercise": approved,
                "expected_revision_id": None,
                "reason": None,
            },
            key=f"approved-measurement-{index}",
        )
        assert response["data"]["draft_id"] and response["data"]["revision_id"]

    incompatible = exercise("Несовместимая метрика", alias="несовместимая метрика")
    incompatible["measurements"] = [
        {"metric_kind": "duration", "requiredness": "required", "unit_family": "count"}
    ]
    incompatible_error = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context("incompatible-measurement"),
            "intent": "create",
            "exercise": incompatible,
            "expected_revision_id": None,
            "reason": None,
        },
        key="incompatible-measurement",
        expected=400,
    )
    assert incompatible_error["error"]["code"] == "validation_error"

    missing_reason = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context("correct-missing-reason"),
            "intent": "correct",
            "exercise": exercise("Жим лёжа", exercise_id=exercise_ref["object_id"]),
            "expected_revision_id": exercise_ref["revision_id"],
            "reason": "   ",
        },
        key="correct-missing-reason",
        expected=400,
        validate_request=False,
    )
    assert missing_reason["error"]["code"] == "validation_error"

    unknown_reused_status = exercise("Жим с неизвестным статусом группы")
    unknown_reused_status["muscle_roles"][0]["status_code"] = "not_in_catalog"
    unknown_status_error = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context("unknown-reused-status"),
            "intent": "create",
            "exercise": unknown_reused_status,
            "expected_revision_id": None,
            "reason": None,
        },
        key="unknown-reused-status",
        expected=422,
    )
    assert unknown_status_error["error"]["code"] == "validation_error"

    unknown_new_status = exercise("Жим с новой группой неизвестного статуса")
    unknown_new_status["muscle_roles"][0].update(
        {"name": "Новая синтетическая группа", "status_code": "not_in_catalog"}
    )
    unknown_new_status_error = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context("unknown-new-status"),
            "intent": "create",
            "exercise": unknown_new_status,
            "expected_revision_id": None,
            "reason": None,
        },
        key="unknown-new-status",
        expected=422,
    )
    assert unknown_new_status_error["error"]["code"] == "validation_error"

    blank_group = exercise("Жим с пустой группой")
    blank_group["muscle_roles"][0]["name"] = "   "
    blank_group_error = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context("blank-group"),
            "intent": "create",
            "exercise": blank_group,
            "expected_revision_id": None,
            "reason": None,
        },
        key="blank-group",
        expected=400,
    )
    assert blank_group_error["error"]["code"] == "validation_error"

    duplicate_alias = exercise("Жим с повтором alias")
    duplicate_alias["aliases"] = [
        {"alias": "Тяга Ёлкой", "locale": "ru", "context": "one"},
        {"alias": "тяга елкой", "locale": "en", "context": "two"},
    ]
    duplicate_alias_error = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context("duplicate-alias"),
            "intent": "create",
            "exercise": duplicate_alias,
            "expected_revision_id": None,
            "reason": None,
        },
        key="duplicate-alias",
        expected=400,
    )
    assert duplicate_alias_error["error"]["code"] == "validation_error"

    second_preview, second_commit = draft_and_confirm(
        {
            "intent": "correct",
            "exercise": exercise(
                "Жим лёжа", exercise_id=exercise_ref["object_id"], alias="классический жим"
            ),
            "expected_revision_id": exercise_ref["revision_id"],
            "reason": "  synthetic correction  ",
        },
        "exercise-correct",
    )
    second_ref = next(
        item for item in second_commit["created_revisions"] if item["object_type"] == "exercise"
    )
    assert second_ref["revision_id"] != exercise_ref["revision_id"]
    old = post(
        "/api/v1/queries/exercises/search",
        {
            "context": context("exact-old"),
            "mode": "exact",
            "exercise_id": exercise_ref["object_id"],
            "revision_id": exercise_ref["revision_id"],
        },
    )["data"]
    assert not old["current"] and old["aggregate"]["aliases"][0]["alias"] == "жим штанги"
    assert second_preview["canonical_payload"]["before"]["aliases"][0]["alias"] == ("жим штанги")
    corrected_text = second_preview["canonical_text"]
    assert corrected_text.startswith("Полное описание упражнения\n")
    assert "Текущее состояние" in corrected_text
    assert "жим штанги" in corrected_text
    assert "классический жим" in corrected_text
    assert exercise_ref["object_id"] not in corrected_text
    assert exercise_ref["revision_id"] not in corrected_text

    _, narrow_commit = draft_and_confirm(
        {
            "intent": "create",
            "exercise": exercise("Жим лёжа узким хватом", alias="узкий жим"),
            "expected_revision_id": None,
            "reason": "distinct synthetic exercise",
        },
        "exercise-narrow",
    )
    narrow_ref = next(
        item for item in narrow_commit["created_revisions"] if item["object_type"] == "exercise"
    )
    assert narrow_ref["object_id"] != exercise_ref["object_id"]
    both = post(
        "/api/v1/queries/exercises/search",
        {
            "context": context("search-both"),
            "mode": "search",
            "text": "жим лежа",
            "metric_kinds": None,
            "equipment_type": None,
            "limit": 20,
            "cursor": None,
        },
    )["data"]
    assert {item["name"] for item in both["items"]} == {
        "Жим лёжа",
        "Жим лёжа узким хватом",
    }

    invalid = exercise("Черновой жим")
    invalid["muscle_roles"] = []
    error = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context("invalid"),
            "intent": "create",
            "exercise": invalid,
            "expected_revision_id": None,
            "reason": None,
        },
        key="invalid",
        expected=400,
    )
    assert error["error"]["code"] == "validation_error"

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM trainer.exercise_revisions WHERE exercise_id=:id"),
                {"id": exercise_ref["object_id"]},
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM trainer.muscle_groups "
                    "WHERE normalized_name='грудные мышцы'"
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    "SELECT has_table_privilege"
                    "('trainer_app','trainer.exercise_revisions','DELETE')"
                )
            ).scalar_one()
            is False
        )
        assert (
            connection.execute(
                text(
                    "SELECT has_table_privilege"
                    "('trainer_app','trainer.exercise_revisions','UPDATE')"
                )
            ).scalar_one()
            is False
        )
        duplicate_alias_index = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='trainer' AND indexname='ux_exercise_alias_revision'"
            )
        ).scalar_one()
        assert "normalized_alias" in duplicate_alias_index
        assert "locale" not in duplicate_alias_index and "context" not in duplicate_alias_index
        assert (
            connection.execute(
                text("SELECT count(*) FROM trainer.exercise_measurement_compatibility")
            ).scalar_one()
            == 6
        )
        assert (
            connection.execute(
                text("""
                    SELECT reason FROM trainer.domain_changes
                    WHERE object_id=:exercise AND operation='correct'
                    ORDER BY occurred_at DESC LIMIT 1
                """),
                {"exercise": exercise_ref["object_id"]},
            ).scalar_one()
            == "synthetic correction"
        )

    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO trainer.exercise_measurements(
                        exercise_measurement_id,exercise_revision_id,metric_kind,
                        requiredness,unit_family,created_at
                    ) VALUES (
                        :id,:revision,'duration','required','count',now()
                    )
                """),
                {"id": str(uuid4()), "revision": exercise_ref["revision_id"]},
            )
    except IntegrityError as error:
        assert "fk_exercise_measurement_compatibility" in str(error.orig)
    else:
        raise AssertionError("database accepted an incompatible exercise measurement")

    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO trainer.exercise_aliases(
                        exercise_alias_id,exercise_revision_id,alias,normalized_alias,
                        locale,context,created_at
                    ) VALUES (:id,:revision,'duplicate alias','классический жим',
                              'en','different metadata',now())
                """),
                {"id": str(uuid4()), "revision": second_ref["revision_id"]},
            )
    except IntegrityError as error:
        assert "ux_exercise_alias_revision" in str(error.orig)
    else:
        raise AssertionError("database accepted duplicate normalized alias with other metadata")

    try:
        with engine.begin() as connection:
            confirmation_id = connection.execute(
                text("SELECT confirmation_id FROM trainer.confirmations LIMIT 1")
            ).scalar_one()
            connection.execute(
                text("""
                    INSERT INTO trainer.muscle_groups(
                        muscle_group_id,name,normalized_name,status_code,
                        normalization_version,confirmation_id,created_at
                    ) VALUES (:id,'blank','   ','active','exercise-name-v1',:confirmation,now())
                """),
                {"id": str(uuid4()), "confirmation": confirmation_id},
            )
    except IntegrityError as error:
        assert "ck_muscle_groups_normalized_name_nonblank" in str(error.orig)
    else:
        raise AssertionError("database accepted blank normalized muscle-group name")
    assert [
        group["name"] for group in first_preview["canonical_payload"]["created_muscle_groups"]
    ] == ["Грудные мышцы"]

    dg06_fixtures = (
        (
            "Synthetic pull-up",
            False,
            [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
            [],
        ),
        (
            "Synthetic unilateral curl",
            True,
            [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
            [],
        ),
        (
            "Synthetic timed hold",
            False,
            [{"metric_kind": "duration", "requiredness": "required", "unit_family": "time"}],
            [],
        ),
        (
            "Synthetic fixed-distance cardio",
            False,
            [
                {"metric_kind": "duration", "requiredness": "required", "unit_family": "time"},
                {"metric_kind": "distance", "requiredness": "optional", "unit_family": "length"},
            ],
            [],
        ),
        (
            "Synthetic dumbbell press",
            False,
            [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
            [],
        ),
        (
            "Synthetic replacement source",
            False,
            [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
            [],
        ),
        (
            "Synthetic replacement exercise",
            False,
            [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
            [],
        ),
        (
            "Synthetic circuit lunge",
            False,
            [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
            [],
        ),
        (
            "Synthetic circuit push-up",
            False,
            [{"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}],
            [],
        ),
    )
    for index, (name, unilateral, measurements, equipment_requirements) in enumerate(dg06_fixtures):
        fixture = exercise(name, alias=f"dg06 synthetic {index}")
        fixture["unilateral"] = unilateral
        fixture["measurements"] = measurements
        fixture["equipment_requirements"] = equipment_requirements
        draft_and_confirm(
            {
                "intent": "create",
                "exercise": fixture,
                "expected_revision_id": None,
                "reason": "D-G06 synthetic fixture",
            },
            f"dg06-exercise-{index}",
        )
    print("PASS: D-G03 exercise create/correct/query/immutability/permissions")


if __name__ == "__main__":
    main()
