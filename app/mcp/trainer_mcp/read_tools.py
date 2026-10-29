from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from trainer_mcp.contracts import (
    MeasurementQueryInput,
    ProgressQueryInput,
    RecordCandidatesInput,
    RecordsInput,
    WorkoutHistoryInput,
)


class ApiCall(Protocol):
    def __call__(
        self,
        path: str,
        body: dict[str, Any],
        correlation_id: UUID | str,
        causation_id: UUID | str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...


ContextFactory = Callable[[str, str, str], dict[str, str]]


def register_read_tools(
    mcp: FastMCP,
    api_call: ApiCall,
    context: ContextFactory,
) -> None:
    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(structured_output=True, annotations=read_annotations)
    def get_workout_history(
        conversation_id: str,
        message_id: str,
        timezone: str,
        query: WorkoutHistoryInput,
        correlation_id: UUID,
        causation_id: UUID,
    ) -> dict[str, Any]:
        """Read confirmed workout revisions by date range or from a complete pinned snapshot."""
        body = {
            "context": context(conversation_id, message_id, timezone),
            **query.model_dump(mode="json"),
        }
        return api_call("/api/v1/queries/workouts/history", body, correlation_id, causation_id)

    @mcp.tool(structured_output=True, annotations=read_annotations)
    def get_progress(
        conversation_id: str,
        message_id: str,
        timezone: str,
        query: ProgressQueryInput,
        correlation_id: UUID,
        causation_id: UUID,
    ) -> dict[str, Any]:
        """Read actual and calculated progress without storing read-time metrics."""
        body = {
            "context": context(conversation_id, message_id, timezone),
            **query.model_dump(mode="json"),
        }
        return api_call("/api/v1/queries/progress", body, correlation_id, causation_id)

    @mcp.tool(structured_output=True, annotations=read_annotations)
    def get_record_candidates(
        conversation_id: str,
        message_id: str,
        timezone: str,
        query: RecordCandidatesInput,
        correlation_id: UUID,
        causation_id: UUID,
    ) -> dict[str, Any]:
        """Read record candidates and their exact source/eligibility state."""
        body = {
            "context": context(conversation_id, message_id, timezone),
            **query.model_dump(mode="json"),
        }
        return api_call("/api/v1/queries/record-candidates", body, correlation_id, causation_id)

    @mcp.tool(structured_output=True, annotations=read_annotations)
    def get_records(
        conversation_id: str,
        message_id: str,
        timezone: str,
        query: RecordsInput,
        correlation_id: UUID,
        causation_id: UUID,
    ) -> dict[str, Any]:
        """Read confirmed record results in the requested current/history view."""
        body = {
            "context": context(conversation_id, message_id, timezone),
            **query.model_dump(mode="json"),
        }
        return api_call("/api/v1/queries/records", body, correlation_id, causation_id)

    @mcp.tool(structured_output=True, annotations=read_annotations)
    def get_body_measurements(
        conversation_id: str,
        message_id: str,
        timezone: str,
        query: MeasurementQueryInput,
        correlation_id: UUID,
        causation_id: UUID,
    ) -> dict[str, Any]:
        """Read confirmed body measurements or calculate BMI only on explicit request."""
        body = {
            "context": context(conversation_id, message_id, timezone),
            **query.model_dump(mode="json"),
        }
        return api_call("/api/v1/queries/measurements", body, correlation_id, causation_id)
