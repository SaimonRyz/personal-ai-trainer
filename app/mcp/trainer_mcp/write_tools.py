from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, model_validator

from trainer_mcp.contracts import (
    CalculationInput,
    ClosedModel,
    EquipmentCardInput,
    HistoricalRecordDraftInput,
    MeasurementDraftInput,
    PlanTreeInput,
    ProgramIdentityInput,
    ProgramRecommendationInput,
    ProgramVersionDraftInput,
    RecordDecisionInput,
    WorkoutCorrectionDraftInput,
    WorkoutReportInput,
)
from trainer_mcp.read_tools import ApiCall, ContextFactory


class ProgramDraftReplacement(ClosedModel):
    op: Literal["replace"]
    path: Literal["/goal_revision_id", "/program", "/version"]
    value: UUID | ProgramIdentityInput | ProgramVersionDraftInput

    @model_validator(mode="after")
    def validate_target(self) -> ProgramDraftReplacement:
        expected = {
            "/goal_revision_id": UUID,
            "/program": ProgramIdentityInput,
            "/version": ProgramVersionDraftInput,
        }[self.path]
        if not isinstance(self.value, expected):
            raise ValueError("replacement value does not match target schema")
        return self


class WorkoutDraftReplacement(ClosedModel):
    op: Literal["replace"]
    path: Literal["/report"]
    value: WorkoutReportInput


class InventoryCreateInput(ClosedModel):
    intent: Literal["create"]
    equipment: EquipmentCardInput


class InventoryCorrectInput(ClosedModel):
    intent: Literal["correct"]
    equipment_id: UUID
    expected_current_revision_id: UUID
    equipment: EquipmentCardInput
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_reason(self) -> InventoryCorrectInput:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("correction reason must be nonblank")
        return self


InventoryChangeInput = Annotated[
    InventoryCreateInput | InventoryCorrectInput, Field(discriminator="intent")
]


class PlanRevisionRef(ClosedModel):
    object_type: Literal["workout_plan"]
    object_id: UUID
    revision_id: UUID


class PlanLifecycleInput(ClosedModel):
    intent: Literal["cancel", "miss", "reschedule"]
    source_ref: PlanRevisionRef
    expected_current_revision_id: UUID
    reason: str | None = Field(default=None, max_length=2000)
    replacement_plan: PlanTreeInput | None = None
    replacement_recommendations: list[ProgramRecommendationInput] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def validate_lifecycle_shape(self) -> PlanLifecycleInput:
        if self.source_ref.revision_id != self.expected_current_revision_id:
            raise ValueError("source revision must match expected current revision")
        reschedule = self.intent == "reschedule"
        if reschedule != (self.replacement_plan is not None):
            raise ValueError("reschedule requires exactly one replacement plan")
        if reschedule != (self.replacement_recommendations is not None):
            raise ValueError("reschedule requires replacement recommendations")
        return self


class ScientificSourceInput(ClosedModel):
    citation_key: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=4000)
    authors_or_org: str = Field(min_length=1, max_length=4000)
    publication_year: int = Field(ge=1800)
    source_type: str = Field(min_length=1, max_length=100)
    doi: str | None = Field(default=None, min_length=1, max_length=2000)
    pmid: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=4000)
    metadata_snapshot: dict[str, Any]
    availability: str = Field(min_length=1, max_length=100)
    conflict_of_interest_note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_source(self) -> ScientificSourceInput:
        if not any((self.doi, self.pmid, self.url)):
            raise ValueError("source requires a DOI, PMID, or URL")
        if self.publication_year > datetime.now().year:
            raise ValueError("publication year cannot be in the future")
        if any(
            not getattr(self, name).strip() for name in ("citation_key", "title", "authors_or_org")
        ):
            raise ValueError("source metadata cannot be blank")
        return self


class EvidenceSourceInput(ClosedModel):
    source: ScientificSourceInput
    relation: Literal["supports", "contradicts", "contextualizes"]
    cited_locator: str | None = Field(default=None, min_length=1, max_length=4000)
    note: str | None = Field(default=None, min_length=1, max_length=4000)


class EvidenceConclusionInput(ClosedModel):
    conclusion_id: UUID | None = None
    topic: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=8000)
    population_conditions: str = Field(min_length=1, max_length=4000)
    confidence: Literal["high", "moderate", "low", "insufficient"]
    review_due_on: date
    applicability: list[dict[str, Any]] = Field(max_length=100)
    caveats: list[dict[str, Any]] = Field(max_length=100)


class SourceAvailabilityUpdateInput(ClosedModel):
    source_id: UUID
    expected_availability: Literal["available", "unavailable"]
    expected_last_checked_at: datetime | None
    new_availability: Literal["available", "unavailable"]
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_reason(self) -> SourceAvailabilityUpdateInput:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("correction reason must be nonblank")
        if self.new_availability == self.expected_availability:
            raise ValueError("availability update must change the value")
        return self


class EvidenceInput(ClosedModel):
    sources: list[EvidenceSourceInput] = Field(min_length=1, max_length=20)
    conclusion: EvidenceConclusionInput

    @model_validator(mode="after")
    def validate_unique_source_relations(self) -> EvidenceInput:
        keys = [(item.source.citation_key, item.relation) for item in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("source relations must be unique")
        return self


def register_write_tools(
    mcp: FastMCP,
    api_call: ApiCall,
    context: ContextFactory,
    canonical_digest: Callable[[dict[str, Any]], str],
) -> None:
    write_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_evidence_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        evidence: EvidenceInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare sources and a scientific conclusion as draft only; never verifies it."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            "evidence": evidence.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/evidence-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def update_source_availability_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        update: SourceAvailabilityUpdateInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare one availability correction through the normal preview/confirmation flow."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            "source_availability_update": update.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/evidence-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def revise_program_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        draft_id: UUID,
        base_revision_id: UUID,
        changes: Annotated[list[ProgramDraftReplacement], Field(min_length=1, max_length=100)],
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Revise an existing program draft; publication needs a fresh publication draft."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            "base_revision_id": str(base_revision_id),
            "changes": [change.model_dump(mode="json") for change in changes],
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            f"/api/v1/commands/drafts/{draft_id}/revisions",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_workout_report_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        report: WorkoutReportInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a draft from the complete user-reported workout; no fact until confirmation."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            "report": report.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/workout-report-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def revise_workout_report_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        draft_id: UUID,
        base_revision_id: UUID,
        change: WorkoutDraftReplacement,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Replace the complete report in the same open CMD-06 draft; requires a fresh preview."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            "base_revision_id": str(base_revision_id),
            "changes": [change.model_dump(mode="json")],
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            f"/api/v1/commands/drafts/{draft_id}/revisions",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_plan_lifecycle_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        transition: PlanLifecycleInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare cancellation, missed status, or full replacement of one exact plan."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            **transition.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/plan-lifecycle-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_inventory_change_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        inventory: InventoryChangeInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare a complete current equipment card create/correction for confirmation."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            **inventory.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/inventory-change-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_record_decision_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        decision: RecordDecisionInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare an accept/reject decision for one exact record candidate."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            **decision.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/record-decision-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_measurement_session_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        measurement: MeasurementDraftInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare a complete body-measurement session create/correction draft."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            **measurement.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/measurement-session-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_historical_record_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        historical_record: HistoricalRecordDraftInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare one manually reported pre-system record, or correct its exact record."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            **historical_record.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/historical-record-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def calculate_metric(
        conversation_id: str,
        message_id: str,
        timezone: str,
        calculation: CalculationInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Calculate from exact confirmed sources; persistence remains preview/confirm gated."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            **calculation.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/calculations",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )

    @mcp.tool(structured_output=True, annotations=write_annotations)
    def create_workout_correction_draft(
        conversation_id: str,
        message_id: str,
        timezone: str,
        correction: WorkoutCorrectionDraftInput,
        correlation_id: UUID,
        causation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare one full-report correction for the exact saved workout revision."""
        body: dict[str, Any] = {
            "context": context(conversation_id, message_id, timezone),
            **correction.model_dump(mode="json"),
        }
        body["request_digest"] = canonical_digest(body)
        return api_call(
            "/api/v1/commands/correction-drafts",
            body,
            correlation_id,
            causation_id,
            idempotency_key,
        )
