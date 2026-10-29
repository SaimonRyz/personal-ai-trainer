from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from trainer.application.context import ContextService
from trainer.domain.context import digest_payload
from trainer.transport.http.contracts import (
    ConfirmRequest,
    DraftResult,
    ExerciseSearchRequest,
    ExerciseSearchResponse,
    OnboardingDraftRequest,
    PlanDraftRequest,
    PlanningContextRequest,
    PlanningContextResponse,
    PreviewCandidate,
    PreviewRequest,
    PreviewResult,
    PreviewShownRequest,
    ProfileContextRequest,
    ProfileContextResponse,
    ProgramDraftRequest,
    ProgramPublicationDraftRequest,
    WorkoutHistoryResponse,
)
from trainer.transport.http.evidence_contracts import (
    EligibleConclusionsRequest,
    EligibleConclusionsResponse,
)

TOKEN = "spike-local-token"
DRAFT_ID = UUID("30000000-0000-4000-8000-000000000101")
REVISION_ID = UUID("30000000-0000-4000-8000-000000000102")
PREVIEW_ID = UUID("30000000-0000-4000-8000-000000000103")
PROGRAM_DRAFT_ID = UUID("30000000-0000-4000-8000-000000000111")
PROGRAM_REVISION_ID = UUID("30000000-0000-4000-8000-000000000112")
PROGRAM_PREVIEW_ID = UUID("30000000-0000-4000-8000-000000000113")
PROGRAM_PUBLICATION_DRAFT_ID = UUID("30000000-0000-4000-8000-000000000114")
PROGRAM_PUBLICATION_REVISION_ID = UUID("30000000-0000-4000-8000-000000000115")
PROGRAM_PUBLICATION_PREVIEW_ID = UUID("30000000-0000-4000-8000-000000000116")
PROFILE_ID = UUID("30000000-0000-4000-8000-000000000201")
PROFILE_REVISION_ID = UUID("30000000-0000-4000-8000-000000000202")
GOAL_ID = UUID("30000000-0000-4000-8000-000000000203")
GOAL_REVISION_ID = UUID("30000000-0000-4000-8000-000000000204")
EQUIPMENT_ID = UUID("30000000-0000-4000-8000-000000000205")
EQUIPMENT_REVISION_ID = UUID("30000000-0000-4000-8000-000000000206")
EXERCISE_ID = UUID("30000000-0000-4000-8000-000000000207")
EXERCISE_REVISION_ID = UUID("30000000-0000-4000-8000-000000000208")
CONCLUSION_VERSION_ID = UUID("30000000-0000-4000-8000-000000000209")
HISTORY_SNAPSHOT_ID = UUID("30000000-0000-4000-8000-000000000230")
HISTORY_PAGE_TWO_CURSOR = "synthetic-plan-history-page-2"
ONBOARDING_DRAFT_ID = UUID("30000000-0000-4000-8000-000000000221")
ONBOARDING_REVISION_ID = UUID("30000000-0000-4000-8000-000000000222")
ONBOARDING_PREVIEW_ID = UUID("30000000-0000-4000-8000-000000000223")
PLACE_ID = UUID("30000000-0000-4000-8000-000000000224")
app = FastAPI()
DRAFTS: dict[UUID, dict[str, Any]] = {}


@app.exception_handler(HTTPException)
async def api_error_envelope(_: Request, error: HTTPException) -> JSONResponse:
    detail = str(error.detail)
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {"code": detail, "message_safe": detail},
            "meta": {"api_version": "v1"},
        },
    )


def _check_query_request(authorization: str, correlation_id: str, causation_id: str) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    if not correlation_id or not causation_id:
        raise HTTPException(status_code=400, detail="missing_request_metadata")


def _query_envelope(response: Any, correlation_id: str, causation_id: str) -> dict[str, Any]:
    return {
        "data": {"response": response.model_dump(mode="json")},
        "meta": {
            "api_version": "v1",
            "correlation_id": correlation_id,
            "causation_id": causation_id,
        },
    }


def _synthetic_equipment_item() -> dict[str, Any]:
    return {
        "ref": {
            "object_type": "equipment_item",
            "object_id": str(EQUIPMENT_ID),
            "revision_id": str(EQUIPMENT_REVISION_ID),
        },
        "equipment": {
            "equipment_type": "barbell",
            "name": "Synthetic barbell",
            "maker": None,
            "brand": None,
            "model": None,
            "quantity": 1,
            "quantity_unit_code": "piece",
            "condition": "usable",
            "characteristics": {
                "mass_per_item_kg": "7",
                "bar_sleeve_diameter_mm": None,
                "plate_bore_diameter_mm": None,
            },
            "comment": None,
        },
    }


def _synthetic_history_items() -> list[dict[str, Any]]:
    return [
        {
            "session_ref": {
                "object_type": "workout_session",
                "object_id": "30000000-0000-4000-8000-000000000231",
                "revision_id": "30000000-0000-4000-8000-000000000232",
            },
            "current": True,
            "supersedes_revision_ref": None,
            "local_start_date": "2026-10-10",
            "lifecycle": "completed",
            "confirmed_at": "2026-10-10T10:00:00Z",
            "confirmation_ref": {
                "object_type": "confirmation",
                "object_id": "30000000-0000-4000-8000-000000000233",
                "revision_id": None,
            },
            "reason": None,
            "plan_ref": None,
            "plan_content_digest": None,
            "workout": {
                "session_id": "30000000-0000-4000-8000-000000000231",
                "session_revision_id": "30000000-0000-4000-8000-000000000232",
                "local_start_date": "2026-10-10",
                "started_at": "2026-10-10T09:00:00+00:00",
                "ended_at": "2026-10-10T10:00:00+00:00",
                "name": "Synthetic strength session one",
            },
            "provenance": [],
        },
        {
            "session_ref": {
                "object_type": "workout_session",
                "object_id": "30000000-0000-4000-8000-000000000234",
                "revision_id": "30000000-0000-4000-8000-000000000235",
            },
            "current": True,
            "supersedes_revision_ref": None,
            "local_start_date": "2026-10-08",
            "lifecycle": "completed",
            "confirmed_at": "2026-10-08T10:00:00Z",
            "confirmation_ref": {
                "object_type": "confirmation",
                "object_id": "30000000-0000-4000-8000-000000000236",
                "revision_id": None,
            },
            "reason": None,
            "plan_ref": None,
            "plan_content_digest": None,
            "workout": {
                "session_id": "30000000-0000-4000-8000-000000000234",
                "session_revision_id": "30000000-0000-4000-8000-000000000235",
                "local_start_date": "2026-10-08",
                "started_at": "2026-10-08T09:00:00+00:00",
                "ended_at": "2026-10-08T10:00:00+00:00",
                "name": "Synthetic strength session two",
            },
            "provenance": [],
        },
    ]


@app.post("/api/v1/queries/profile-context")
async def get_profile_context(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
) -> dict[str, Any]:
    _check_query_request(authorization, x_correlation_id, x_causation_id)
    ProfileContextRequest.model_validate(await request.json())
    response = ProfileContextResponse.model_validate(
        {
            "profile_ref": {
                "object_type": "profile",
                "object_id": str(PROFILE_ID),
                "revision_id": str(PROFILE_REVISION_ID),
            },
            "profile": {
                "timezone": "Asia/Novokuznetsk",
                "desired_sessions_per_week": 4,
                "birth_date": None,
                "sex": None,
                "started_on": None,
                "usual_place_id": None,
                "comment": None,
            },
            "goals": [
                {
                    "ref": {
                        "object_type": "training_goal",
                        "object_id": str(GOAL_ID),
                        "revision_id": str(GOAL_REVISION_ID),
                    },
                    "goal": {
                        "goal_id": str(GOAL_ID),
                        "parent_goal_id": None,
                        "name": "Synthetic dated strength goal",
                        "description": None,
                        "goal_type": "strength",
                        "start_value": None,
                        "target_value": "100",
                        "unit_code": "kilogram",
                        "set_on": "2026-10-01",
                        "target_date": "2027-04-01",
                        "priority": 1,
                        "status": "active",
                        "comment": None,
                    },
                }
            ],
            "places": [],
            "equipment": [_synthetic_equipment_item()],
            "familiar_exercises": [],
            "effective_at": "2026-10-11T10:00:00Z",
            "gaps": [],
        }
    )
    return _query_envelope(response, x_correlation_id, x_causation_id)


@app.post("/api/v1/queries/planning-context")
async def get_planning_context(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
) -> dict[str, Any]:
    _check_query_request(authorization, x_correlation_id, x_causation_id)
    contract = PlanningContextRequest.model_validate(await request.json())
    if contract.target_date.isoformat() != "2026-10-12":
        raise HTTPException(status_code=409, detail="unexpected_target_date")
    response = PlanningContextResponse.model_validate(
        {
            "facts": [
                {
                    "ref": {
                        "object_type": "training_goal",
                        "object_id": str(GOAL_ID),
                        "revision_id": str(GOAL_REVISION_ID),
                    },
                    "priority": 1,
                    "source_kind": "user",
                    "snapshot_digest": "b" * 64,
                }
            ],
            "conflicts": [],
            "gaps": [],
            "eligible_conclusion_version_ids": [str(CONCLUSION_VERSION_ID)],
            "effective_at": "2026-10-11T10:00:00Z",
            "target_date": "2026-10-12",
            "timezone": "Asia/Novokuznetsk",
            "effective_equipment_types": ["barbell"],
            "equipment_inventory_snapshot": [_synthetic_equipment_item()],
            "equipment_inventory_digest": "a" * 64,
        }
    )
    return _query_envelope(response, x_correlation_id, x_causation_id)


@app.post("/api/v1/queries/workouts/history")
async def get_workout_history(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
) -> dict[str, Any]:
    _check_query_request(authorization, x_correlation_id, x_causation_id)
    body = await request.json()
    if body.get("mode") != "all_confirmed" or body.get("date_from") or body.get("date_to"):
        raise HTTPException(status_code=409, detail="unexpected_history_query")
    if body.get("limit") != 1:
        raise HTTPException(status_code=409, detail="synthetic_history_requires_one_item_pages")
    cursor = body.get("cursor")
    if cursor is None:
        items = _synthetic_history_items()[:1]
        next_cursor = HISTORY_PAGE_TWO_CURSOR
    elif cursor == HISTORY_PAGE_TWO_CURSOR:
        items = _synthetic_history_items()[1:]
        next_cursor = None
    else:
        raise HTTPException(status_code=410, detail="history_snapshot_expired")
    response = WorkoutHistoryResponse.model_validate(
        {
            "items": items,
            "next_cursor": next_cursor,
            "snapshot_id": str(HISTORY_SNAPSHOT_ID),
            "snapshot_confirmed_at": "2026-10-11T10:00:00Z",
            "total_count": 2,
        }
    )
    return _query_envelope(response, x_correlation_id, x_causation_id)


@app.post("/api/v1/queries/exercises/search")
async def search_exercises(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
) -> dict[str, Any]:
    _check_query_request(authorization, x_correlation_id, x_causation_id)
    contract = ExerciseSearchRequest.model_validate(await request.json())
    if contract.mode != "search" or contract.text != "synthetic bench press":
        raise HTTPException(status_code=409, detail="unexpected_exercise_query")
    response = ExerciseSearchResponse.model_validate(
        {
            "items": [
                {
                    "exercise_ref": {
                        "object_type": "exercise",
                        "object_id": str(EXERCISE_ID),
                        "revision_id": str(EXERCISE_REVISION_ID),
                    },
                    "name": "Synthetic bench press",
                    "aliases": [],
                    "category": "compound",
                    "metric_kinds": ["repetitions"],
                    "equipment_types": ["barbell"],
                    "rank": "1.0",
                }
            ],
            "next_cursor": None,
        }
    )
    return _query_envelope(response, x_correlation_id, x_causation_id)


@app.post("/api/v1/queries/conclusions/eligible")
async def get_eligible_conclusions(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
) -> dict[str, Any]:
    _check_query_request(authorization, x_correlation_id, x_causation_id)
    contract = EligibleConclusionsRequest.model_validate(await request.json())
    if contract.decision_date.isoformat() != "2026-10-12":
        raise HTTPException(status_code=409, detail="unexpected_decision_date")
    response = EligibleConclusionsResponse.model_validate(
        {
            "items": [
                {
                    "conclusion_version_id": str(CONCLUSION_VERSION_ID),
                    "status": "verified",
                    "confidence": "moderate",
                    "review_due_on": "2027-01-01",
                    "source_ids": ["30000000-0000-4000-8000-000000000210"],
                    "applicability_assessment": {"synthetic": True, "eligible": True},
                }
            ],
            "conflicts": [],
            "gaps": [],
        }
    )
    return _query_envelope(response, x_correlation_id, x_causation_id)


@app.post("/api/v1/commands/plan-drafts")
async def create_plan_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = PlanDraftRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if not x_correlation_id or not x_causation_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="missing_request_metadata")
    DRAFTS[REVISION_ID] = {
        "draft_id": DRAFT_ID,
        "preview_id": PREVIEW_ID,
        "draft_kind": "plan",
        "plan": contract.plan.model_dump(mode="json"),
        "recommendations": [item.model_dump(mode="json") for item in contract.recommendations],
    }

    result = DraftResult(
        draft_id=DRAFT_ID,
        revision_id=REVISION_ID,
        revision_no=1,
        content_digest="b" * 64,
        dependencies=[],
        gaps=[],
        preview_candidate=PreviewCandidate(
            summary="Synthetic dated training plan",
            confirmation_kind="normal",
            complete=True,
        ),
    )
    return {
        "data": result.model_dump(mode="json"),
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }


@app.post("/api/v1/commands/onboarding-drafts")
async def create_onboarding_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = OnboardingDraftRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if not x_correlation_id or not x_causation_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="missing_request_metadata")
    DRAFTS[ONBOARDING_REVISION_ID] = {
        "draft_id": ONBOARDING_DRAFT_ID,
        "preview_id": ONBOARDING_PREVIEW_ID,
        "draft_kind": "onboarding",
        "profile": contract.profile.model_dump(mode="json"),
        "goals": [item.model_dump(mode="json") for item in contract.goals],
        "places": [item.model_dump(mode="json") for item in contract.places],
        "equipment": [item.model_dump(mode="json") for item in contract.equipment],
    }
    result = DraftResult(
        draft_id=ONBOARDING_DRAFT_ID,
        revision_id=ONBOARDING_REVISION_ID,
        revision_no=1,
        content_digest="f" * 64,
        dependencies=[],
        gaps=[],
        preview_candidate=PreviewCandidate(
            summary="Synthetic onboarding profile, dated goal, place, and inventory",
            confirmation_kind="normal",
            complete=True,
        ),
    )
    return {
        "data": result.model_dump(mode="json"),
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }


@app.post("/api/v1/commands/program-drafts")
async def create_program_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = ProgramDraftRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if not x_correlation_id or not x_causation_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="missing_request_metadata")
    DRAFTS[PROGRAM_REVISION_ID] = {
        "draft_id": PROGRAM_DRAFT_ID,
        "preview_id": PROGRAM_PREVIEW_ID,
        "draft_kind": "program",
        "state": "open",
        "goal_revision_id": str(contract.goal_revision_id),
        "intent": contract.intent,
        "program": contract.program.model_dump(mode="json") if contract.program else None,
        "program_id": str(contract.program_id) if contract.program_id else None,
        "expected_current_version_id": (
            str(contract.expected_current_version_id)
            if contract.expected_current_version_id
            else None
        ),
        "version": contract.version.model_dump(mode="json"),
    }

    result = DraftResult(
        draft_id=PROGRAM_DRAFT_ID,
        revision_id=PROGRAM_REVISION_ID,
        revision_no=1,
        content_digest="d" * 64,
        dependencies=[],
        gaps=[],
        preview_candidate=PreviewCandidate(
            summary="Synthetic new program",
            confirmation_kind="normal",
            complete=True,
        ),
    )
    return {
        "data": result.model_dump(mode="json"),
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }


@app.post("/api/v1/commands/program-publication-drafts")
async def create_program_publication_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = ProgramPublicationDraftRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if not x_correlation_id or not x_causation_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="missing_request_metadata")
    source = DRAFTS.get(contract.source_revision_id)
    if (
        source is None
        or source["draft_kind"] != "program"
        or source["draft_id"] != contract.source_draft_id
        or source.get("state") != "open"
    ):
        raise HTTPException(status_code=409, detail="source_draft_not_open")
    DRAFTS[PROGRAM_PUBLICATION_REVISION_ID] = {
        "draft_id": PROGRAM_PUBLICATION_DRAFT_ID,
        "preview_id": PROGRAM_PUBLICATION_PREVIEW_ID,
        "draft_kind": "program_publication",
        "source_draft_id": str(contract.source_draft_id),
        "source_revision_id": str(contract.source_revision_id),
        "expected_current_version_id": (
            str(contract.expected_current_version_id)
            if contract.expected_current_version_id
            else None
        ),
        "publication_reason": contract.publication_reason,
        "confirmed": False,
    }
    result = DraftResult(
        draft_id=PROGRAM_PUBLICATION_DRAFT_ID,
        revision_id=PROGRAM_PUBLICATION_REVISION_ID,
        revision_no=1,
        content_digest="9" * 64,
        dependencies=[],
        gaps=[],
        preview_candidate=PreviewCandidate(
            summary="Synthetic separate program publication",
            confirmation_kind="publication",
            complete=True,
        ),
    )
    return {
        "data": result.model_dump(mode="json"),
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }


@app.post("/api/v1/commands/previews")
async def render_preview(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = PreviewRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    draft = DRAFTS.get(contract.revision_id)
    if draft is None or contract.detail_level != "full":
        raise HTTPException(status_code=409, detail="unexpected_preview_target")
    if not idempotency_key or not x_correlation_id or not x_causation_id:
        raise HTTPException(status_code=400, detail="missing_request_metadata")

    canonical_payload = {
        "fixture": "synthetic-only",
        "draft_revision_id": str(contract.revision_id),
        "detail_level": contract.detail_level,
        **{
            key: str(value) if isinstance(value, UUID) else value
            for key, value in draft.items()
            if key not in {"draft_id", "preview_id", "confirmed"}
        },
    }
    preview_id = draft["preview_id"]
    if not isinstance(preview_id, UUID):
        raise HTTPException(status_code=500, detail="invalid_fixture_preview_id")
    canonical_text = (
        "СИНТЕТИЧЕСКОЕ ПЕРВОЕ ЗНАКОМСТВО — только тест transport.\n"
        "Профиль, цель с датой, обычное место и инвентарь собраны одним черновиком.\n"
        "Сохранение и подтверждение не выполнялись."
        if draft["draft_kind"] == "onboarding"
        else (
            "СИНТЕТИЧЕСКИЙ ПЛАН — только тест transport.\n"
            "Дата: 2026-10-12.\n"
            "Жим лёжа: 3 подхода по 5 повторений.\n"
            "На момент предпросмотра подтверждение ещё не выполнялось."
            if draft["draft_kind"] == "plan"
            else (
                "СИНТЕТИЧЕСКАЯ ПУБЛИКАЦИЯ ПРОГРАММЫ — отдельный draft для теста.\n"
                "Источник: точная открытая revision технического черновика программы.\n"
                "Публикация ещё не выполнена."
                if draft["draft_kind"] == "program_publication"
                else (
                    "СИНТЕТИЧЕСКАЯ ПРОГРАММА — только тест transport.\n"
                    "Период: 2026-10-12 — 2026-12-31.\n"
                    "Сохранена точная редакция упражнения и цели.\n"
                    "Публикация и подтверждение не выполнялись."
                )
            )
        )
    )
    preview = PreviewResult(
        preview_id=preview_id,
        revision_id=contract.revision_id,
        conversation_id=contract.context.conversation_id,
        digest=digest_payload(canonical_payload),
        confirmation_kind=(
            "publication" if draft["draft_kind"] == "program_publication" else "normal"
        ),
        complete=True,
        canonical_payload=canonical_payload,
        canonical_text=canonical_text,
        changed_objects=[],
        dependencies=[],
        gaps=[],
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    draft["preview_digest"] = preview.digest
    draft["canonical_text"] = canonical_text
    draft["shown_message_id"] = None
    return {
        "data": preview.model_dump(mode="json"),
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }


@app.post("/api/v1/commands/previews/{preview_id}/shown")
async def acknowledge_preview_shown(
    preview_id: UUID,
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    _check_query_request(authorization, x_correlation_id, x_causation_id)
    body = await request.json()
    contract = PreviewShownRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    draft = DRAFTS.get(contract.revision_id)
    expected_digest = (
        hashlib.sha256(str(draft["canonical_text"]).encode()).hexdigest()
        if draft is not None
        else None
    )
    if (
        draft is None
        or preview_id != draft["preview_id"]
        or contract.preview_digest != draft["preview_digest"]
        or contract.displayed_text_digest != expected_digest
        or not idempotency_key
    ):
        raise HTTPException(status_code=409, detail="stale_or_unshown_preview")
    draft["shown_message_id"] = contract.shown_message_id
    return {
        "data": {
            "preview_id": str(preview_id),
            "shown_message_id": contract.shown_message_id,
            "shown_at": datetime.now(UTC).isoformat(),
        },
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }


@app.post("/api/v1/commands/previews/{preview_id}/confirm")
async def confirm_preview(
    preview_id: UUID,
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    _check_query_request(authorization, x_correlation_id, x_causation_id)
    body = await request.json()
    contract = ConfirmRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    draft = DRAFTS.get(contract.revision_id)
    if draft is not None and draft["draft_kind"] == "program":
        raise HTTPException(status_code=409, detail="confirmation_not_supported")
    if (
        draft is None
        or preview_id != draft["preview_id"]
        or contract.preview_digest != draft["preview_digest"]
        or draft["shown_message_id"] is None
        or contract.confirmation_mode != "same_dialog"
        or contract.confirmation_kind
        != ("publication" if draft["draft_kind"] == "program_publication" else "normal")
        or not contract.explicit_intent.strip()
        or not idempotency_key
    ):
        raise HTTPException(status_code=409, detail="stale_or_unshown_preview")
    draft["confirmed"] = True
    return {
        "data": {
            "commit_result_id": "30000000-0000-4000-8000-000000000240",
            "confirmation_id": "30000000-0000-4000-8000-000000000241",
            "operation_id": "30000000-0000-4000-8000-000000000242",
            "status": "committed",
            "domain_changes": [],
            "created_revisions": [],
            "invalidated_calculations": [],
            "committed_at": datetime.now(UTC).isoformat(),
        },
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }
