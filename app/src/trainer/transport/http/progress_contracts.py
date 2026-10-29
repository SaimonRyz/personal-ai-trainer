"""Closed HTTP contracts for E-G03 progress and calculation methods."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from trainer.transport.http.contracts import ClosedModel, Gap, ObjectRef, RequestContext

ProgressMetricKind = Literal[
    "record_full_reps",
    "record_external_load",
    "record_hold_duration",
    "record_cardio_time",
    "estimated_1rm",
    "external_working_set_volume",
]


class ProgressQueryRequest(ClosedModel):
    context: RequestContext
    metric_kind: ProgressMetricKind
    date_from: date
    date_to: date
    exercise_id: UUID | None = None
    condition_id: UUID | None = None
    include_history: bool = True
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_range(self) -> ProgressQueryRequest:
        if self.date_to < self.date_from:
            raise ValueError("date_to precedes date_from")
        if self.metric_kind == "external_working_set_volume" and self.condition_id:
            raise ValueError("read-time external volume has no comparison condition")
        return self


class ProgressValue(ClosedModel):
    exact_value: Decimal
    unit_code: Literal["repetition", "kilogram", "second", "kilogram_repetition"]


class ProgressExclusion(ClosedModel):
    source_ref: ObjectRef
    label: str = Field(min_length=1)
    reason_code: Literal[
        "warmup",
        "skipped_set",
        "interrupted_set",
        "unknown_repetitions",
        "partial_repetitions",
        "unknown_load",
        "ambiguous_load",
        "non_external_load",
        "unknown_side",
        "unknown_item_count",
        "no_applicable_external_volume",
        "unsupported_exercise",
    ]


class CalculationInputSnapshot(ClosedModel):
    role: Literal[
        "session_revision",
        "exercise_revision",
        "actual_set",
        "load_component",
        "execution_attestation",
    ]
    source_ref: ObjectRef | None
    normalized_value: Decimal | None
    unit_code: str | None
    snapshot: dict[str, object]


class ProgressFact(ClosedModel):
    record_ref: ObjectRef
    exercise_ref: ObjectRef
    metric_kind: ProgressMetricKind
    value: ProgressValue
    achieved_on: date
    condition_id: UUID
    session_ref: ObjectRef | None
    actual_set_ref: ObjectRef | None
    is_current: bool
    is_historical: bool


class DerivedProgressValue(ClosedModel):
    derived_value_ref: ObjectRef | None
    method_id: str
    method_version: str
    metric_kind: ProgressMetricKind
    value: ProgressValue | None
    achieved_on: date
    exercise_ref: ObjectRef
    exercise_revision_ref: ObjectRef | None
    session_ref: ObjectRef
    session_revision_ref: ObjectRef
    source_actual_set_ref: ObjectRef | None
    condition_id: UUID | None
    state: Literal["ephemeral", "current", "stale", "replaced"]
    completeness: Literal["complete", "partial", "insufficient"]
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    inputs: list[CalculationInputSnapshot]
    exclusions: list[ProgressExclusion]
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stale_reason: str | None

    @model_validator(mode="after")
    def validate_ephemeral_value(self) -> DerivedProgressValue:
        if (self.derived_value_ref is None) != (self.state == "ephemeral"):
            raise ValueError("only ephemeral points have no persisted value reference")
        if (self.value is None) != (self.completeness == "insufficient"):
            raise ValueError("only insufficient points have no numeric value")
        return self


class ActualProgressItem(ClosedModel):
    kind: Literal["actual"]
    point_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    point: ProgressFact


class DerivedProgressItem(ClosedModel):
    kind: Literal["derived"]
    point_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    point: DerivedProgressValue


ProgressItem = Annotated[ActualProgressItem | DerivedProgressItem, Field(discriminator="kind")]


class ProgressQueryResponse(ClosedModel):
    items: list[ProgressItem]
    next_cursor: str | None
    has_more: bool
    gaps: list[Gap]
    comparison_status: Literal[
        "no_points",
        "one_point",
        "two_points",
        "descriptive_direction",
        "insufficient",
        "not_comparable",
    ]


class CalculationActualSetSource(ClosedModel):
    kind: Literal["actual_set"]
    session_id: UUID
    session_revision_id: UUID
    exercise_id: UUID
    exercise_revision_id: UUID
    actual_set_id: UUID


class CalculationExerciseSessionSource(ClosedModel):
    kind: Literal["exercise_session"]
    session_id: UUID
    session_revision_id: UUID
    exercise_id: UUID


CalculationSource = Annotated[
    CalculationActualSetSource | CalculationExerciseSessionSource,
    Field(discriminator="kind"),
]


class CalculationExecutionAttestation(ClosedModel):
    source_actual_set_id: UUID
    grip_condition: str = Field(min_length=1, max_length=200)
    bench_pause_condition: Literal["no_pause", "intentional_chest_pause"]
    standard_confirmed: Literal[True]
    spotter_released_before_rep: Literal[True]
    no_assistance_during_rep: Literal[True]


class CalculationRequest(ClosedModel):
    context: RequestContext
    method_id: Literal["e1rm_brzycki", "external_working_set_volume"]
    method_version: Literal["1"]
    mode: Literal["ephemeral", "persist"]
    source: CalculationSource
    execution_attestation: CalculationExecutionAttestation | None = None
    prior_derived_value_id: UUID | None = None
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_method_shape(self) -> CalculationRequest:
        e1rm = self.method_id == "e1rm_brzycki"
        if e1rm != (self.source.kind == "actual_set"):
            raise ValueError("method and exact source kind do not match")
        if e1rm != (self.execution_attestation is not None):
            raise ValueError("e1RM requires calculation-time execution attestation")
        if (
            isinstance(self.source, CalculationActualSetSource)
            and self.execution_attestation
            and (self.execution_attestation.source_actual_set_id != self.source.actual_set_id)
        ):
            raise ValueError("attestation must be bound to the selected ActualSet")
        if self.prior_derived_value_id is not None and (self.mode != "persist" or not e1rm):
            raise ValueError("an exact prior calculation is allowed only for persistent e1RM")
        if self.method_id == "external_working_set_volume" and self.mode != "ephemeral":
            raise ValueError("external working-set volume is read-time only")
        return self


class CalculationResult(ClosedModel):
    calculation_id: UUID
    method_id: Literal["e1rm_brzycki", "external_working_set_volume"]
    method_version: Literal["1"]
    metric_kind: ProgressMetricKind
    value: ProgressValue | None
    completeness: Literal["complete", "partial", "insufficient"]
    state: Literal["ephemeral"]
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_refs: list[ObjectRef]
    applicability_version: str
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    exclusions: list[ProgressExclusion]
    execution_attestation: CalculationExecutionAttestation | None = None


class CalculationDraftResponse(ClosedModel):
    state: Literal["draft"]
    calculation_id: UUID
    draft: dict[str, object]
