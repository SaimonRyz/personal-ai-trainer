from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestContext(ClosedModel):
    conversation_id: str
    message_id: str
    effective_at: datetime
    timezone: str
    locale: str = "und"


class RenderPreviewRequest(ClosedModel):
    context: RequestContext
    revision_id: UUID
    detail_level: Literal["short", "full"]
    request_digest: str


class ShownRequest(ClosedModel):
    context: RequestContext
    revision_id: UUID
    preview_digest: str
    shown_message_id: str
    displayed_text_digest: str
    cross_dialog: bool
    request_digest: str


class ConfirmRequest(ClosedModel):
    context: RequestContext
    revision_id: UUID
    preview_digest: str
    confirmation_kind: Literal["normal"]
    confirmation_message_id: str
    explicit_intent: str
    confirmation_mode: Literal["same_dialog", "cross_dialog"]
    referenced_preview_id: UUID | None
    reshown_message_id: str | None
    request_digest: str


class OperationIdLocator(ClosedModel):
    kind: Literal["operation_id"]
    operation_id: UUID


class IdempotencyLocator(ClosedModel):
    kind: Literal["idempotency"]
    method_id: str
    idempotency_key: str


class CorrelationLocator(ClosedModel):
    kind: Literal["correlation"]
    method_id: str
    correlation_id: UUID


OperationLocator = Annotated[
    OperationIdLocator | IdempotencyLocator | CorrelationLocator,
    Field(discriminator="kind"),
]


class OperationStatusRequest(ClosedModel):
    context: RequestContext
    locator: OperationLocator
