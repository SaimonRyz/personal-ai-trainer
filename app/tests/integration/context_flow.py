from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, text

from trainer.application.context import ContextApplicationError, ContextService
from trainer.config.settings import Settings
from trainer.infrastructure.db.context_repository import ContextDatabaseExecutor
from trainer.infrastructure.db.engine import create_runtime_engine
from trainer.transport.http.contracts import (
    ConfirmRequest,
    ExerciseCatalogDraftRequest,
    GoalDraftRequest,
    OnboardingDraftRequest,
    PreviewRequest,
    PreviewShownRequest,
)

BASE_URL = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]


def digest(body: dict[str, Any]) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def post(
    path: str, body: dict[str, Any], *, key: str | None = None, expected_error: int | None = None
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
    }
    if key is not None:
        headers["Idempotency-Key"] = key
        model = (
            ConfirmRequest
            if path.endswith("/confirm")
            else PreviewShownRequest
            if path.endswith("/shown")
            else OnboardingDraftRequest
            if path.endswith("/onboarding-drafts")
            else ExerciseCatalogDraftRequest
            if path.endswith("/exercise-catalog-drafts")
            else GoalDraftRequest
            if path.endswith("/goal-drafts")
            else PreviewRequest
        )
        try:
            normalized = model.model_validate({**body, "request_digest": "0" * 64}).model_dump(
                mode="json"
            )
            normalized.pop("request_digest")
            if model is OnboardingDraftRequest:
                for field in ("draft_id", "expected_revision_id"):
                    if normalized.get(field) is None:
                        normalized.pop(field)
            body = {**normalized, "request_digest": digest(normalized)}
        except ValidationError:
            if expected_error is None:
                raise
            body = {**body, "request_digest": digest(body)}
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        response = json.loads(error.read())
        if error.code == expected_error:
            return response
        raise AssertionError(f"{path}: {error.code} {response}") from error


def context(
    message: str, effective_at: str, *, conversation: str = "synthetic-d-g02"
) -> dict[str, str | None]:
    return {
        "conversation_id": conversation,
        "message_id": message,
        "effective_at": effective_at,
        "timezone": "Asia/Novokuznetsk",
        "locale": None,
    }


def goal_payload(*, goal_id: str | None, name: str, target: str = "12") -> dict[str, Any]:
    return {
        "goal_id": goal_id,
        "parent_goal_id": None,
        "name": name,
        "description": None,
        "goal_type": "performance",
        "start_value": "5",
        "target_value": target,
        "unit_code": "repetition",
        "set_on": "2026-08-24",
        "target_date": "2026-12-31",
        "priority": 20,
        "status": "active",
        "comment": None,
    }


def preview_and_show(draft: dict[str, Any], *, at: str, prefix: str) -> dict[str, Any]:
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context(f"{prefix}-preview", at),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        key=f"{prefix}-preview",
    )["data"]
    post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context(f"{prefix}-shown", at),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": f"{prefix}-shown",
            "displayed_text_digest": text_digest(preview["canonical_text"]),
            "cross_dialog": False,
        },
        key=f"{prefix}-shown",
    )
    return preview


def confirm_preview(
    preview: dict[str, Any],
    draft: dict[str, Any],
    *,
    at: str,
    prefix: str,
    expected_error: int | None = None,
) -> dict[str, Any]:
    return post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        {
            "context": context(f"{prefix}-confirm", at),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": f"{prefix}-confirm",
            "explicit_intent": "confirm shown preview",
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        },
        key=f"{prefix}-confirm",
        expected_error=expected_error,
    )


def familiarity_exercise(
    name: str,
    *,
    alias: str,
    exercise_id: str | None = None,
    muscle_group_id: str | None = None,
) -> dict[str, Any]:
    muscle_role: dict[str, Any] = {"role": "primary"}
    if muscle_group_id is None:
        muscle_role.update(
            {
                "kind": "create_by_name",
                "name": f"Синтетическая группа знакомства {uuid4().hex[:8]}",
                "status_code": "active",
            }
        )
    else:
        muscle_role.update({"kind": "existing", "muscle_group_id": muscle_group_id})
    return {
        "exercise_id": exercise_id,
        "canonical_name": name,
        "description": "Synthetic familiarity exercise",
        "category_code": "strength",
        "unilateral": False,
        "status_code": "active",
        "comment": None,
        "aliases": [{"alias": alias, "locale": "ru", "context": None}],
        "muscle_roles": [muscle_role],
        "measurements": [
            {"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"}
        ],
        "equipment_requirements": [],
    }


def save_exercise_revision(
    *,
    name: str,
    alias: str,
    prefix: str,
    intent: str,
    exercise_id: str | None = None,
    expected_revision_id: str | None = None,
    muscle_group_id: str | None = None,
) -> dict[str, str]:
    at = datetime.now(UTC).replace(microsecond=0).isoformat()
    draft = post(
        "/api/v1/commands/exercise-catalog-drafts",
        {
            "context": context(f"{prefix}-draft", at),
            "intent": intent,
            "exercise": familiarity_exercise(
                name,
                alias=alias,
                exercise_id=exercise_id,
                muscle_group_id=muscle_group_id,
            ),
            "expected_revision_id": expected_revision_id,
            "reason": (
                "synthetic familiarity dependency correction" if intent == "correct" else None
            ),
        },
        key=f"{prefix}-draft",
    )["data"]
    preview = preview_and_show(draft, at=at, prefix=prefix)
    committed = confirm_preview(preview, draft, at=at, prefix=prefix)["data"]
    return next(
        item for item in committed["created_revisions"] if item["object_type"] == "exercise"
    )


def main() -> None:
    at = datetime.now(UTC).replace(microsecond=0).isoformat()
    empty = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-empty", at), "purpose": "synthetic integration"},
    )["data"]
    assert empty["profile"] is None and empty["goals"] == []

    familiarity_name = f"Synthetic familiar exercise {uuid4().hex[:8]}"
    first_exercise_ref = save_exercise_revision(
        name=familiarity_name,
        alias=f"familiar-{uuid4().hex[:8]}",
        prefix="familiarity-exercise-create",
        intent="create",
    )
    exact_exercise = post(
        "/api/v1/queries/exercises/search",
        {
            "context": context("familiarity-exercise-exact", at),
            "mode": "exact",
            "exercise_id": first_exercise_ref["object_id"],
            "revision_id": first_exercise_ref["revision_id"],
        },
    )["data"]["aggregate"]
    muscle_group_id = exact_exercise["muscle_roles"][0]["muscle_group"]["muscle_group_id"]

    place_id, equipment_id = str(uuid4()), str(uuid4())
    onboarding_body: dict[str, Any] = {
        "context": context("draft", at),
        "profile": {
            "birth_date": None,
            "sex": None,
            "timezone": "Asia/Novokuznetsk",
            "started_on": None,
            "desired_sessions_per_week": 3,
            "usual_place_id": place_id,
            "comment": "synthetic",
        },
        "goals": [
            {
                "goal_id": None,
                "parent_goal_id": None,
                "name": "Synthetic pull-up goal",
                "description": None,
                "goal_type": "strength",
                "start_value": "5",
                "target_value": "10",
                "unit_code": "repetition",
                "set_on": "2026-08-24",
                "target_date": "2026-12-31",
                "priority": 10,
                "status": "active",
                "comment": None,
            }
        ],
        "places": [
            {
                "place_id": place_id,
                "name": "Synthetic home gym",
                "place_type": "home_gym",
                "status": "active",
                "comment": None,
            }
        ],
        "equipment": [
            {
                "equipment_id": equipment_id,
                "equipment_type": "barbell",
                "name": "Synthetic barbell",
                "maker": None,
                "brand": None,
                "model": None,
                "quantity": None,
                "quantity_unit_code": None,
                "condition": "usable",
                "characteristics": {
                    "mass_per_item_kg": None,
                    "bar_sleeve_diameter_mm": None,
                    "plate_bore_diameter_mm": None,
                },
                "comment": None,
            }
        ],
        "familiar_exercises": [
            {
                "exercise_ref": {
                    "object_type": "exercise",
                    "object_id": first_exercise_ref["object_id"],
                    "revision_id": first_exercise_ref["revision_id"],
                }
            }
        ],
    }
    stale_draft = post(
        "/api/v1/commands/onboarding-drafts",
        onboarding_body,
        key="onboarding-familiarity-stale",
    )["data"]
    admin = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"])
    with admin.begin() as db:
        persisted = db.execute(
            text(
                "SELECT typed_payload,content_digest FROM trainer.draft_revisions "
                "WHERE draft_revision_id=:id"
            ),
            {"id": stale_draft["revision_id"]},
        ).one()
    assert persisted.typed_payload["familiar_exercises"][0]["name"] == familiarity_name
    assert (
        digest(persisted.typed_payload) == persisted.content_digest == stale_draft["content_digest"]
    )
    stale_preview = preview_and_show(stale_draft, at=at, prefix="onboarding-familiarity-stale")
    assert stale_preview["canonical_payload"]["familiar_exercises"][0]["name"] == familiarity_name
    assert familiarity_name in stale_preview["canonical_text"]
    current_exercise_ref = save_exercise_revision(
        name=familiarity_name,
        alias=f"familiar-corrected-{uuid4().hex[:8]}",
        prefix="familiarity-exercise-correct",
        intent="correct",
        exercise_id=first_exercise_ref["object_id"],
        expected_revision_id=first_exercise_ref["revision_id"],
        muscle_group_id=muscle_group_id,
    )
    stale_confirmation = confirm_preview(
        stale_preview,
        stale_draft,
        at=at,
        prefix="onboarding-familiarity-stale-confirm",
        expected_error=409,
    )
    assert stale_confirmation["error"]["code"] == "stale_preview"
    onboarding_body["context"] = context("draft-current-familiarity", at)
    onboarding_body["familiar_exercises"][0]["exercise_ref"]["revision_id"] = current_exercise_ref[
        "revision_id"
    ]
    invalid_window = json.loads(json.dumps(onboarding_body))
    invalid_window["place_equipment_availability"] = []
    rejected_window = post(
        "/api/v1/commands/onboarding-drafts",
        invalid_window,
        key="onboarding-invalid-window",
        expected_error=400,
    )
    assert rejected_window["error"]["code"] == "validation_error"
    still_empty = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-after-invalid-window", at), "purpose": "no partial effect"},
    )["data"]
    assert still_empty["profile"] is None and still_empty["goals"] == []

    unsupported_goal = json.loads(json.dumps(onboarding_body))
    unsupported_goal["goals"][0]["goal_type"] = "unknown_goal_type"
    rejected_goal = post(
        "/api/v1/commands/onboarding-drafts", unsupported_goal,
        key="onboarding-unsupported-goal-type", expected_error=400,
    )
    assert rejected_goal["error"]["code"] == "validation_error"
    assert rejected_goal["error"]["field_errors"][0]["path"] == "goals[0].goal_type"
    draft = post("/api/v1/commands/onboarding-drafts", onboarding_body, key="onboarding-1")["data"]
    assert set(draft) == {
        "draft_id",
        "revision_id",
        "revision_no",
        "content_digest",
        "dependencies",
        "gaps",
        "preview_candidate",
    }
    repeated = post("/api/v1/commands/onboarding-drafts", onboarding_body, key="onboarding-1")[
        "data"
    ]
    assert repeated == draft

    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context("preview", at),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        key="preview-1",
    )["data"]
    assert set(preview) == {
        "preview_id",
        "revision_id",
        "conversation_id",
        "digest",
        "confirmation_kind",
        "complete",
        "canonical_payload",
        "canonical_text",
        "changed_objects",
        "dependencies",
        "gaps",
        "shown_message_id",
        "shown_at",
        "expires_at",
    }
    assert "place_equipment_availability" not in preview["canonical_payload"]
    assert preview["canonical_payload"]["equipment"][0]["quantity"] is None
    assert preview["canonical_payload"]["equipment"][0]["quantity_unit_code"] is None
    assert preview["canonical_payload"]["equipment"][0]["condition"] == "usable"
    preview_text = preview["canonical_text"]
    assert preview["complete"] is True
    assert preview_text.startswith("Первое знакомство: пока ничего не сохранено.\n")
    for detail in (
        familiarity_name,
        "Synthetic pull-up goal",
        "31 декабря 2026 года",
        "Synthetic home gym",
        "1 позиция инвентаря",
    ):
        assert detail in preview_text
    assert "Канонические данные" not in preview_text
    assert "Synthetic barbell" not in preview_text
    inventory_detail = post(
        "/api/v1/commands/previews",
        {
            "context": context("preview-inventory", at),
            "revision_id": draft["revision_id"],
            "detail_level": "inventory",
        },
        key="preview-inventory",
    )["data"]
    assert inventory_detail["revision_id"] == preview["revision_id"]
    assert inventory_detail["canonical_payload"] == preview["canonical_payload"]
    assert inventory_detail["complete"] is False
    assert "Synthetic barbell" in inventory_detail["canonical_text"]
    assert current_exercise_ref["object_id"] not in preview_text
    assert current_exercise_ref["revision_id"] not in preview_text

    revised_body = json.loads(json.dumps(onboarding_body))
    revised_body["context"] = context("revise-onboarding-quantity", at)
    revised_body["draft_id"] = draft["draft_id"]
    revised_body["expected_revision_id"] = draft["revision_id"]
    revised_body["equipment"][0]["quantity"] = 1
    revised_body["equipment"][0]["quantity_unit_code"] = "piece"
    revised = post(
        "/api/v1/commands/onboarding-drafts", revised_body, key="onboarding-quantity-revision"
    )["data"]
    assert revised["draft_id"] == draft["draft_id"]
    assert revised["revision_id"] != draft["revision_id"]
    assert revised["revision_no"] == draft["revision_no"] + 1
    old_show = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context("old-onboarding-shown", at),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": "old-onboarding-shown",
            "displayed_text_digest": text_digest(preview["canonical_text"]),
            "cross_dialog": False,
        },
        key="old-onboarding-shown", expected_error=409,
    )
    assert old_show["error"]["code"] == "stale_preview"
    old_confirm = confirm_preview(
        preview, draft, at=at, prefix="old-onboarding-confirm", expected_error=409
    )
    assert old_confirm["error"]["code"] == "stale_preview"
    draft = revised
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context("revised-onboarding-preview", at),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        key="revised-onboarding-preview",
    )["data"]
    assert "1 шт." in preview["canonical_text"]

    not_shown = confirm_preview(
        preview, draft, at=at, prefix="onboarding-before-shown", expected_error=400
    )
    assert not_shown["error"]["code"] == "incomplete_preview"

    # Synthetic legacy state: a persisted shown marker without the immutable
    # show-event cannot authorize confirm.  A new /shown operation recovers it.
    with admin.begin() as db:
        db.execute(
            text(
                "UPDATE trainer.previews SET shown_message_id='legacy-shown', "
                "returned_as_shown_at=now() WHERE preview_id=:preview"
            ),
            {"preview": preview["preview_id"]},
        )
        db.execute(
            text(
                "UPDATE trainer.draft_revisions SET trust_state='shown' "
                "WHERE draft_revision_id=:revision"
            ),
            {"revision": draft["revision_id"]},
        )
    legacy_confirm = confirm_preview(
        preview, draft, at=at, prefix="onboarding-legacy-shown", expected_error=400
    )
    assert legacy_confirm["error"]["code"] == "ambiguous_confirmation"

    shown_body = {
        "context": context("shown-message", at),
        "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"],
        "shown_message_id": "shown-message",
        "displayed_text_digest": text_digest(preview["canonical_text"]),
        "cross_dialog": False,
    }
    shown = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        shown_body,
        key="shown-1",
    )["data"]
    assert shown["shown_message_id"] == "shown-message"
    assert shown["digest"] == preview["digest"]
    repeated_shown = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        shown_body,
        key="shown-1",
    )["data"]
    assert repeated_shown == shown
    repeated_show_body = {
        **shown_body,
        "context": context("shown-message-repeat", at),
        "shown_message_id": "shown-message-repeat",
    }
    repeated_show = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        repeated_show_body,
        key="shown-repeat-new-message",
    )["data"]
    assert repeated_show["shown_message_id"] == "shown-message-repeat"
    cross_without_reshow = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        {
            "context": context("cross-confirm-before-show", at, conversation="synthetic-cross"),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": "cross-confirm-before-show",
            "explicit_intent": "invalid cross-dialog attempt",
            "confirmation_mode": "cross_dialog",
            "referenced_preview_id": preview["preview_id"],
            "reshown_message_id": "shown-message",
        },
        key="cross-confirm-before-show",
        expected_error=400,
    )
    assert cross_without_reshow["error"]["code"] == "ambiguous_confirmation"
    cross_shown = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context("cross-shown-message", at, conversation="synthetic-cross"),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": "cross-shown-message",
            "displayed_text_digest": text_digest(preview["canonical_text"]),
            "cross_dialog": True,
        },
        key="cross-shown",
    )["data"]
    assert cross_shown["conversation_id"] == "synthetic-cross"
    assert cross_shown["shown_message_id"] == "cross-shown-message"

    confirm_body = {
        "context": context("confirm-message", at, conversation="synthetic-cross"),
        "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"],
        "confirmation_kind": "normal",
        "confirmation_message_id": "confirm-message",
        "explicit_intent": "confirm exactly the shown preview",
        "confirmation_mode": "cross_dialog",
        "referenced_preview_id": preview["preview_id"],
        "reshown_message_id": "cross-shown-message",
    }
    committed = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        confirm_body,
        key="confirm-1",
    )["data"]
    assert committed["status"] == "committed"
    assert set(committed) == {
        "commit_result_id",
        "confirmation_id",
        "operation_id",
        "status",
        "domain_changes",
        "created_revisions",
        "invalidated_calculations",
        "committed_at",
    }
    replay = post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        confirm_body,
        key="confirm-1",
    )["data"]
    assert replay["status"] == "already_committed"
    assert replay["commit_result_id"] == committed["commit_result_id"]

    current = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-current", at), "purpose": "synthetic read-back"},
    )["data"]
    assert current["profile"] is not None
    assert current["goals"][0]["goal"]["target_date"] == "2026-12-31"
    assert current["equipment"][0]["equipment"]["name"] == "Synthetic barbell"
    assert "place_equipment_availability" not in current
    assert current["equipment"][0]["equipment"]["quantity"] == 1
    assert current["equipment"][0]["equipment"]["quantity_unit_code"] == "piece"
    assert current["equipment"][0]["equipment"]["condition"] == "usable"
    assert current["familiar_exercises"] == [
        {
            "exercise_ref": {
                "object_type": "exercise",
                "object_id": current_exercise_ref["object_id"],
                "revision_id": current_exercise_ref["revision_id"],
            },
            "name": familiarity_name,
            "current": True,
        }
    ]
    second_profile = post(
        "/api/v1/commands/onboarding-drafts",
        onboarding_body,
        key="onboarding-second-profile",
        expected_error=409,
    )
    assert second_profile["error"]["code"] == "revision_conflict"

    invalid = goal_payload(goal_id=None, name="Missing date")
    invalid.pop("target_date")
    rejected = post(
        "/api/v1/commands/goal-drafts",
        {
            "context": context("goal-invalid", at),
            "intent": "create",
            "goal": invalid,
            "expected_revision_id": None,
            "reason": None,
        },
        key="goal-invalid",
        expected_error=400,
    )
    assert rejected["error"]["code"] == "validation_error"
    invalid_priority = goal_payload(goal_id=None, name="Invalid priority")
    invalid_priority["priority"] = 0
    priority_rejected = post(
        "/api/v1/commands/goal-drafts",
        {
            "context": context("goal-invalid-priority", at),
            "intent": "create",
            "goal": invalid_priority,
            "expected_revision_id": None,
            "reason": None,
        },
        key="goal-invalid-priority",
        expected_error=400,
    )
    assert priority_rejected["error"]["code"] == "validation_error"
    unknown_parent_goal = goal_payload(goal_id=str(uuid4()), name="Unknown parent")
    unknown_parent_goal["parent_goal_id"] = str(uuid4())
    unknown_parent = post(
        "/api/v1/commands/goal-drafts",
        {
            "context": context("goal-unknown-parent", at),
            "intent": "create",
            "goal": unknown_parent_goal,
            "expected_revision_id": None,
            "reason": None,
        },
        key="goal-unknown-parent",
        expected_error=400,
    )
    assert unknown_parent["error"]["code"] == "validation_error"

    goal_id = str(uuid4())
    create_body = {
        "context": context("goal-create", at),
        "intent": "create",
        "goal": goal_payload(goal_id=goal_id, name="Synthetic second goal"),
        "expected_revision_id": None,
        "reason": "synthetic",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: post(
                    "/api/v1/commands/goal-drafts", dict(create_body), key="goal-create-concurrent"
                )["data"],
                range(2),
            )
        )
    assert responses[0] == responses[1]
    create_draft = responses[0]
    short_preview = post(
        "/api/v1/commands/previews",
        {
            "context": context("goal-create-short-preview", at),
            "revision_id": create_draft["revision_id"],
            "detail_level": "short",
        },
        key="goal-create-short-preview",
    )["data"]
    assert short_preview["complete"] is False
    assert "Synthetic second goal" in short_preview["canonical_text"]
    assert "Канонические данные" not in short_preview["canonical_text"]
    short_shown = post(
        f"/api/v1/commands/previews/{short_preview['preview_id']}/shown",
        {
            "context": context("goal-create-short-shown", at),
            "revision_id": create_draft["revision_id"],
            "preview_digest": short_preview["digest"],
            "shown_message_id": "goal-create-short-shown",
            "displayed_text_digest": text_digest(short_preview["canonical_text"]),
            "cross_dialog": False,
        },
        key="goal-create-short-shown",
        expected_error=400,
    )
    assert short_shown["error"]["code"] == "incomplete_preview"
    short_confirm = confirm_preview(
        short_preview,
        create_draft,
        at=at,
        prefix="goal-create-short",
        expected_error=400,
    )
    assert short_confirm["error"]["code"] == "incomplete_preview"
    created = confirm_preview(
        preview_and_show(create_draft, at=at, prefix="goal-create"),
        create_draft,
        at=at,
        prefix="goal-create",
    )["data"]
    assert created["status"] == "committed"

    after_create = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-after-create", at), "purpose": "revision"},
    )["data"]
    created_item = next(
        item for item in after_create["goals"] if item["goal"]["goal_id"] == goal_id
    )
    expected_revision = created_item["ref"]["revision_id"]

    race_a_body = {
        "context": context("goal-race-a", at),
        "intent": "correct",
        "goal": goal_payload(goal_id=goal_id, name="Race correction A", target="12.5"),
        "expected_revision_id": expected_revision,
        "reason": "synthetic race A",
    }
    race_b_body = {
        "context": context("goal-race-b", at),
        "intent": "correct",
        "goal": goal_payload(goal_id=goal_id, name="Race correction B", target="12.7"),
        "expected_revision_id": expected_revision,
        "reason": "synthetic race B",
    }
    race_a_draft = post("/api/v1/commands/goal-drafts", race_a_body, key="goal-race-a-draft")[
        "data"
    ]
    race_b_draft = post("/api/v1/commands/goal-drafts", race_b_body, key="goal-race-b-draft")[
        "data"
    ]
    race_a_preview = preview_and_show(race_a_draft, at=at, prefix="goal-race-a")
    race_b_preview = preview_and_show(race_b_draft, at=at, prefix="goal-race-b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        race_results = list(
            pool.map(
                lambda args: confirm_preview(
                    args[0], args[1], at=at, prefix=args[2], expected_error=409
                ),
                [
                    (race_a_preview, race_a_draft, "goal-race-a"),
                    (race_b_preview, race_b_draft, "goal-race-b"),
                ],
            )
        )
    assert sum("data" in result for result in race_results) == 1
    race_error = next(result for result in race_results if "error" in result)
    assert race_error["error"]["code"] == "stale_preview"
    after_race = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-after-race", at), "purpose": "race verification"},
    )["data"]
    expected_revision = next(
        item for item in after_race["goals"] if item["goal"]["goal_id"] == goal_id
    )["ref"]["revision_id"]

    child_goal_id = str(uuid4())
    child_goal = goal_payload(goal_id=child_goal_id, name="Synthetic child goal", target="8")
    child_goal["parent_goal_id"] = goal_id
    child_body = {
        "context": context("goal-child", at),
        "intent": "create",
        "goal": child_goal,
        "expected_revision_id": None,
        "reason": "synthetic decomposition",
    }
    child_draft = post("/api/v1/commands/goal-drafts", child_body, key="goal-child-draft")["data"]
    child_preview = preview_and_show(child_draft, at=at, prefix="goal-child")
    assert any(dependency["object_id"] == goal_id for dependency in child_preview["dependencies"])
    confirm_preview(child_preview, child_draft, at=at, prefix="goal-child")
    cycle_goal = goal_payload(goal_id=goal_id, name="Cycle attempt", target="13")
    cycle_goal["parent_goal_id"] = child_goal_id
    cycle = post(
        "/api/v1/commands/goal-drafts",
        {
            "context": context("goal-cycle", at),
            "intent": "correct",
            "goal": cycle_goal,
            "expected_revision_id": expected_revision,
            "reason": "synthetic invalid cycle",
        },
        key="goal-cycle-draft",
        expected_error=400,
    )
    assert cycle["error"]["code"] == "validation_error"
    stale_body = {
        "context": context("goal-stale", at),
        "intent": "correct",
        "goal": goal_payload(goal_id=goal_id, name="Stale proposed name", target="13"),
        "expected_revision_id": expected_revision,
        "reason": "synthetic stale",
    }
    stale_draft = post("/api/v1/commands/goal-drafts", stale_body, key="goal-stale-draft")["data"]
    stale_preview = preview_and_show(stale_draft, at=at, prefix="goal-stale")

    current_body = {
        "context": context("goal-current", at),
        "intent": "correct",
        "goal": goal_payload(goal_id=goal_id, name="Current corrected goal", target="14"),
        "expected_revision_id": expected_revision,
        "reason": "synthetic correction",
    }
    current_draft = post("/api/v1/commands/goal-drafts", current_body, key="goal-current-draft")[
        "data"
    ]
    corrected = confirm_preview(
        preview_and_show(current_draft, at=at, prefix="goal-current"),
        current_draft,
        at=at,
        prefix="goal-current",
    )["data"]
    assert corrected["status"] == "committed"
    stale_result = confirm_preview(
        stale_preview, stale_draft, at=at, prefix="goal-stale", expected_error=409
    )
    assert stale_result["error"]["code"] == "stale_preview"

    after_correct = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-after-correct", at), "purpose": "revision"},
    )["data"]
    corrected_item = next(
        item for item in after_correct["goals"] if item["goal"]["goal_id"] == goal_id
    )
    cancel_body = {
        "context": context("goal-cancel", at),
        "intent": "cancel",
        "goal": goal_payload(goal_id=goal_id, name="Current corrected goal", target="14"),
        "expected_revision_id": corrected_item["ref"]["revision_id"],
        "reason": "synthetic cancellation",
    }
    cancel_draft = post("/api/v1/commands/goal-drafts", cancel_body, key="goal-cancel-draft")[
        "data"
    ]
    cancelled_commit = confirm_preview(
        preview_and_show(cancel_draft, at=at, prefix="goal-cancel"),
        cancel_draft,
        at=at,
        prefix="goal-cancel",
    )["data"]
    after_cancel = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-after-cancel", at), "purpose": "revision"},
    )["data"]
    cancelled = next(item for item in after_cancel["goals"] if item["goal"]["goal_id"] == goal_id)
    assert cancelled["goal"]["status"] == "cancelled"

    conflict_body = dict(create_body)
    conflict_body["goal"] = goal_payload(goal_id=str(uuid4()), name="Different digest")
    conflict = post(
        "/api/v1/commands/goal-drafts",
        conflict_body,
        key="goal-create-concurrent",
        expected_error=409,
    )
    assert conflict["error"]["code"] == "idempotency_conflict"

    fault_goal_id = str(uuid4())
    fault_body = {
        "context": context("goal-fault", at),
        "intent": "create",
        "goal": goal_payload(goal_id=fault_goal_id, name="Rollback goal"),
        "expected_revision_id": None,
        "reason": "fault injection",
    }
    fault_draft = post("/api/v1/commands/goal-drafts", fault_body, key="goal-fault-draft")["data"]
    fault_preview = preview_and_show(fault_draft, at=at, prefix="goal-fault")
    fault_confirm = {
        "context": context("goal-fault-confirm", at),
        "revision_id": fault_draft["revision_id"],
        "preview_digest": fault_preview["digest"],
        "confirmation_kind": "normal",
        "confirmation_message_id": "goal-fault-confirm",
        "explicit_intent": "confirm shown preview",
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }
    normalized = ConfirmRequest.model_validate(
        {**fault_confirm, "request_digest": "0" * 64}
    ).model_dump(mode="json")
    normalized.pop("request_digest")
    normalized["request_digest"] = digest(normalized)
    settings = Settings(
        api_token=SecretStr(TOKEN), database_app_dsn=os.environ["TRAINER_DATABASE_APP_DSN"]
    )
    service = ContextService(ContextDatabaseExecutor(create_runtime_engine(settings)))
    try:
        service.execute(
            "confirm",
            preview_id=fault_preview["preview_id"],
            payload=normalized,
            key="goal-fault-confirm",
            correlation_id=uuid4(),
            fail_before_commit=True,
        )
        raise AssertionError("fault injection unexpectedly committed")
    except ContextApplicationError as error:
        assert error.code == "internal_error"
    after_fault = post(
        "/api/v1/queries/profile-context",
        {"context": context("q-after-fault", at), "purpose": "rollback"},
    )["data"]
    assert all(item["goal"]["name"] != "Rollback goal" for item in after_fault["goals"])
    engine = create_runtime_engine(settings)
    with engine.connect() as connection:
        nullable_columns = dict(
            connection.execute(
                text("""
                SELECT table_name||'.'||column_name,is_nullable
                FROM information_schema.columns
                WHERE table_schema='trainer' AND (
                  (table_name='drafts' AND column_name='owner_profile_id') OR
                  (table_name='equipment_item_revisions'
                   AND column_name IN ('condition','availability')) OR
                  (table_name='idempotent_operations' AND column_name='response_payload'))
            """)
            )
            .tuples()
            .all()
        )
        assert nullable_columns == {
            "drafts.owner_profile_id": "YES",
            "equipment_item_revisions.availability": "YES",
            "equipment_item_revisions.condition": "YES",
            "idempotent_operations.response_payload": "YES",
        }
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM trainer.equipment_condition_catalog "
                    "WHERE code IN ('usable','unusable') AND archived_at IS NULL"
                )
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text("""
                SELECT count(*) FROM pg_constraint
                WHERE connamespace='trainer'::regnamespace
                  AND conname IN ('ck_drafts_owner_scope','ck_operation_commit_result')
            """)
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text("""
                SELECT count(*) FROM information_schema.column_privileges
                WHERE grantee='trainer_app' AND table_schema='trainer'
                  AND table_name='goals' AND column_name='parent_goal_id'
                  AND privilege_type='UPDATE'
            """)
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM trainer.equipment_availability_catalog")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("""
            SELECT count(*) FROM information_schema.role_table_grants
            WHERE grantee='trainer_app' AND privilege_type IN ('DELETE','TRUNCATE')
              AND table_schema='trainer'
        """)
            ).scalar_one()
            == 0
        )
        domain_change_counts = dict(
            connection.execute(
                text("SELECT object_type,count(*) FROM trainer.domain_changes GROUP BY object_type")
            ).all()
        )
        assert domain_change_counts == {
            "equipment_item": 1,
            "exercise": 2,  # familiar exercise create and correction
            "goal": 6,
            "muscle_group": 1,  # unique group created for the familiar exercise
            "profile": 1,
            "training_place": 1,
        }
        familiarity_changes = connection.execute(
            text(
                "SELECT object_type,operation FROM trainer.domain_changes "
                "WHERE object_id IN (:exercise,:muscle_group)"
            ),
            {
                "exercise": current_exercise_ref["object_id"],
                "muscle_group": muscle_group_id,
            },
        ).all()
        assert sorted((row.object_type, row.operation) for row in familiarity_changes) == [
            ("exercise", "correct"),
            ("exercise", "create"),
            ("muscle_group", "create"),
        ]
        assert (
            connection.execute(
                text("SELECT count(*) FROM trainer.place_equipment_availability")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(text("SELECT count(*) FROM trainer.equipment_events")).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("""
            SELECT count(*) FROM trainer.agent_operation_audit
            WHERE result_kind='failed' AND method_id='CMD-00'
        """)
            ).scalar_one()
            >= 2
        )
        assert (
            connection.execute(
                text("""
            SELECT count(*) FROM trainer.goals g
            JOIN trainer.goal_revisions r ON r.goal_id=g.goal_id
            WHERE g.goal_id=:goal
        """),
                {"goal": goal_id},
            ).scalar_one()
            == 4
        )
        rollback_counts = connection.execute(
            text("""
                SELECT
                  (SELECT count(*) FROM trainer.goals WHERE goal_id=:goal),
                  (SELECT count(*) FROM trainer.goal_revisions WHERE goal_id=:goal),
                  (SELECT count(*) FROM trainer.domain_changes WHERE object_id=:goal),
                  (SELECT count(*) FROM trainer.confirmations
                     WHERE user_message_id='goal-fault-confirm'),
                  (SELECT count(*) FROM trainer.idempotent_operations
                     WHERE scope='local-owner:CMD-00' AND idempotency_key='goal-fault-confirm'
                       AND state <> 'failed')
            """),
            {"goal": fault_goal_id},
        ).one()
        assert tuple(rollback_counts) == (0, 0, 0, 0, 0), (
            f"goal confirmation rollback left rows: {tuple(rollback_counts)}"
        )
        assert (
            connection.execute(
                text("""
                SELECT count(*) FROM trainer.idempotent_operations
                WHERE scope='local-owner:CMD-00' AND idempotency_key='goal-fault-confirm'
                  AND state='failed' AND error_code='internal_error'
            """)
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("""
                    SELECT count(*) FROM trainer.agent_operation_audit
                    WHERE correlation_id IS NOT NULL AND causation_id IS NOT NULL
                """)
            ).scalar_one()
            >= 1
        )
        history_snapshots: dict[str, tuple[dict[str, Any] | None, dict[str, Any]]] = {}
        for commit, operation, before_required, cancelled_after in (
            (created, "create", False, False),
            (corrected, "correct", True, False),
            (cancelled_commit, "cancel", True, True),
        ):
            chain = (
                connection.execute(
                    text("""
                        SELECT o.operation_id,a.method_id,a.result_kind,c.confirmation_id,
                               cr.commit_result_id,dc.operation,dc.initiator,
                               dc.before_snapshot,dc.after_snapshot
                        FROM trainer.idempotent_operations o
                        JOIN trainer.agent_operation_audit a ON a.operation_id=o.operation_id
                        JOIN trainer.commit_results cr ON cr.operation_id=o.operation_id
                        JOIN trainer.confirmations c ON c.confirmation_id=cr.confirmation_id
                        JOIN trainer.domain_changes dc ON dc.commit_result_id=cr.commit_result_id
                        WHERE o.operation_id=:operation_id AND dc.object_id=:goal
                          AND a.result_kind='succeeded'
                        ORDER BY a.attempt_no LIMIT 1
                    """),
                    {"operation_id": commit["operation_id"], "goal": goal_id},
                )
                .mappings()
                .one()
            )
            assert str(chain["confirmation_id"]) == commit["confirmation_id"]
            assert str(chain["commit_result_id"]) == commit["commit_result_id"]
            assert chain["method_id"] == "CMD-00"
            assert chain["operation"] == operation and chain["initiator"] == "ai_agent"
            assert (chain["before_snapshot"] is not None) is before_required
            assert chain["after_snapshot"] is not None
            assert bool(chain["after_snapshot"].get("status") == "cancelled") is cancelled_after
            history_snapshots[operation] = (chain["before_snapshot"], chain["after_snapshot"])
        create_before, create_after = history_snapshots["create"]
        assert create_before is None
        assert {
            key: str(create_after.get(key)) if create_after.get(key) is not None else None
            for key in ("goal_id", "name", "target_value", "status", "parent_goal_id", "priority")
        } == {
            "goal_id": goal_id,
            "name": "Synthetic second goal",
            "target_value": "12",
            "status": "active",
            "parent_goal_id": None,
            "priority": "20",
        }
        correct_before, correct_after = history_snapshots["correct"]
        assert correct_before is not None
        assert correct_before["name"] in {"Race correction A", "Race correction B"}
        assert Decimal(str(correct_before["target_value"])) in {Decimal("12.5"), Decimal("12.7")}
        assert str(correct_before["goal_id"]) == goal_id
        assert correct_before["status"] == "active" and correct_before["parent_goal_id"] is None
        assert {
            key: str(correct_after.get(key)) if correct_after.get(key) is not None else None
            for key in ("goal_id", "name", "target_value", "status", "parent_goal_id", "priority")
        } == {
            "goal_id": goal_id,
            "name": "Current corrected goal",
            "target_value": "14",
            "status": "active",
            "parent_goal_id": None,
            "priority": "20",
        }
        cancel_before, cancel_after = history_snapshots["cancel"]
        assert cancel_before is not None
        for key in ("goal_id", "name", "status", "parent_goal_id", "priority"):
            before_value = cancel_before.get(key)
            after_value = correct_after.get(key)
            assert (str(before_value) if before_value is not None else None) == (
                str(after_value) if after_value is not None else None
            )
        assert Decimal(str(cancel_before["target_value"])) == Decimal(
            str(correct_after["target_value"])
        )
        assert cancel_after["name"] == correct_after["name"]
        assert str(cancel_after["target_value"]) == str(correct_after["target_value"])
        assert cancel_after["status"] == "cancelled"
        for method, key, expected_attempts in (
            ("CMD-01", "onboarding-1", 2),
            ("CMD-20", "shown-1", 2),
            ("CMD-00", "confirm-1", 2),
        ):
            assert (
                connection.execute(
                    text("""
                        SELECT count(*) FROM trainer.agent_operation_audit
                        WHERE method_id=:method AND idempotency_key_digest=:digest
                          AND result_kind='succeeded'
                    """),
                    {"method": method, "digest": hashlib.sha256(key.encode()).hexdigest()},
                ).scalar_one()
                == expected_attempts
            )
        assert (
            connection.execute(
                text("""
                    SELECT count(*) FROM trainer.agent_operation_audit
                    WHERE result_kind='succeeded'
                      AND safe_metadata @> '{"replayed": true}'::jsonb
                """)
            ).scalar_one()
            >= 3
        )
    print(
        "PASS: D-G02 onboarding, typed read-back, goal revisions, stale/concurrent "
        "idempotency, rollback, audit and DB permissions"
    )


if __name__ == "__main__":
    main()
