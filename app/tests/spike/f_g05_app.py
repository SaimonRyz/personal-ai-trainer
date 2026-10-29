from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from trainer.application.context import ContextService
from trainer.transport.http.evidence_contracts import EvidenceDraftRequest

TOKEN = "spike-local-token"
app = FastAPI()


@app.post("/api/v1/commands/evidence-drafts")
async def create_evidence_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    contract = EvidenceDraftRequest.model_validate(body)
    ContextService.verify_digest(body, contract.request_digest)
    if not x_correlation_id or not x_causation_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="missing_request_metadata")

    return {
        "data": {
            "synthetic": True,
            "effect": "none",
            "path": request.url.path,
            "context": contract.context.model_dump(mode="json"),
            "evidence": contract.evidence.model_dump(mode="json"),
            "request_digest": contract.request_digest,
            "idempotency_key": idempotency_key,
        },
        "meta": {
            "api_version": "v1",
            "correlation_id": x_correlation_id,
            "causation_id": x_causation_id,
        },
    }
