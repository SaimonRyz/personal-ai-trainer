from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from trainer.application.context import ContextService
from trainer.transport.http.contracts import (
    ConfirmRequest,
    PreviewRequest,
    PreviewShownRequest,
    WorkoutDraftRevisionRequest,
    WorkoutReportDraftRequest,
)

TOKEN = "spike-local-token"
DRAFT_ID = UUID("20000000-0000-4000-8000-000000000301")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000302")
INITIAL_REVISION_ID = UUID("20000000-0000-4000-8000-000000000303")
REVISED_REVISION_ID = UUID("20000000-0000-4000-8000-000000000304")
OLD_PREVIEW_ID = UUID("20000000-0000-4000-8000-000000000305")
PREVIEW_ID = UUID("20000000-0000-4000-8000-000000000309")
OLD_PREVIEW_DIGEST = "b" * 64
PREVIEW_DIGEST = "c" * 64
CANONICAL_TEXT = "SYNTHETIC WORKOUT REPORT PREVIEW\nNo domain effect."
latest_revision_id = INITIAL_REVISION_ID
latest_report: dict[str, Any] | None = None
app = FastAPI()


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


def _check_request(
    authorization: str,
    x_correlation_id: str,
    x_causation_id: str,
    idempotency_key: str,
) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    if not x_correlation_id or not x_causation_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="missing_request_metadata")


@app.post("/api/v1/commands/workout-report-drafts")
async def create_workout_report_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    global latest_revision_id, latest_report
    _check_request(authorization, x_correlation_id, x_causation_id, idempotency_key)
    body = await request.json()
    contract = WorkoutReportDraftRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    latest_revision_id = INITIAL_REVISION_ID
    latest_report = contract.report.model_dump(mode="json")
    return {
        "data": {
            "synthetic": True,
            "effect": "none",
            "draft_id": str(DRAFT_ID),
            "session_id": str(SESSION_ID),
            "revision_id": str(INITIAL_REVISION_ID),
            "revision_no": 1,
            "content_digest": "d" * 64,
            "submitted_report": latest_report,
            "idempotency_key": idempotency_key,
        },
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }


@app.post("/api/v1/commands/drafts/{draft_id}/revisions")
async def revise_workout_report_draft(
    draft_id: UUID,
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    global latest_revision_id, latest_report
    _check_request(authorization, x_correlation_id, x_causation_id, idempotency_key)
    if draft_id != DRAFT_ID:
        raise HTTPException(status_code=404, detail="unknown_synthetic_draft")
    body = await request.json()
    contract = WorkoutDraftRevisionRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if contract.base_revision_id != latest_revision_id:
        raise HTTPException(status_code=409, detail="stale_base_revision")
    change = contract.changes[0]
    if change.path != "/report":
        raise HTTPException(status_code=422, detail="only_full_report_replacement_is_allowed")
    latest_revision_id = REVISED_REVISION_ID
    latest_report = change.value.model_dump(mode="json")
    return {
        "data": {
            "synthetic": True,
            "effect": "none",
            "draft_id": str(DRAFT_ID),
            "session_id": str(SESSION_ID),
            "revision_id": str(REVISED_REVISION_ID),
            "revision_no": 2,
            "content_digest": "e" * 64,
            "replaced_report": latest_report,
            "idempotency_key": idempotency_key,
        },
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
    _check_request(authorization, x_correlation_id, x_causation_id, idempotency_key)
    body = await request.json()
    contract = PreviewRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if contract.revision_id != latest_revision_id or latest_report is None:
        raise HTTPException(status_code=409, detail="stale_revision")
    preview_id = OLD_PREVIEW_ID if contract.revision_id == INITIAL_REVISION_ID else PREVIEW_ID
    preview_digest = OLD_PREVIEW_DIGEST if preview_id == OLD_PREVIEW_ID else PREVIEW_DIGEST
    return {
        "data": {
            "preview_id": str(preview_id),
            "revision_id": str(latest_revision_id),
            "conversation_id": contract.context.conversation_id,
            "digest": preview_digest,
            "confirmation_kind": "normal",
            "complete": True,
            "canonical_payload": {"synthetic": True, "report": latest_report},
            "canonical_text": CANONICAL_TEXT,
            "changed_objects": [],
            "dependencies": [],
            "gaps": [],
            "shown_message_id": None,
            "shown_at": None,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
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
    _check_request(authorization, x_correlation_id, x_causation_id, idempotency_key)
    body = await request.json()
    contract = PreviewShownRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    expected_digest = OLD_PREVIEW_DIGEST if preview_id == OLD_PREVIEW_ID else PREVIEW_DIGEST
    if (
        preview_id not in {OLD_PREVIEW_ID, PREVIEW_ID}
        or preview_id == OLD_PREVIEW_ID
        and latest_revision_id != INITIAL_REVISION_ID
        or contract.revision_id != latest_revision_id
        or contract.preview_digest != expected_digest
    ):
        raise HTTPException(status_code=409, detail="stale_preview")
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
    _check_request(authorization, x_correlation_id, x_causation_id, idempotency_key)
    body = await request.json()
    contract = ConfirmRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if (
        preview_id != PREVIEW_ID
        or contract.revision_id != latest_revision_id
        or contract.preview_digest != PREVIEW_DIGEST
        or contract.confirmation_kind != "normal"
    ):
        raise HTTPException(status_code=409, detail="stale_preview")
    return {
        "data": {
            "commit_result_id": "20000000-0000-4000-8000-000000000306",
            "confirmation_id": "20000000-0000-4000-8000-000000000307",
            "operation_id": "20000000-0000-4000-8000-000000000308",
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
