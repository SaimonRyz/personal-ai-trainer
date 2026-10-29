"""Closed E-G07 wire contracts. Owner capability is never an input field."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from trainer.transport.http.contracts import ClosedModel, Gap, ObjectRef, RequestContext

Confidence = Literal["high", "moderate", "low", "insufficient"]
KnowledgeStatus = Literal["draft", "verified", "outdated", "rejected"]


class ScientificSourceInput(ClosedModel):
    citation_key: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=4000)
    authors_or_org: str = Field(min_length=1, max_length=4000)
    publication_year: int = Field(ge=1800)
    source_type: str = Field(min_length=1, max_length=100)
    doi: str | None = Field(default=None, min_length=1, max_length=2000)
    pmid: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=4000)
    metadata_snapshot: dict[str, object]
    availability: str = Field(min_length=1, max_length=100)
    conflict_of_interest_note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_identifiers(self) -> ScientificSourceInput:
        if not any((self.doi, self.pmid, self.url)) or self.publication_year > datetime.now().year:
            raise ValueError("source requires a locator and a valid publication year")
        for name in ("citation_key", "title", "authors_or_org"):
            if not getattr(self, name).strip():
                raise ValueError("source metadata cannot be blank")
        return self


class EvidenceSourceInput(ClosedModel):
    source: ScientificSourceInput
    relation: Literal["supports", "contradicts", "contextualizes"]
    cited_locator: str | None = Field(default=None, min_length=1, max_length=4000)
    note: str | None = Field(default=None, min_length=1, max_length=4000)


class ConclusionInput(ClosedModel):
    conclusion_id: UUID | None = None
    topic: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=8000)
    population_conditions: str = Field(min_length=1, max_length=4000)
    confidence: Confidence
    review_due_on: date
    applicability: list[dict[str, object]] = Field(max_length=100)
    caveats: list[dict[str, object]] = Field(max_length=100)


class EvidenceInput(ClosedModel):
    sources: list[EvidenceSourceInput] = Field(min_length=1, max_length=20)
    conclusion: ConclusionInput

    @model_validator(mode="after")
    def unique_links(self) -> EvidenceInput:
        keys = [(item.source.citation_key, item.relation) for item in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("source relations must be unique")
        return self


class EvidenceDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: EvidenceInput


class SourceAvailabilityUpdateInput(ClosedModel):
    source_id: UUID
    expected_availability: Literal["available", "unavailable"]
    expected_last_checked_at: datetime | None
    new_availability: Literal["available", "unavailable"]
    reason: str = Field(min_length=1, max_length=2000)


class EvidenceAvailabilityDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_availability_update: SourceAvailabilityUpdateInput


# The HTTP route can accept either closed CMD-14 variant without changing the
# existing content-draft payload shape.
EvidenceCommandRequest = EvidenceDraftRequest | EvidenceAvailabilityDraftRequest


class ProtocolCheckInput(ClosedModel):
    check_id: str = Field(min_length=1, max_length=100)
    result: bool
    note: str | None = Field(default=None, max_length=4000)


class VerificationDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    conclusion_version_id: UUID
    protocol_version: str = Field(min_length=1, max_length=100)
    checklist: list[ProtocolCheckInput] = Field(min_length=1, max_length=100)
    decision: Literal["verified", "rejected"]
    reason: str = Field(min_length=1, max_length=4000)


class EligibleConclusionsRequest(ClosedModel):
    context: RequestContext
    topic: str = Field(min_length=1, max_length=200)
    applicability_snapshot: dict[str, object]
    decision_date: date


class EligibleConclusionItem(ClosedModel):
    conclusion_version_id: UUID
    status: Literal["verified"]
    confidence: Confidence
    review_due_on: date
    source_ids: list[UUID]
    applicability_assessment: dict[str, object]


class EligibleConclusionsResponse(ClosedModel):
    items: list[EligibleConclusionItem]
    conflicts: list[Gap]
    gaps: list[Gap]


class RecommendationExplanationRequest(ClosedModel):
    context: RequestContext
    recommendation_id: UUID
    detail_level: Literal["short", "detailed"]


class ConclusionSourceView(ClosedModel):
    source_ref: ObjectRef
    relation: Literal["supports", "contradicts", "contextualizes"]
    cited_locator: str | None
    metadata_snapshot: dict[str, object]
    current_availability: Literal["available", "unavailable"]
    last_checked_at: datetime | None

class ConclusionBasisView(ClosedModel):
    conclusion_version_ref: ObjectRef
    statement: str
    population_conditions: str
    confidence: Confidence
    review_due_on: date
    status_at_decision: KnowledgeStatus
    current_status: KnowledgeStatus
    applicability_assessment: dict[str, object]
    caveats: list[dict[str, object]]
    sources: list[ConclusionSourceView]


class RecommendationExplanationResponse(ClosedModel):
    decision_text: str
    confidence: Confidence
    explanation: str
    fact_refs: list[ObjectRef]
    derived_value_ids: list[UUID]
    conclusion_version_ids: list[UUID]
    conclusion_basis: list[ConclusionBasisView]
    assumptions: dict[str, object]
    alternatives: dict[str, object]
    revisit_condition: str
    stale_markers: list[ObjectRef]
