from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from trainer_mcp.contracts import (
    ContextEquipmentInput,
    ExerciseCatalogInput,
    FamiliarExerciseSelection,
    FamiliarExerciseBatchItem,
    GoalInput,
    PlaceInput,
    PlanTreeInput,
    ProfileInput,
    ProgramIdentityInput,
    ProgramRecommendationInput,
    ProgramVersionDraftInput,
    TemporaryPlanConditionsInput,
)
from trainer_mcp.read_tools import register_read_tools
from trainer_mcp.write_tools import register_write_tools

mcp = FastMCP(
    "personal-ai-trainer",
    instructions=(
        "Local personal trainer tools. Use only the declared typed methods. "
        "Follow each tool's input schema exactly: place filters under the declared "
        "nested argument such as `query`, not at the top level. `conversation_id` and "
        "`message_id` are strings; `correlation_id` and `causation_id` are UUIDs. "
        "Copy domain object IDs only from tool results. Return API envelopes unchanged; "
        "never invent or rewrite persisted values."
    ),
    host="127.0.0.1",
    port=int(os.environ.get("TRAINER_MCP_PORT", "8766")),
    streamable_http_path="/mcp",
    json_response=True,
)

ALLOWED_STATIC_ROUTES = {
    "/api/v1/queries/profile-context",
    "/api/v1/queries/operations/status",
    "/api/v1/queries/exercises/search",
    "/api/v1/queries/planning-context",
    "/api/v1/queries/programs/get",
    "/api/v1/queries/plans/get",
    "/api/v1/queries/workouts/history",
    "/api/v1/queries/progress",
    "/api/v1/queries/record-candidates",
    "/api/v1/queries/records",
    "/api/v1/queries/measurements",
    "/api/v1/commands/record-decision-drafts",
    "/api/v1/commands/measurement-session-drafts",
    "/api/v1/commands/historical-record-drafts",
    "/api/v1/commands/calculations",
    "/api/v1/commands/correction-drafts",
    "/api/v1/queries/conclusions/eligible",
    "/api/v1/queries/recommendations/explanation",
    "/api/v1/commands/goal-drafts",
    "/api/v1/commands/onboarding-drafts",
    "/api/v1/commands/exercise-catalog-drafts",
    "/api/v1/commands/familiar-exercise-batches",
    "/api/v1/commands/program-drafts",
    "/api/v1/commands/program-publication-drafts",
    "/api/v1/commands/plan-drafts",
    "/api/v1/commands/inventory-change-drafts",
    "/api/v1/commands/plan-lifecycle-drafts",
    "/api/v1/commands/workout-report-drafts",
    "/api/v1/commands/evidence-drafts",
    "/api/v1/commands/previews",
}
PREVIEW_TRANSITION_ROUTE = re.compile(
    r"^/api/v1/commands/previews/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}/(?:shown|confirm)$"
)
PROGRAM_REVISION_ROUTE = re.compile(
    r"^/api/v1/commands/drafts/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}/revisions$"
)


def canonical_digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def with_request_digest(body: dict[str, Any]) -> dict[str, Any]:
    body["request_digest"] = canonical_digest(body)
    return body


def json_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=128)]
ConversationId = Annotated[str, Field(min_length=1, max_length=200)]
MessageId = Annotated[str, Field(min_length=1, max_length=200)]
DecimalString = Annotated[
    str,
    Field(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]{1,6})?$"),
]
Timezone = Annotated[str, Field(min_length=1, max_length=100)]


def api_call(
    path: str,
    body: dict[str, Any],
    correlation_id: UUID | str,
    causation_id: UUID | str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    base_url = os.environ.get("TRAINER_API_URL", "http://127.0.0.1:8765")
    token = os.environ.get("TRAINER_API_TOKEN")
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme != "http"
        or parsed_url.hostname != "127.0.0.1"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeError("TRAINER_API_URL must point directly to the local 127.0.0.1 API")
    if not token or "\r" in token or "\n" in token:
        raise RuntimeError("TRAINER_API_TOKEN is missing or invalid")
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
        raise RuntimeError("Idempotency key must contain 1 to 128 characters")
    if idempotency_key is not None and ("\r" in idempotency_key or "\n" in idempotency_key):
        raise RuntimeError("Idempotency key is invalid")
    if (
        path not in ALLOWED_STATIC_ROUTES
        and PREVIEW_TRANSITION_ROUTE.fullmatch(path) is None
        and PROGRAM_REVISION_ROUTE.fullmatch(path) is None
    ):
        raise RuntimeError("The requested trainer API route is not allowlisted")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Correlation-ID": str(correlation_id),
        "X-Causation-ID": str(causation_id),
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    try:
        with httpx.Client(
            base_url=base_url,
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(path, json=body, headers=headers)
            result = response.json()
    except (httpx.HTTPError, ValueError):
        raise RuntimeError("Local trainer API is unavailable or returned invalid JSON") from None
    if 300 <= response.status_code < 400:
        raise RuntimeError("Redirects from the local trainer API are not allowed")
    if not isinstance(result, dict):
        raise RuntimeError("Local trainer API returned an invalid response envelope")
    if response.status_code >= 400 and "error" not in result:
        raise RuntimeError("Local trainer API returned an invalid error envelope")
    if response.status_code < 400 and "data" not in result:
        raise RuntimeError("Local trainer API returned an invalid success envelope")
    return cast(dict[str, Any], result)


def context(conversation_id: str, message_id: str, timezone: str) -> dict[str, str]:
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "effective_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "timezone": timezone,
        "locale": "und",
    }


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_profile_context(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    purpose: str,
    correlation_id: UUID,
    causation_id: UUID,
) -> dict[str, Any]:
    """Read the current profile and its confirmed context for an explicit purpose."""
    body = {"context": context(conversation_id, message_id, timezone), "purpose": purpose}
    return api_call("/api/v1/queries/profile-context", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def create_onboarding_draft(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    profile: ProfileInput,
    goals: Annotated[list[GoalInput], Field(min_length=1)],
    places: list[PlaceInput],
    equipment: list[ContextEquipmentInput],
    familiar_exercises: list[FamiliarExerciseSelection] | None = None,
) -> dict[str, Any]:
    """Create an onboarding draft, optionally with exact familiar exercise revisions."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "profile": json_model(profile),
        "goals": [json_model(goal) for goal in goals],
        "places": [json_model(place) for place in places],
        "equipment": [json_model(item) for item in equipment],
        "familiar_exercises": [json_model(item) for item in familiar_exercises or []],
    }
    with_request_digest(body)
    return api_call(
        "/api/v1/commands/onboarding-drafts",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def create_goal_draft(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    name: Annotated[str, Field(min_length=1, max_length=200)],
    goal_type: Annotated[str, Field(min_length=1, max_length=100)],
    set_on: date,
    target_date: date,
    priority: Annotated[int, Field(ge=1, le=100)],
    status: Literal["active"],
    description: str | None = None,
    start_value: DecimalString | None = None,
    target_value: DecimalString | None = None,
    unit_code: Annotated[str, Field(min_length=1, max_length=100)] | None = None,
    comment: Annotated[str, Field(max_length=2000)] | None = None,
) -> dict[str, Any]:
    """Create a goal draft only; this does not persist a confirmed goal."""
    goal = {
        "goal_id": None,
        "parent_goal_id": None,
        "name": name,
        "description": description,
        "goal_type": goal_type,
        "start_value": start_value,
        "target_value": target_value,
        "unit_code": unit_code,
        "set_on": set_on.isoformat(),
        "target_date": target_date.isoformat(),
        "priority": priority,
        "status": status,
        "comment": comment,
    }
    body: dict[str, Any] = {
        "context": context(conversation_id, message_id, timezone),
        "intent": "create",
        "goal": goal,
        "expected_revision_id": None,
        "reason": None,
    }
    body["request_digest"] = canonical_digest(body)
    return api_call(
        "/api/v1/commands/goal-drafts",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def search_exercises(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    text: Annotated[str, Field(min_length=1, max_length=200)],
    correlation_id: UUID,
    causation_id: UUID,
    metric_kinds: list[str] | None = None,
    equipment_types: list[str] | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Search planning-eligible exercises using the current closed API query."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "mode": "search",
        "text": text,
        "metric_kinds": metric_kinds,
        "equipment_type": equipment_types,
        "limit": limit,
        "cursor": cursor,
    }
    return api_call("/api/v1/queries/exercises/search", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_exercise_exact(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    exercise_id: UUID,
    correlation_id: UUID,
    causation_id: UUID,
    revision_id: UUID | None = None,
) -> dict[str, Any]:
    """Read the current or exact immutable revision of one exercise."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "mode": "exact",
        "exercise_id": str(exercise_id),
        "revision_id": str(revision_id) if revision_id else None,
    }
    return api_call("/api/v1/queries/exercises/search", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_planning_context(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    target_date: date,
    correlation_id: UUID,
    causation_id: UUID,
    program_id: UUID | None = None,
    temporary_conditions: TemporaryPlanConditionsInput | None = None,
) -> dict[str, Any]:
    """Read planning facts and global equipment; apply only explicit temporary overlay."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "target_date": target_date.isoformat(),
        "program_id": str(program_id) if program_id else None,
        "temporary_conditions": (
            json_model(temporary_conditions) if temporary_conditions is not None else None
        ),
    }
    return api_call("/api/v1/queries/planning-context", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_current_program(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    program_id: UUID,
    correlation_id: UUID,
    causation_id: UUID,
) -> dict[str, Any]:
    """Read the current immutable version of one program."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "mode": "current",
        "program_id": str(program_id),
    }
    return api_call("/api/v1/queries/programs/get", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_exact_program(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    program_id: UUID,
    program_version_id: UUID,
    correlation_id: UUID,
    causation_id: UUID,
) -> dict[str, Any]:
    """Read an exact immutable program version."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "mode": "exact",
        "program_id": str(program_id),
        "program_version_id": str(program_version_id),
    }
    return api_call("/api/v1/queries/programs/get", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def create_exercise_draft(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    exercise: ExerciseCatalogInput,
    intent: Literal["create", "correct"] = "create",
    expected_revision_id: UUID | None = None,
    reason: Annotated[str, Field(min_length=1, max_length=1000)] | None = None,
) -> dict[str, Any]:
    """Create or replace an exercise draft. Measurements: repetitions/count,
    duration/time, distance/length (required or optional). Record external mass
    in each planned or performed set's loads, not in exercise measurements.
    """
    body = {
        "context": context(conversation_id, message_id, timezone),
        "intent": intent,
        "exercise": json_model(exercise),
        "expected_revision_id": str(expected_revision_id) if expected_revision_id else None,
        "reason": reason,
    }
    with_request_digest(body)
    return api_call(
        "/api/v1/commands/exercise-catalog-drafts",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def create_familiar_exercise_batch_draft(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    profile_id: UUID,
    expected_profile_revision_id: UUID,
    items: list[FamiliarExerciseBatchItem],
    unresolved_names: list[str] | None = None,
) -> dict[str, Any]:
    """Prepare one familiar-exercise list from exact catalog refs and new exercise drafts.
    Show and confirm only this batch preview; do not confirm child exercise drafts.
    """
    body = {
        "context": context(conversation_id, message_id, timezone),
        "profile_id": str(profile_id),
        "expected_profile_revision_id": str(expected_profile_revision_id),
        "items": [json_model(item) for item in items],
        "unresolved_names": unresolved_names or [],
    }
    with_request_digest(body)
    return api_call(
        "/api/v1/commands/familiar-exercise-batches",
        body, correlation_id, causation_id, idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def create_program_draft(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    goal_revision_id: UUID,
    version: ProgramVersionDraftInput,
    intent: Literal["create", "new_version"] = "create",
    program: ProgramIdentityInput | None = None,
    program_id: UUID | None = None,
    expected_current_version_id: UUID | None = None,
) -> dict[str, Any]:
    """Create a technical program draft; publication is a separate confirmed action."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "intent": intent,
        "program_id": str(program_id) if program_id else None,
        "expected_current_version_id": (
            str(expected_current_version_id) if expected_current_version_id else None
        ),
        "goal_revision_id": str(goal_revision_id),
        "program": json_model(program) if program is not None else None,
        "version": json_model(version),
    }
    with_request_digest(body)
    return api_call(
        "/api/v1/commands/program-drafts",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def create_program_publication_draft(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    source_draft_id: UUID,
    source_revision_id: UUID,
    publication_reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None,
    expected_current_version_id: UUID | None = None,
) -> dict[str, Any]:
    """Create a separate publication draft for one exact program revision."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "source_draft_id": str(source_draft_id),
        "source_revision_id": str(source_revision_id),
        "expected_current_version_id": (
            str(expected_current_version_id) if expected_current_version_id else None
        ),
        "publication_reason": publication_reason,
    }
    with_request_digest(body)
    return api_call(
        "/api/v1/commands/program-publication-drafts",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_plan(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    plan_id: UUID,
    mode: Literal["current", "exact"],
    correlation_id: UUID,
    causation_id: UUID,
    revision_id: UUID | None = None,
) -> dict[str, Any]:
    """Read one current or exact immutable plan revision."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "mode": mode,
        "plan_id": str(plan_id),
        "revision_id": str(revision_id) if revision_id else None,
    }
    return api_call("/api/v1/queries/plans/get", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def create_plan_draft(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    plan: PlanTreeInput,
    recommendations: Annotated[
        list[ProgramRecommendationInput], Field(min_length=1, max_length=100)
    ],
) -> dict[str, Any]:
    """Create a plan draft with exact recommendation evidence; does not confirm it."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "plan": json_model(plan),
        "recommendations": [json_model(item) for item in recommendations],
    }
    with_request_digest(body)
    return api_call(
        "/api/v1/commands/plan-drafts",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_eligible_conclusions(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    topic: Annotated[str, Field(min_length=1, max_length=200)],
    applicability_snapshot: dict[str, Any],
    decision_date: date,
    correlation_id: UUID,
    causation_id: UUID,
) -> dict[str, Any]:
    """Read only conclusions the API currently considers eligible for a dated decision."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "topic": topic,
        "applicability_snapshot": applicability_snapshot,
        "decision_date": decision_date.isoformat(),
    }
    return api_call("/api/v1/queries/conclusions/eligible", body, correlation_id, causation_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_recommendation_explanation(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    recommendation_id: UUID,
    detail_level: Literal["short", "detailed"],
    correlation_id: UUID,
    causation_id: UUID,
) -> dict[str, Any]:
    """Read the saved explanation for one exact program or plan recommendation."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "recommendation_id": str(recommendation_id),
        "detail_level": detail_level,
    }
    return api_call(
        "/api/v1/queries/recommendations/explanation", body, correlation_id, causation_id
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def render_preview(
    conversation_id: ConversationId,
    message_id: MessageId,
    timezone: Timezone,
    revision_id: UUID,
    detail_level: Literal["short", "full"],
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    """Request the API's canonical preview for one exact draft revision."""
    body: dict[str, Any] = {
        "context": context(conversation_id, message_id, timezone),
        "revision_id": str(revision_id),
        "detail_level": detail_level,
    }
    body["request_digest"] = canonical_digest(body)
    return api_call(
        "/api/v1/commands/previews", body, correlation_id, causation_id, idempotency_key
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def acknowledge_preview_shown(
    preview_id: UUID,
    revision_id: UUID,
    preview_digest: Digest,
    canonical_text: str,
    conversation_id: ConversationId,
    shown_message_id: MessageId,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    timezone: Timezone,
    cross_dialog: bool = False,
) -> dict[str, Any]:
    """Record that the exact canonical preview text was fully shown in this dialog."""
    body: dict[str, Any] = {
        "context": context(conversation_id, shown_message_id, timezone),
        "revision_id": str(revision_id),
        "preview_digest": preview_digest,
        "shown_message_id": shown_message_id,
        "displayed_text_digest": hashlib.sha256(canonical_text.encode()).hexdigest(),
        "cross_dialog": cross_dialog,
    }
    body["request_digest"] = canonical_digest(body)
    return api_call(
        f"/api/v1/commands/previews/{preview_id}/shown",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def confirm_preview(
    preview_id: UUID,
    revision_id: UUID,
    preview_digest: Digest,
    conversation_id: ConversationId,
    confirmation_message_id: MessageId,
    correlation_id: UUID,
    causation_id: UUID,
    idempotency_key: IdempotencyKey,
    timezone: Timezone,
    explicit_intent: Annotated[str, Field(min_length=1, max_length=1000)],
    confirmation_mode: Literal["same_dialog", "cross_dialog"],
    referenced_preview_id: UUID | None = None,
    reshown_message_id: MessageId | None = None,
    confirmation_kind: Literal["normal", "publication"] = "normal",
) -> dict[str, Any]:
    """Confirm only the exact preview previously shown and explicitly approved by the user."""
    body: dict[str, Any] = {
        "context": context(conversation_id, confirmation_message_id, timezone),
        "revision_id": str(revision_id),
        "preview_digest": preview_digest,
        "confirmation_kind": confirmation_kind,
        "confirmation_message_id": confirmation_message_id,
        "explicit_intent": explicit_intent,
        "confirmation_mode": confirmation_mode,
        "referenced_preview_id": (
            str(referenced_preview_id) if referenced_preview_id is not None else None
        ),
        "reshown_message_id": reshown_message_id,
    }
    body["request_digest"] = canonical_digest(body)
    return api_call(
        f"/api/v1/commands/previews/{preview_id}/confirm",
        body,
        correlation_id,
        causation_id,
        idempotency_key,
    )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_operation_status(
    operation_id: UUID,
    conversation_id: ConversationId,
    message_id: MessageId,
    correlation_id: UUID,
    causation_id: UUID,
    timezone: str = "Asia/Novokuznetsk",
) -> dict[str, Any]:
    """Read one operation by its exact ID after an uncertain response."""
    body = {
        "context": context(conversation_id, message_id, timezone),
        "locator": {"kind": "operation_id", "operation_id": str(operation_id)},
    }
    return api_call("/api/v1/queries/operations/status", body, correlation_id, causation_id)


def _call_read_tool(
    path: str,
    body: dict[str, Any],
    correlation_id: UUID | str,
    causation_id: UUID | str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return api_call(path, body, correlation_id, causation_id, idempotency_key)


register_read_tools(mcp, _call_read_tool, context)
register_write_tools(mcp, _call_read_tool, context, canonical_digest)


def _close_tool_argument_schemas() -> None:
    """Reject undeclared MCP arguments, matching the API's closed DTO policy."""
    manager = mcp._tool_manager  # FastMCP 1.x has no public tool-schema config hook.
    for tool in manager.list_tools():
        model = tool.fn_metadata.arg_model
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
        tool.parameters = model.model_json_schema(by_alias=True)


_close_tool_argument_schemas()


def main() -> None:
    transport = os.environ.get("TRAINER_MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http"}:
        raise RuntimeError("TRAINER_MCP_TRANSPORT must be stdio or streamable-http")
    try:
        mcp.run(transport=cast(Any, transport))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
