from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from trainer.spike.contracts import (
    ConfirmRequest,
    OperationStatusRequest,
    RenderPreviewRequest,
    ShownRequest,
)
from trainer.transport.http.app import ApiBoundaryMiddleware, error_response

FIXTURE_REVISION_ID = UUID("10000000-0000-4000-8000-000000000001")
FIXTURE_PAYLOAD = {"spike_fixture": "technical-foundation", "value": "no-domain-effect"}
FIXTURE_TEXT = "SPIKE PREVIEW\n- technical fixture\n- no domain effect"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def request_without_digest(model: Any) -> dict[str, Any]:
    data = cast(dict[str, Any], model.model_dump(mode="json", exclude_unset=True))
    data.pop("request_digest", None)
    return data


def require_request_digest(model: Any) -> None:
    if model.request_digest != digest(request_without_digest(model)):
        raise HTTPException(status_code=400, detail="digest_mismatch")


def create_spike_app(token: str = "spike-local-token") -> FastAPI:
    app = FastAPI(title="Stage C test-only spike harness", openapi_url=None, docs_url=None)
    previews: dict[UUID, dict[str, Any]] = {}
    operations: dict[UUID, dict[str, Any]] = {}
    idempotency: dict[tuple[str, str], tuple[str, UUID]] = {}
    shown_idempotency: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    correlations: dict[tuple[str, UUID], UUID] = {}
    app.state.draining = False
    settings = type(
        "SpikeSettings",
        (),
        {
            "api_token": type("Token", (), {"get_secret_value": lambda _: token})(),
            "owner_verifier_token": None,
            "max_request_bytes": 1_048_576,
        },
    )()
    app.add_middleware(ApiBoundaryMiddleware, settings=settings)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", uuid4())
        code = str(error.detail)
        if error.status_code == 404 and code == "Not Found":
            code = "not_found"
        elif error.status_code == 405:
            code = "validation_error"
        return error_response(
            status_code=error.status_code,
            code=code,
            message=code.replace("_", " "),
            correlation_id=correlation_id,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", uuid4())
        return error_response(
            status_code=400,
            code="validation_error",
            message="request validation failed",
            correlation_id=correlation_id,
        )

    def authorize(authorization: str) -> None:
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthenticated")

    def command_headers(
        authorization: str,
        correlation_id: str,
        causation_id: str,
        idempotency_key: str,
    ) -> None:
        authorize(authorization)
        UUID(correlation_id)
        UUID(causation_id)
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="validation_error")

    @app.post("/api/v1/commands/previews")
    def render_preview(
        request: RenderPreviewRequest,
        authorization: str = Header(),
        x_correlation_id: str = Header(),
        x_causation_id: str = Header(),
        idempotency_key: str = Header(),
    ) -> dict[str, Any]:
        command_headers(authorization, x_correlation_id, x_causation_id, idempotency_key)
        require_request_digest(request)
        if request.revision_id != FIXTURE_REVISION_ID:
            raise HTTPException(status_code=404, detail="not_found")
        key = ("CMD-20", idempotency_key)
        request_hash = request.request_digest
        if key in idempotency:
            old_hash, preview_id = idempotency[key]
            if old_hash != request_hash:
                raise HTTPException(status_code=409, detail="idempotency_conflict")
            return {"data": previews[preview_id], "meta": meta(x_correlation_id, None)}
        preview_id = uuid4()
        preview_core = {
            "revision_id": str(FIXTURE_REVISION_ID),
            "canonical_payload": FIXTURE_PAYLOAD,
            "canonical_text": FIXTURE_TEXT,
            "dependencies": [],
            "confirmation_kind": "normal",
        }
        preview = {
            "preview_id": str(preview_id),
            **preview_core,
            "conversation_id": request.context.conversation_id,
            "digest": digest(preview_core),
            "complete": True,
            "changed_objects": [],
            "gaps": [],
            "shown_message_id": None,
            "shown_at": None,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        previews[preview_id] = preview
        idempotency[key] = (request_hash, preview_id)
        return {"data": preview, "meta": meta(x_correlation_id, None)}

    @app.post("/api/v1/commands/previews/{preview_id}/shown")
    def shown(
        preview_id: UUID,
        request: ShownRequest,
        authorization: str = Header(),
        x_correlation_id: str = Header(),
        x_causation_id: str = Header(),
        idempotency_key: str = Header(),
    ) -> dict[str, Any]:
        command_headers(authorization, x_correlation_id, x_causation_id, idempotency_key)
        require_request_digest(request)
        preview = previews.get(preview_id)
        if preview is None:
            raise HTTPException(status_code=404, detail="not_found")
        if (
            request.revision_id != FIXTURE_REVISION_ID
            or request.preview_digest != preview["digest"]
        ):
            raise HTTPException(status_code=409, detail="stale_preview")
        if request.displayed_text_digest != text_digest(preview["canonical_text"]):
            raise HTTPException(status_code=400, detail="digest_mismatch")
        if request.context.conversation_id != preview["conversation_id"]:
            raise HTTPException(status_code=409, detail="revision_conflict")
        key = ("CMD-20/shown", idempotency_key)
        if key in shown_idempotency:
            old_hash, saved_preview = shown_idempotency[key]
            if old_hash != request.request_digest:
                raise HTTPException(status_code=409, detail="idempotency_conflict")
            return {"data": saved_preview, "meta": meta(x_correlation_id, None)}
        preview["shown_message_id"] = request.shown_message_id
        preview["shown_at"] = datetime.now(UTC).isoformat()
        saved_preview = dict(preview)
        shown_idempotency[key] = (request.request_digest, saved_preview)
        return {"data": saved_preview, "meta": meta(x_correlation_id, None)}

    @app.post("/api/v1/commands/previews/{preview_id}/confirm")
    def confirm(
        preview_id: UUID,
        request: ConfirmRequest,
        response: Response,
        authorization: str = Header(),
        x_correlation_id: str = Header(),
        x_causation_id: str = Header(),
        idempotency_key: str = Header(),
    ) -> dict[str, Any]:
        command_headers(authorization, x_correlation_id, x_causation_id, idempotency_key)
        require_request_digest(request)
        preview = previews.get(preview_id)
        if preview is None:
            raise HTTPException(status_code=404, detail="not_found")
        key = ("CMD-00", idempotency_key)
        if key in idempotency:
            old_hash, operation_id = idempotency[key]
            if old_hash != request.request_digest:
                raise HTTPException(status_code=409, detail="idempotency_conflict")
            result = dict(operations[operation_id]["commit_result"])
            result["status"] = "already_committed"
            return {"data": result, "meta": meta(x_correlation_id, operation_id)}
        if preview["shown_message_id"] is None:
            raise HTTPException(status_code=400, detail="incomplete_preview")
        if request.context.conversation_id != preview["conversation_id"]:
            raise HTTPException(status_code=400, detail="ambiguous_confirmation")
        if (
            request.revision_id != FIXTURE_REVISION_ID
            or request.preview_digest != preview["digest"]
        ):
            raise HTTPException(status_code=409, detail="stale_preview")
        if request.confirmation_message_id == preview["shown_message_id"]:
            raise HTTPException(status_code=400, detail="ambiguous_confirmation")
        operation_id = uuid4()
        result = {
            "commit_result_id": str(uuid4()),
            "operation_id": str(operation_id),
            "confirmation_id": str(uuid4()),
            "status": "committed",
            "domain_changes": [],
            "created_revisions": [],
            "invalidated_calculations": [],
            "committed_at": datetime.now(UTC).isoformat(),
        }
        operations[operation_id] = {
            "state": "committed",
            "commit_result": result,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        idempotency[key] = (request.request_digest, operation_id)
        correlations[("CMD-00", UUID(x_correlation_id))] = operation_id
        response.headers["X-Spike-Operation-ID"] = str(operation_id)
        return {"data": result, "meta": meta(x_correlation_id, operation_id)}

    @app.post("/api/v1/queries/operations/status")
    def operation_status(
        request: OperationStatusRequest,
        authorization: str = Header(),
        x_correlation_id: str = Header(),
        x_causation_id: str = Header(),
    ) -> dict[str, Any]:
        authorize(authorization)
        UUID(x_correlation_id)
        UUID(x_causation_id)
        operation_id = None
        if request.locator.kind == "operation_id":
            operation_id = request.locator.operation_id
        elif request.locator.kind == "idempotency":
            key = (request.locator.method_id, request.locator.idempotency_key)
            record = idempotency.get(key)
            operation_id = record[1] if record else None
        else:
            operation_id = correlations.get(
                (request.locator.method_id, request.locator.correlation_id)
            )
        operation = operations.get(operation_id) if operation_id else None
        if operation is None:
            raise HTTPException(status_code=404, detail="not_found")
        data = {"operation_id": str(operation_id), **operation, "error": None}
        return {"data": data, "meta": meta(x_correlation_id, operation_id)}

    return app


def meta(correlation_id: str, operation_id: UUID | None) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "correlation_id": correlation_id,
        "operation_id": str(operation_id) if operation_id else None,
    }


app = create_spike_app()
