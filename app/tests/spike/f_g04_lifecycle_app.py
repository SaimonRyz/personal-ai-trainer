from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request

from trainer.application.context import ContextService
from trainer.transport.http.contracts import (
    ConfirmRequest,
    PlanLifecycleRequest,
    PreviewRequest,
    PreviewShownRequest,
)

TOKEN = "spike-local-token"
DRAFT_ID = UUID("20000000-0000-4000-8000-000000000201")
REVISION_ID = UUID("20000000-0000-4000-8000-000000000202")
PREVIEW_ID = UUID("20000000-0000-4000-8000-000000000203")
PREVIEW_DIGEST = "a" * 64
CANONICAL_TEXT = "SYNTHETIC PLAN LIFECYCLE PREVIEW\nNo domain effect."
app = FastAPI()


@app.post("/api/v1/commands/plan-lifecycle-drafts")
async def create_plan_lifecycle_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = PlanLifecycleRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if not x_correlation_id or not x_causation_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="missing_request_metadata")

    return {
        "data": {
            "synthetic": True,
            "effect": "none",
            "draft_id": str(DRAFT_ID),
            "revision_id": str(REVISION_ID),
            "revision_no": 1,
            "content_digest": "b" * 64,
            "dependencies": [],
            "gaps": [],
            "preview_candidate": {
                "summary": "Synthetic plan lifecycle draft",
                "confirmation_kind": "normal",
                "complete": True,
            },
            "intent": contract.intent,
            "source_ref": contract.source_ref.model_dump(mode="json"),
            "expected_current_revision_id": str(contract.expected_current_revision_id),
            "reason": contract.reason,
            "replacement_plan": body.get("replacement_plan"),
            "replacement_recommendations": body.get("replacement_recommendations"),
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
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = PreviewRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if contract.revision_id != REVISION_ID or not idempotency_key:
        raise HTTPException(status_code=409, detail="unexpected_preview_target")
    return {
        "data": {
            "preview_id": str(PREVIEW_ID),
            "revision_id": str(REVISION_ID),
            "conversation_id": contract.context.conversation_id,
            "digest": PREVIEW_DIGEST,
            "confirmation_kind": "normal",
            "complete": True,
            "canonical_payload": {"synthetic": True},
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
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = PreviewShownRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if preview_id != PREVIEW_ID or contract.revision_id != REVISION_ID or not idempotency_key:
        raise HTTPException(status_code=409, detail="unexpected_shown_target")
    return {
        "data": {
            "preview_id": str(PREVIEW_ID),
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
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = ConfirmRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if (
        preview_id != PREVIEW_ID
        or contract.revision_id != REVISION_ID
        or contract.preview_digest != PREVIEW_DIGEST
        or contract.confirmation_kind != "normal"
        or not idempotency_key
    ):
        raise HTTPException(status_code=409, detail="unexpected_confirmation_target")
    return {
        "data": {
            "commit_result_id": "20000000-0000-4000-8000-000000000204",
            "confirmation_id": "20000000-0000-4000-8000-000000000205",
            "operation_id": "20000000-0000-4000-8000-000000000206",
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
