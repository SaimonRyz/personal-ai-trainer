from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trainer.transport.http.inventory_types import (
    ContextEquipmentInput,
    EquipmentCardInput,
    EquipmentCardView,
    PlanningEquipmentTypeCode,
)


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldError(ClosedModel):
    path: str
    code: str
    message_safe: str


class DependencyRef(ClosedModel):
    dependency_type: str
    object_id: UUID
    revision_id: UUID
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str


class ErrorDetail(ClosedModel):
    code: str
    message_safe: str
    retryable: bool
    operation_state: Literal["received", "executing", "committed", "failed", "recovery_required"]
    correlation_id: UUID
    operation_id: UUID | None = None
    idempotency_key: str | None = None
    field_errors: list[FieldError]
    changed_dependencies: list[DependencyRef]
    required_next_action: str | None = None
    retry_after_seconds: int | None = None


class ResponseMeta(ClosedModel):
    api_version: Literal["v1"] = "v1"
    correlation_id: UUID
    operation_id: UUID | None = None


class ErrorEnvelope(ClosedModel):
    error: ErrorDetail
    meta: dict[Literal["api_version"], Literal["v1"]]


class RequestContext(ClosedModel):
    conversation_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    effective_at: datetime
    timezone: str = Field(min_length=1, max_length=100)
    locale: str | None = Field(default=None, min_length=2, max_length=35)


class ObjectRef(ClosedModel):
    object_type: str
    object_id: UUID
    revision_id: UUID | None = None


class Gap(ClosedModel):
    path: str
    code: Literal["missing", "ambiguous", "conflict", "unsupported"]
    disposition: Literal["block", "clarify", "assume_with_disclosure", "leave_unknown"]
    message_safe: str


class ProfileInput(ClosedModel):
    birth_date: date | None = None
    sex: Literal["male", "female", "other", "not_stated"] | None = None
    timezone: str
    started_on: date | None = None
    desired_sessions_per_week: int | None = Field(default=None, ge=1, le=14)
    usual_place_id: UUID | None = None
    comment: str | None = None


class FamiliarExerciseRef(ClosedModel):
    object_type: Literal["exercise"]
    object_id: UUID
    revision_id: UUID


class FamiliarExerciseSelection(ClosedModel):
    exercise_ref: FamiliarExerciseRef


class FamiliarExerciseView(ClosedModel):
    exercise_ref: FamiliarExerciseRef
    name: str
    current: bool


class GoalInput(ClosedModel):
    goal_id: UUID | None = None
    parent_goal_id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    goal_type: str = Field(min_length=1, max_length=100, description="Use performance or strength; strength includes a one-repetition maximum goal.")
    start_value: Decimal | None = None
    target_value: Decimal | None = None
    unit_code: str | None = Field(default=None, description="Use catalog codes: kilogram for kg, repetition for reps; never send kg.")
    set_on: date
    target_date: date
    priority: int = Field(ge=1, le=100)
    status: Literal["active", "achieved", "cancelled"]
    comment: str | None = None


class PlaceInput(ClosedModel):
    place_id: UUID
    name: str = Field(min_length=1, max_length=200)
    place_type: str = Field(min_length=1, max_length=100)
    status: Literal["active", "unavailable", "archived"]
    comment: str | None = None


class PlaceEquipmentAvailabilityInput(ClosedModel):
    place_id: UUID
    equipment_id: UUID
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    context: str | None = None


class OnboardingDraftRequest(ClosedModel):
    context: RequestContext
    profile: ProfileInput
    goals: list[GoalInput] = Field(min_length=1)
    places: list[PlaceInput]
    equipment: list[ContextEquipmentInput]
    familiar_exercises: list[FamiliarExerciseSelection] = Field(default_factory=list)
    draft_id: UUID | None = None
    expected_revision_id: UUID | None = None
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_revision_target(self) -> OnboardingDraftRequest:
        if (self.draft_id is None) != (self.expected_revision_id is None):
            raise ValueError("draft_id and expected_revision_id must be supplied together")
        return self


class InventoryCreateDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["create"]
    equipment: EquipmentCardInput


class InventoryCorrectDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["correct"]
    equipment_id: UUID
    expected_current_revision_id: UUID
    equipment: EquipmentCardInput
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_reason(self) -> InventoryCorrectDraftRequest:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("correction reason must be nonblank")
        return self


InventoryChangeDraftRequest = Annotated[
    InventoryCreateDraftRequest | InventoryCorrectDraftRequest, Field(discriminator="intent")
]


class GoalDraftRequest(ClosedModel):
    context: RequestContext
    intent: Literal["create", "correct", "cancel"]
    goal: GoalInput
    expected_revision_id: UUID | None = None
    reason: str | None = None
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_intent_shape(self) -> GoalDraftRequest:
        if self.intent == "create" and self.expected_revision_id is not None:
            raise ValueError("create cannot have expected_revision_id")
        if self.intent != "create" and (
            self.goal.goal_id is None or self.expected_revision_id is None
        ):
            raise ValueError("correction and cancellation require goal and expected revision")
        return self


class ExerciseAliasInput(ClosedModel):
    alias: str = Field(min_length=1, max_length=200)
    locale: str | None = Field(default=None, min_length=1, max_length=35)
    context: str | None = Field(default=None, min_length=1, max_length=200)


class ExistingMuscleRoleInput(ClosedModel):
    kind: Literal["existing"]
    muscle_group_id: UUID
    role: Literal["primary", "secondary", "stabilizer"]


class CreateMuscleRoleInput(ClosedModel):
    kind: Literal["create_by_name"]
    name: str = Field(min_length=1, max_length=200)
    status_code: str = Field(min_length=1, max_length=100)
    role: Literal["primary", "secondary", "stabilizer"]


ExerciseMuscleRoleInput = Annotated[
    ExistingMuscleRoleInput | CreateMuscleRoleInput, Field(discriminator="kind")
]


class ExerciseMeasurementInput(ClosedModel):
    metric_kind: str = Field(min_length=1, max_length=100)
    requiredness: Literal["required", "optional"]
    unit_family: str = Field(min_length=1, max_length=100)


class ExerciseEquipmentRequirementInput(ClosedModel):
    equipment_type: str = Field(min_length=1, max_length=100)
    optionality: Literal["required", "optional", "alternative"]
    alternative_group: str | None = Field(default=None, min_length=1, max_length=100)


class ExerciseCatalogInput(ClosedModel):
    exercise_id: UUID | None = None
    canonical_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category_code: str = Field(min_length=1, max_length=100, description="Use strength or cardio; jump rope and treadmill walking are cardio.")
    unilateral: bool
    status_code: str = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=2000)
    aliases: list[ExerciseAliasInput] = Field(max_length=100)
    muscle_roles: list[ExerciseMuscleRoleInput] = Field(max_length=100)
    measurements: list[ExerciseMeasurementInput] = Field(min_length=1, max_length=100)
    equipment_requirements: list[ExerciseEquipmentRequirementInput] = Field(max_length=100)


class ExerciseCatalogDraftRequest(ClosedModel):
    context: RequestContext
    intent: Literal["create", "correct"]
    exercise: ExerciseCatalogInput
    expected_revision_id: UUID | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1000)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> ExerciseCatalogDraftRequest:
        if self.intent == "create" and (
            self.exercise.exercise_id is not None or self.expected_revision_id is not None
        ):
            raise ValueError("create forbids exercise_id and expected_revision_id")
        if self.intent == "correct" and (
            self.exercise.exercise_id is None or self.expected_revision_id is None
        ):
            raise ValueError("correct requires exercise_id and expected_revision_id")
        if self.reason is not None:
            self.reason = self.reason.strip()
        if self.intent == "correct" and not self.reason:
            raise ValueError("correct requires a non-blank reason")
        return self


class FamiliarExerciseBatchItem(ClosedModel):
    source_name: str = Field(min_length=1, max_length=200)
    exercise_ref: FamiliarExerciseRef | None = None
    new_draft_revision_id: UUID | None = None

    @model_validator(mode="after")
    def one_resolution(self) -> FamiliarExerciseBatchItem:
        if (self.exercise_ref is None) == (self.new_draft_revision_id is None):
            raise ValueError("choose exactly one existing exercise or new exercise draft")
        return self


class FamiliarExerciseBatchDraftRequest(ClosedModel):
    context: RequestContext
    profile_id: UUID
    expected_profile_revision_id: UUID
    items: list[FamiliarExerciseBatchItem] = Field(min_length=1, max_length=100)
    unresolved_names: list[str] = Field(default_factory=list, max_length=100)
    draft_id: UUID | None = None
    expected_revision_id: UUID | None = None
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_revision_target(self) -> FamiliarExerciseBatchDraftRequest:
        if (self.draft_id is None) != (self.expected_revision_id is None):
            raise ValueError("draft_id and expected_revision_id must be supplied together")
        return self


class ExerciseSearchRequest(ClosedModel):
    context: RequestContext
    mode: Literal["search"]
    text: str = Field(min_length=1, max_length=200)
    metric_kinds: list[str] | None = None
    equipment_type: list[str] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


class ExerciseExactRequest(ClosedModel):
    context: RequestContext
    mode: Literal["exact"]
    exercise_id: UUID
    revision_id: UUID | None = None


ExerciseQueryRequest = Annotated[
    ExerciseSearchRequest | ExerciseExactRequest, Field(discriminator="mode")
]


class ProgramIdentityInput(ClosedModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    starts_on: date
    ends_on: date
    status_code: Literal["active"]
    comment: str | None = Field(default=None, min_length=1, max_length=2000)


class ProgramMetricTargetInput(ClosedModel):
    metric_kind: str = Field(min_length=1, max_length=100)
    exact_value: Decimal | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    unit_code: str = Field(min_length=1, max_length=100)


class ProgramTemplateSetInput(ClosedModel):
    position: int = Field(ge=1)
    round_no: int | None = Field(default=None, ge=1)
    logical_set_no: int = Field(ge=1)
    side: Literal["left", "right", "both", "not_applicable"] | None = None
    set_type_code: Literal["warmup", "working"]
    tempo: str | None = Field(default=None, min_length=1, max_length=100)
    pause_seconds: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=3)
    rest_after_seconds: int | None = Field(default=None, ge=0)
    metrics: list[ProgramMetricTargetInput] = Field(min_length=1, max_length=100)


class ProgramTemplateExerciseInput(ClosedModel):
    position: int = Field(ge=1)
    exercise_ref: ObjectRef
    circuit_position: int | None = Field(default=None, ge=1)
    comment: str | None = Field(default=None, min_length=1, max_length=2000)
    sets: list[ProgramTemplateSetInput] = Field(min_length=1, max_length=100)


class ProgramTemplateBlockInput(ClosedModel):
    position: int = Field(ge=1)
    block_type_code: Literal["straight", "superset", "circuit"]
    name: str | None = Field(default=None, min_length=1, max_length=200)
    declared_rounds: int | None = Field(default=None, ge=1)
    rest_seconds: int | None = Field(default=None, ge=0)
    comment: str | None = Field(default=None, min_length=1, max_length=2000)
    exercises: list[ProgramTemplateExerciseInput] = Field(min_length=1, max_length=100)


class ProgramDayTemplateInput(ClosedModel):
    position: int = Field(ge=1)
    day_type_code: Literal["training"]
    purpose: str = Field(min_length=1, max_length=2000)
    blocks: list[ProgramTemplateBlockInput] = Field(min_length=1, max_length=100)


class RecommendationFactLinkInput(ClosedModel):
    ref: ObjectRef
    role: Literal["input", "constraint", "baseline", "context"]
    reason: str = Field(min_length=1, max_length=2000)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecommendationCalculationLinkInput(ClosedModel):
    derived_value_id: UUID
    role: Literal["input", "supporting", "comparison"]


class RecommendationConclusionLinkInput(ClosedModel):
    conclusion_version_id: UUID
    role: Literal["supports", "contradicts", "contextualizes"]


class ProgramRecommendationInput(ClosedModel):
    recommendation_kind: str = Field(min_length=1, max_length=100)
    decision_text: str = Field(min_length=1, max_length=4000)
    confidence: Literal["high", "moderate", "low", "insufficient"]
    revisit_condition: str = Field(min_length=1, max_length=2000)
    fact_links: list[RecommendationFactLinkInput]
    calculation_links: list[RecommendationCalculationLinkInput]
    conclusion_links: list[RecommendationConclusionLinkInput]
    assumptions: dict[str, object]
    alternatives: dict[str, object]
    short_explanation: str = Field(min_length=1, max_length=2000)
    detailed_explanation: str | None = Field(default=None, min_length=1, max_length=8000)


class ProgramVersionDraftInput(ClosedModel):
    version_label: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=4000)
    period_start: date
    period_end: date
    templates: list[ProgramDayTemplateInput] = Field(min_length=1, max_length=100)
    recommendations: list[ProgramRecommendationInput]


class ProgramDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["create", "new_version"]
    program_id: UUID | None = None
    expected_current_version_id: UUID | None = None
    goal_revision_id: UUID
    program: ProgramIdentityInput | None = None
    version: ProgramVersionDraftInput


class ProgramPublicationDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_draft_id: UUID
    source_revision_id: UUID
    expected_current_version_id: UUID | None = None
    publication_reason: str | None = Field(default=None, min_length=1, max_length=2000)


class ProgramDraftReplacement(ClosedModel):
    op: Literal["replace"]
    path: Literal["/goal_revision_id", "/program", "/version"]
    value: UUID | ProgramIdentityInput | ProgramVersionDraftInput

    @model_validator(mode="after")
    def check_target_type(self) -> ProgramDraftReplacement:
        expected = {
            "/goal_revision_id": UUID,
            "/program": ProgramIdentityInput,
            "/version": ProgramVersionDraftInput,
        }[self.path]
        if not isinstance(self.value, expected):
            raise ValueError("replacement value does not match target schema")
        return self


class ProgramDraftRevisionRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision_id: UUID
    changes: list[ProgramDraftReplacement] = Field(min_length=1, max_length=100)


class ProgramCurrentRequest(ClosedModel):
    context: RequestContext
    mode: Literal["current"]
    program_id: UUID


class ProgramExactRequest(ClosedModel):
    context: RequestContext
    mode: Literal["exact"]
    program_id: UUID
    program_version_id: UUID


ProgramQueryRequest = Annotated[
    ProgramCurrentRequest | ProgramExactRequest, Field(discriminator="mode")
]


class ProgramCardView(ProgramIdentityInput):
    program_ref: ObjectRef
    profile_ref: ObjectRef
    goal_id: UUID
    current_version_id: UUID
    description: str | None = Field(min_length=1, max_length=4000)
    comment: str | None = Field(min_length=1, max_length=2000)


class ProgramMetricTargetView(ProgramMetricTargetInput):
    metric_target_id: UUID
    exact_value: Decimal | None
    min_value: Decimal | None
    max_value: Decimal | None


class ProgramTemplateSetView(ClosedModel):
    template_set_id: UUID
    position: int = Field(ge=1)
    round_no: int | None = Field(ge=1)
    logical_set_no: int = Field(ge=1)
    side: Literal["left", "right", "both", "not_applicable"] | None
    set_type_code: Literal["warmup", "working"]
    tempo: str | None = Field(min_length=1, max_length=100)
    pause_seconds: Decimal | None = Field(ge=0)
    rest_after_seconds: int | None = Field(ge=0)
    metrics: list[ProgramMetricTargetView] = Field(min_length=1, max_length=100)


class ProgramTemplateExerciseView(ClosedModel):
    template_exercise_id: UUID
    position: int = Field(ge=1)
    exercise_ref: ObjectRef
    circuit_position: int | None = Field(ge=1)
    comment: str | None = Field(min_length=1, max_length=2000)
    equipment_snapshot: dict[str, object]
    sets: list[ProgramTemplateSetView] = Field(min_length=1, max_length=100)


class ProgramTemplateBlockView(ClosedModel):
    template_block_id: UUID
    position: int = Field(ge=1)
    block_type_code: Literal["straight", "superset", "circuit"]
    name: str | None = Field(min_length=1, max_length=200)
    declared_rounds: int | None = Field(ge=1)
    rest_seconds: int | None = Field(ge=0)
    comment: str | None = Field(min_length=1, max_length=2000)
    exercises: list[ProgramTemplateExerciseView] = Field(min_length=1, max_length=100)


class ProgramDayTemplateView(ClosedModel):
    day_template_id: UUID
    position: int = Field(ge=1)
    day_type_code: Literal["training"]
    purpose: str = Field(min_length=1, max_length=2000)
    equipment_needs: list[dict[str, object]]
    blocks: list[ProgramTemplateBlockView] = Field(min_length=1, max_length=100)


class RecommendationConclusionLinkView(RecommendationConclusionLinkInput):
    applicability_assessment: dict[str, object]
    status_at_decision: Literal["draft", "verified", "outdated", "rejected"]
    status_revision_ref: ObjectRef


class ProgramRecommendationView(ClosedModel):
    recommendation_id: UUID
    recommendation_kind: str = Field(min_length=1, max_length=100)
    decision_text: str = Field(min_length=1, max_length=4000)
    confidence: Literal["high", "moderate", "low", "insufficient"]
    revisit_condition: str = Field(min_length=1, max_length=2000)
    fact_links: list[RecommendationFactLinkInput]
    calculation_links: list[RecommendationCalculationLinkInput]
    conclusion_links: list[RecommendationConclusionLinkView]
    assumptions: dict[str, object]
    alternatives: dict[str, object]
    short_explanation: str = Field(min_length=1, max_length=2000)
    detailed_explanation: str | None = Field(min_length=1, max_length=8000)


class ProgramVersionView(ClosedModel):
    version_ref: ObjectRef
    goal_revision_ref: ObjectRef
    version_no: int = Field(ge=1)
    version_label: str = Field(min_length=1, max_length=200)
    period_start: date
    period_end: date
    rationale: str = Field(min_length=1, max_length=4000)
    publication_state: Literal["published", "retired"]
    published_at: datetime
    supersedes_version_id: UUID | None
    templates: list[ProgramDayTemplateView] = Field(min_length=1, max_length=100)
    recommendations: list[ProgramRecommendationView]
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProgramResponse(ClosedModel):
    program: ProgramCardView
    version: ProgramVersionView
    current: bool


class SavedPlaceOverlay(ClosedModel):
    kind: Literal["saved"]
    place_ref: ObjectRef


class AdHocPlaceOverlay(ClosedModel):
    kind: Literal["ad_hoc"]
    name: str = Field(min_length=1, max_length=200)
    place_type: str = Field(min_length=1, max_length=100)


TemporaryPlaceOverlay = Annotated[
    SavedPlaceOverlay | AdHocPlaceOverlay, Field(discriminator="kind")
]


class TemporaryPlanConditionsInput(ClosedModel):
    place: TemporaryPlaceOverlay | None = None
    equipment_additions: list[PlanningEquipmentTypeCode] = Field(max_length=100)
    equipment_removals: list[PlanningEquipmentTypeCode] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_delta(self) -> TemporaryPlanConditionsInput:
        additions, removals = self.equipment_additions, self.equipment_removals
        if len(additions) != len(set(additions)) or len(removals) != len(set(removals)):
            raise ValueError("equipment overlay entries must be unique")
        if set(additions) & set(removals):
            raise ValueError("equipment additions and removals cannot overlap")
        return self


class LegacyTemporaryPlanConditionsView(ClosedModel):
    """Saved D overlays retain their historical code set."""

    place: TemporaryPlaceOverlay | None = None
    equipment_additions: list[str]
    equipment_removals: list[str]


class PlanMetricInput(ClosedModel):
    metric_kind: str = Field(min_length=1, max_length=100)
    exact_value: Decimal | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    unit_code: str = Field(min_length=1, max_length=100)
    custom_definition: dict[str, object] | None = None


class PlanLoadInput(ClosedModel):
    position: int = Field(ge=1)
    load_kind: Literal["external", "bodyweight", "band"]
    equipment_id: UUID | None = None
    item_count: Decimal | None = Field(default=None, gt=0)
    mass: Decimal | None = Field(default=None, ge=0)
    unit_code: str | None = None
    mass_meaning: str | None = None
    direction: Literal["assistance", "resistance"] | None = None
    raw_ambiguous_text: str | None = None

    @model_validator(mode="after")
    def validate_load_direction(self) -> PlanLoadInput:
        if (self.load_kind == "band") != (self.direction is not None):
            raise ValueError("band requires direction; other load kinds prohibit it")
        return self


class PlanSetInput(ClosedModel):
    position: int = Field(ge=1)
    round_no: int | None = Field(default=None, ge=1)
    logical_set_no: int = Field(ge=1)
    side: Literal["left", "right", "both", "not_applicable"] | None = None
    set_type_code: Literal["warmup", "working"]
    target_rpe_min: Decimal | None = Field(default=None, ge=0, le=10)
    target_rpe_max: Decimal | None = Field(default=None, ge=0, le=10)
    target_rir_min: Decimal | None = Field(default=None, ge=0, le=10)
    target_rir_max: Decimal | None = Field(default=None, ge=0, le=10)
    tempo: str | None = None
    pause_seconds: Decimal | None = Field(default=None, ge=0)
    rest_after_seconds: int | None = Field(default=None, ge=0)
    metrics: list[PlanMetricInput] = Field(min_length=1, max_length=100)
    loads: list[PlanLoadInput] = Field(max_length=100)


class PlanExerciseInput(ClosedModel):
    position: int = Field(ge=1)
    exercise_id: UUID
    circuit_position: int | None = Field(default=None, ge=1)
    comment: str | None = None
    sets: list[PlanSetInput] = Field(min_length=1, max_length=100)


class PlanBlockInput(ClosedModel):
    position: int = Field(ge=1)
    block_type_code: Literal["straight", "superset", "circuit"]
    name: str | None = None
    declared_rounds: int | None = Field(default=None, ge=1)
    rest_seconds: int | None = Field(default=None, ge=0)
    comment: str | None = None
    exercises: list[PlanExerciseInput] = Field(min_length=1, max_length=100)


class PlanTreeInput(ClosedModel):
    planned_date: date
    planned_time: time | None = None
    program_id: UUID | None = None
    program_version_id: UUID | None = None
    day_template_id: UUID | None = None
    plan_type: Literal["training"]
    name: str = Field(min_length=1, max_length=200)
    purpose: str | None = None
    comment: str | None = None
    temporary_conditions: TemporaryPlanConditionsInput
    blocks: list[PlanBlockInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_source_pair(self) -> PlanTreeInput:
        if (self.program_id is None) != (self.program_version_id is None):
            raise ValueError("program and version must be supplied together")
        if self.day_template_id is not None and self.program_version_id is None:
            raise ValueError("day template requires program version")
        return self


class PlanLoadView(ClosedModel):
    planned_load_component_id: UUID
    position: int = Field(ge=1)
    load_kind: Literal["external", "bodyweight", "band"]
    equipment_id: UUID | None
    item_count: Decimal | None = Field(default=None, gt=0)
    mass: Decimal | None = Field(default=None, ge=0)
    unit_code: str | None
    mass_meaning: str | None
    direction: Literal["assistance", "resistance"] | None
    raw_ambiguous_text: str | None
    equipment_snapshot: dict[str, object]

    @model_validator(mode="after")
    def validate_load_direction(self) -> PlanLoadView:
        if (self.load_kind == "band") != (self.direction is not None):
            raise ValueError("band requires direction; other load kinds prohibit it")
        return self


class PlanSetView(ClosedModel):
    planned_set_id: UUID
    position: int = Field(ge=1)
    round_no: int | None
    logical_set_no: int = Field(ge=1)
    side: Literal["left", "right", "both", "not_applicable"] | None
    set_type_code: Literal["warmup", "working"]
    target_rpe_min: Decimal | None = Field(default=None, ge=0, le=10)
    target_rpe_max: Decimal | None = Field(default=None, ge=0, le=10)
    target_rir_min: Decimal | None = Field(default=None, ge=0, le=10)
    target_rir_max: Decimal | None = Field(default=None, ge=0, le=10)
    tempo: str | None
    pause_seconds: Decimal | None = Field(default=None, ge=0)
    rest_after_seconds: int | None = Field(default=None, ge=0)
    metrics: list[PlanMetricInput] = Field(min_length=1, max_length=100)
    loads: list[PlanLoadView] = Field(max_length=100)


class PlanExerciseView(ClosedModel):
    planned_exercise_id: UUID
    position: int = Field(ge=1)
    exercise_id: UUID
    circuit_position: int | None = Field(default=None, ge=1)
    comment: str | None
    equipment_snapshot: dict[str, object]
    sets: list[PlanSetView] = Field(min_length=1, max_length=100)


class PlanBlockView(ClosedModel):
    planned_block_id: UUID
    position: int = Field(ge=1)
    block_type_code: Literal["straight", "superset", "circuit"]
    name: str | None
    declared_rounds: int | None = Field(default=None, ge=1)
    rest_seconds: int | None = Field(default=None, ge=0)
    comment: str | None
    exercises: list[PlanExerciseView] = Field(min_length=1, max_length=100)


class PlanTreeView(ClosedModel):
    planned_date: date
    planned_time: time | None
    program_id: UUID | None
    program_version_id: UUID | None
    day_template_id: UUID | None
    plan_type: Literal["training"]
    name: str = Field(min_length=1, max_length=200)
    purpose: str | None
    comment: str | None
    temporary_conditions: LegacyTemporaryPlanConditionsView
    blocks: list[PlanBlockView] = Field(min_length=1, max_length=100)


class PlanDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: PlanTreeInput
    recommendations: list[ProgramRecommendationInput] = Field(min_length=1, max_length=100)


class ActualMetricValueInput(ClosedModel):
    metric_kind: str = Field(min_length=1, max_length=100)
    value: Decimal
    unit_code: str = Field(min_length=1, max_length=100)
    custom_definition: dict[str, object] | None = None
    source_kind: Literal["user", "system", "curator", "external_metadata"]


class ActualLoadComponentInput(ClosedModel):
    position: int = Field(ge=1)
    planned_load_component_id: UUID | None = None
    load_kind: Literal["external", "bodyweight", "band"]
    equipment_id: UUID | None = None
    item_count: Decimal | None = Field(default=None, gt=0)
    mass: Decimal | None = Field(default=None, ge=0)
    unit_code: str | None = Field(default=None, min_length=1, max_length=100)
    mass_meaning: str | None = Field(default=None, min_length=1, max_length=100)
    direction: Literal["assistance", "resistance"] | None = None
    raw_ambiguous_text: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_load(self) -> ActualLoadComponentInput:
        if (self.load_kind == "band") != (self.direction is not None):
            raise ValueError("band requires direction; other load kinds prohibit it")
        if self.mass is not None and (self.unit_code is None or self.mass_meaning is None):
            raise ValueError("mass requires unit_code and mass_meaning")
        if self.mass is not None and self.raw_ambiguous_text is not None:
            raise ValueError("confirmed mass excludes ambiguous text")
        return self


class WorkoutMetricInput(ClosedModel):
    metric_kind: str = Field(min_length=1, max_length=100)
    value: Decimal
    unit_code: str = Field(min_length=1, max_length=100)
    source_kind: Literal["user", "system", "calculation", "curator", "external_metadata"]
    derived_value_id: UUID | None = None

    @model_validator(mode="after")
    def validate_source(self) -> WorkoutMetricInput:
        if (self.source_kind == "calculation") != (self.derived_value_id is not None):
            raise ValueError("calculation requires exactly one derived_value_id")
        return self


class SkippedExecutionReplacementSource(ClosedModel):
    kind: Literal["skipped_execution"]
    block_position: int = Field(ge=1)
    exercise_position: int = Field(ge=1)


ReplacementSourceInput = Annotated[
    SkippedExecutionReplacementSource,
    Field(discriminator="kind"),
]


class ActualSetInput(ClosedModel):
    position: int = Field(ge=1)
    planned_set_id: UUID | None = None
    round_no: int | None = Field(default=None, ge=1)
    logical_set_no: int = Field(ge=1)
    side: Literal["left", "right", "both", "not_applicable"] | None = None
    set_type_code: Literal["warmup", "working"]
    full_reps: int | None = Field(default=None, ge=0)
    partial_reps: int | None = Field(default=None, ge=0)
    failed_attempts: int | None = Field(default=None, ge=0)
    reported_rpe: Decimal | None = Field(default=None, ge=0, le=10)
    reported_rir: Decimal | None = Field(default=None, ge=0, le=10)
    failure_reached: bool | None = None
    rom_state: str | None = Field(default=None, min_length=1, max_length=100)
    technique_state: str | None = Field(default=None, min_length=1, max_length=100)
    tempo: str | None = Field(default=None, min_length=1, max_length=100)
    pause_seconds: Decimal | None = Field(default=None, ge=0)
    rest_after_seconds: int | None = Field(default=None, ge=0)
    status: Literal["performed", "interrupted", "skipped"]
    metrics: list[ActualMetricValueInput] = Field(max_length=100)
    loads: list[ActualLoadComponentInput] = Field(max_length=100)
    comment: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_status_and_effort(self) -> ActualSetInput:
        if self.reported_rpe is not None and self.reported_rir is not None:
            raise ValueError("reported_rpe and reported_rir are mutually exclusive")
        if self.status == "skipped":
            if self.planned_set_id is None:
                raise ValueError("skipped set requires planned_set_id")
            if (
                any(
                    value is not None
                    for value in (
                        self.full_reps,
                        self.partial_reps,
                        self.failed_attempts,
                        self.reported_rpe,
                        self.reported_rir,
                        self.failure_reached,
                        self.rom_state,
                        self.technique_state,
                        self.tempo,
                        self.pause_seconds,
                        self.rest_after_seconds,
                    )
                )
                or self.metrics
                or self.loads
            ):
                raise ValueError("skipped set carries no actual values, metrics or loads")
        return self


class ActualExerciseInput(ClosedModel):
    position: int = Field(ge=1)
    exercise_id: UUID
    planned_exercise_id: UUID | None = None
    result: Literal["performed", "partial", "skipped", "added"]
    replacement_source: ReplacementSourceInput | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=2000)
    sets: list[ActualSetInput] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_result(self) -> ActualExerciseInput:
        if self.result == "skipped" and self.sets:
            raise ValueError("skipped execution has no child sets")
        if self.replacement_source is not None and self.result != "added":
            raise ValueError("replacement source is only valid for an added execution")
        if self.result == "added" and self.planned_exercise_id is not None:
            raise ValueError("added execution cannot have planned_exercise_id")
        if (
            self.result == "added"
            and self.replacement_source is None
            and self.reason == "replacement"
        ):
            raise ValueError("replacement requires an explicit source")
        return self


class ActualBlockInput(ClosedModel):
    position: int = Field(ge=1)
    planned_block_id: UUID | None = None
    block_type_code: Literal["straight", "superset", "circuit"]
    declared_rounds: int | None = Field(default=None, ge=1)
    rest_seconds: int | None = Field(default=None, ge=0)
    comment: str | None = Field(default=None, min_length=1, max_length=2000)
    exercises: list[ActualExerciseInput] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_positions(self) -> ActualBlockInput:
        exercise_positions = [item.position for item in self.exercises]
        if len(exercise_positions) != len(set(exercise_positions)):
            raise ValueError("exercise positions must be unique within a block")
        for exercise in self.exercises:
            set_positions = [item.position for item in exercise.sets]
            if len(set_positions) != len(set(set_positions)):
                raise ValueError("set positions must be unique within an exercise")
            for actual_set in exercise.sets:
                load_positions = [load.position for load in actual_set.loads]
                if len(load_positions) != len(set(load_positions)):
                    raise ValueError("load positions must be unique within a set")
                metric_kinds = [metric.metric_kind for metric in actual_set.metrics]
                if len(metric_kinds) != len(set(metric_kinds)):
                    raise ValueError("metric kinds must be unique within a set")
        return self


class ConfirmedUnknownMetricInput(ClosedModel):
    block_position: int = Field(ge=1)
    exercise_position: int = Field(ge=1)
    set_position: int = Field(ge=1)
    metric_kind: str = Field(min_length=1, max_length=100)


class WorkoutReportInput(ClosedModel):
    plan_id: UUID | None = None
    plan_revision_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    local_start_date: date
    duration_seconds: int | None = Field(default=None, ge=0)
    session_type: Literal["training"]
    name: str | None = Field(default=None, min_length=1, max_length=200)
    lifecycle: Literal["in_progress", "completed", "interrupted"]
    effort: Decimal | None = Field(default=None, ge=0, le=10)
    comment: str | None = Field(default=None, min_length=1, max_length=2000)
    workout_metrics: list[WorkoutMetricInput] = Field(max_length=100)
    blocks: list[ActualBlockInput] = Field(max_length=100)
    confirmed_unknown_planned_exercise_ids: list[UUID] = Field(max_length=200)
    confirmed_unknown_planned_set_ids: list[UUID] = Field(max_length=500)
    confirmed_unknown_planned_load_component_ids: list[UUID] = Field(max_length=500)
    confirmed_unknown_metrics: list[ConfirmedUnknownMetricInput] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_times_and_plan(self) -> WorkoutReportInput:
        if (self.plan_id is None) != (self.plan_revision_id is None):
            raise ValueError("plan_id and plan_revision_id must be supplied together")
        if self.started_at is not None and self.started_at.tzinfo is None:
            raise ValueError("started_at must include a timezone")
        if self.ended_at is not None and self.ended_at.tzinfo is None:
            raise ValueError("ended_at must include a timezone")
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.ended_at < self.started_at
        ):
            raise ValueError("ended_at precedes started_at")
        block_positions = [item.position for item in self.blocks]
        if len(block_positions) != len(set(block_positions)):
            raise ValueError("block positions must be unique")
        workout_metric_kinds = [item.metric_kind for item in self.workout_metrics]
        if len(workout_metric_kinds) != len(set(workout_metric_kinds)):
            raise ValueError("workout metric kinds must be unique")
        for values, label in (
            (self.confirmed_unknown_planned_exercise_ids, "planned exercise"),
            (self.confirmed_unknown_planned_set_ids, "planned set"),
            (self.confirmed_unknown_planned_load_component_ids, "planned load component"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"confirmed unknown {label} ids must be unique")
        metric_unknown_keys = [
            (item.block_position, item.exercise_position, item.set_position, item.metric_kind)
            for item in self.confirmed_unknown_metrics
        ]
        if len(metric_unknown_keys) != len(set(metric_unknown_keys)):
            raise ValueError("confirmed unknown metric paths must be unique")
        return self


class WorkoutReportDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    report: WorkoutReportInput


class WorkoutDraftReplacement(ClosedModel):
    op: Literal["replace"]
    path: Literal["/report"]
    value: WorkoutReportInput


class WorkoutDraftRevisionRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision_id: UUID
    changes: list[WorkoutDraftReplacement] = Field(min_length=1, max_length=1)


DraftRevisionRequest = ProgramDraftRevisionRequest | WorkoutDraftRevisionRequest


class WorkoutHistoryRequest(ClosedModel):
    context: RequestContext
    mode: Literal["range", "all_confirmed"] = "range"
    date_from: date | None = None
    date_to: date | None = None
    exercise_id: UUID | None = None
    lifecycle: list[Literal["in_progress", "completed", "interrupted"]] = Field(
        default_factory=list
    )
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> WorkoutHistoryRequest:
        if self.mode == "range":
            if self.date_from is None or self.date_to is None:
                raise ValueError("date_from and date_to are required for range mode")
            if self.date_from > self.date_to:
                raise ValueError("date_from must not follow date_to")
            if (self.date_to - self.date_from).days > 366:
                raise ValueError("history range must not exceed 366 days")
        elif self.date_from is not None or self.date_to is not None:
            raise ValueError("date filters are not supported in all_confirmed mode")
        if len(self.lifecycle) != len(set(self.lifecycle)):
            raise ValueError("lifecycle filters must be unique")
        return self


class WorkoutHistoryItem(ClosedModel):
    session_ref: ObjectRef
    current: bool
    supersedes_revision_ref: ObjectRef | None
    local_start_date: date
    lifecycle: Literal["in_progress", "completed", "interrupted"]
    confirmed_at: datetime
    confirmation_ref: ObjectRef
    reason: str | None
    plan_ref: ObjectRef | None
    plan_content_digest: str | None
    workout: dict[str, object]
    provenance: list[dict[str, object]]


class WorkoutHistoryResponse(ClosedModel):
    items: list[WorkoutHistoryItem]
    next_cursor: str | None
    snapshot_id: UUID | None = None
    snapshot_confirmed_at: datetime | None = None
    total_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_snapshot_metadata(self) -> WorkoutHistoryResponse:
        present = (
            self.snapshot_id is not None,
            self.snapshot_confirmed_at is not None,
            self.total_count is not None,
        )
        if any(present) and not all(present):
            raise ValueError("snapshot metadata must be complete")
        if self.total_count is not None and self.total_count < len(self.items):
            raise ValueError("total_count cannot be smaller than the page")
        return self


RecordMetricKind = Literal[
    "record_full_reps",
    "record_external_load",
    "record_hold_duration",
    "record_cardio_time",
]


class RecordMetricValue(ClosedModel):
    exact_value: Decimal = Field(gt=0)
    unit_code: Literal["repetition", "kilogram", "second"]


class RecordLoadConditionInput(ClosedModel):
    load_kind: Literal["external", "bodyweight", "band"]
    direction: Literal["assistance", "resistance"] | None = None
    equipment_type: str | None = Field(default=None, min_length=1, max_length=100)
    equipment_id: UUID | None = None
    item_count: Decimal | None = Field(default=None, gt=0)
    mass_kg: Decimal | None = Field(default=None, ge=0)
    mass_meaning: Literal["total_external_mass", "per_item_mass"] | None = None

    @model_validator(mode="after")
    def validate_load(self) -> RecordLoadConditionInput:
        if (self.load_kind == "band") != (self.direction is not None):
            raise ValueError("band requires direction; other loads prohibit it")
        if self.load_kind == "band" and self.equipment_id is None:
            raise ValueError("band requires exact equipment")
        if self.load_kind == "external" and (
            self.equipment_type is None or self.item_count is None or self.mass_meaning is None
        ):
            raise ValueError("external load requires exact type, count and mass meaning")
        return self


class HistoricalConditionInput(ClosedModel):
    side: Literal["left", "right", "both", "not_applicable"] | None = None
    loads: list[RecordLoadConditionInput] = Field(max_length=20)
    fixed_reps: int | None = Field(default=None, gt=0)
    fixed_distance_m: Decimal | None = Field(default=None, gt=0)
    unknown_fields: list[Literal["load", "side", "reps", "distance"]] = Field(
        default_factory=list, max_length=4
    )

    @model_validator(mode="after")
    def validate_unknowns(self) -> HistoricalConditionInput:
        if len(self.unknown_fields) != len(set(self.unknown_fields)):
            raise ValueError("unknown fields must be unique")
        if self.side is None and "side" not in self.unknown_fields:
            raise ValueError("unknown side must be explicit")
        if self.side is not None and "side" in self.unknown_fields:
            raise ValueError("known side cannot also be marked unknown")
        return self


class RecordLoadView(ClosedModel):
    load_kind: Literal["external", "bodyweight", "band"]
    direction: Literal["assistance", "resistance"] | None
    equipment_type: str | None
    equipment_id: UUID | None
    item_count: Decimal | None = Field(default=None, gt=0)
    mass_kg: Decimal | None = Field(default=None, ge=0)
    mass_meaning: Literal["total_external_mass", "per_item_mass"] | None

    @model_validator(mode="after")
    def validate_view_load(self) -> RecordLoadView:
        if (self.load_kind == "band") != (self.direction is not None):
            raise ValueError("band requires direction; other loads prohibit it")
        if self.load_kind == "band" and self.equipment_id is None:
            raise ValueError("band requires exact equipment")
        if self.load_kind == "external" and (
            self.equipment_type is None or self.item_count is None or self.mass_meaning is None
        ):
            raise ValueError("external load requires exact component identity")
        return self


class RecordConditionView(ClosedModel):
    condition_id: UUID
    rule_version: Literal["record_v1"]
    scope: Literal["comparable", "historical_unknown"]
    metric_kind: RecordMetricKind
    unit_code: Literal["repetition", "kilogram", "second"]
    side: Literal["left", "right", "both", "not_applicable"] | None
    loads: list[RecordLoadView]
    fixed_reps: int | None = Field(default=None, gt=0)
    fixed_distance_m: Decimal | None = Field(default=None, gt=0)
    unknown_fields: list[Literal["load", "side", "reps", "distance"]]
    normalized_key: str

    @model_validator(mode="after")
    def validate_condition_view(self) -> RecordConditionView:
        if len(self.unknown_fields) != len(set(self.unknown_fields)):
            raise ValueError("unknown fields must be unique")
        if (self.scope == "comparable") != (not self.unknown_fields):
            raise ValueError("scope and unknown fields disagree")
        if self.side is None and "side" not in self.unknown_fields:
            raise ValueError("unknown side must be explicit")
        if self.side is not None and "side" in self.unknown_fields:
            raise ValueError("known side cannot also be unknown")
        allowed_unknown = {"load", "side"}
        if self.metric_kind == "record_external_load":
            allowed_unknown.add("reps")
        elif self.metric_kind == "record_cardio_time":
            allowed_unknown.add("distance")
        if set(self.unknown_fields) - allowed_unknown:
            raise ValueError("unknown condition field is not applicable to metric")
        if (self.metric_kind == "record_external_load") != (
            self.fixed_reps is not None or "reps" in self.unknown_fields
        ):
            raise ValueError("external load requires fixed reps or explicit unknown")
        if (self.metric_kind == "record_cardio_time") != (
            self.fixed_distance_m is not None or "distance" in self.unknown_fields
        ):
            raise ValueError("cardio time requires fixed distance or explicit unknown")
        if self.metric_kind in {"record_full_reps", "record_hold_duration"} and (
            self.fixed_reps is not None or self.fixed_distance_m is not None
        ):
            raise ValueError("metric forbids unrelated fixed conditions")
        external = [item for item in self.loads if item.load_kind == "external"]
        if self.metric_kind == "record_external_load":
            if (
                "load" not in self.unknown_fields
                and sum(item.mass_kg is None for item in external) != 1
            ):
                raise ValueError(
                    "external-load condition must omit exactly the metric component mass"
                )
        elif any(item.mass_kg is None for item in external) and "load" not in self.unknown_fields:
            raise ValueError("comparable condition requires exact external mass")
        return self


class HistoricalRecordInput(ClosedModel):
    exercise_id: UUID
    exercise_revision_id: UUID
    metric_kind: RecordMetricKind
    value: Decimal = Field(gt=0)
    unit_code: Literal["repetition", "kilogram", "second"]
    achieved_on: date
    condition: HistoricalConditionInput
    body_mass_kg: Decimal | None = Field(default=None, gt=0)
    historical_import: Literal[True]
    pre_system_assertion: Literal[True]
    technique_self_confirmation: Literal[True]
    reason: str = Field(min_length=1, max_length=2000)
    comment: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_metric_shape(self) -> HistoricalRecordInput:
        expected_unit = {
            "record_full_reps": "repetition",
            "record_external_load": "kilogram",
            "record_hold_duration": "second",
            "record_cardio_time": "second",
        }[self.metric_kind]
        if self.unit_code != expected_unit:
            raise ValueError("record metric has an incompatible unit")
        if (
            self.metric_kind == "record_external_load"
            and self.condition.fixed_reps is None
            and "reps" not in self.condition.unknown_fields
        ):
            raise ValueError("external load requires fixed reps")
        if (
            self.metric_kind == "record_cardio_time"
            and self.condition.fixed_distance_m is None
            and "distance" not in self.condition.unknown_fields
        ):
            raise ValueError("cardio time requires fixed distance")
        if self.metric_kind not in {"record_external_load", "record_cardio_time"} and (
            self.condition.fixed_reps is not None or self.condition.fixed_distance_m is not None
        ):
            raise ValueError("metric forbids unrelated fixed conditions")
        if (
            self.metric_kind in {"record_full_reps", "record_hold_duration"}
            and not self.condition.loads
            and "load" not in self.condition.unknown_fields
        ):
            raise ValueError("metric requires an explicit load mode")
        if (
            self.metric_kind == "record_external_load"
            and not self.condition.loads
            and "load" not in self.condition.unknown_fields
        ):
            raise ValueError("external load requires a load signature")
        external_loads = [item for item in self.condition.loads if item.load_kind == "external"]
        if self.metric_kind == "record_external_load":
            if "load" not in self.condition.unknown_fields and len(external_loads) != 1:
                raise ValueError("external load requires exactly one external component")
            if (
                external_loads
                and external_loads[0].mass_kg is not None
                and external_loads[0].mass_kg != self.value
            ):
                raise ValueError("external load condition mass must match the record value")
        elif "load" not in self.condition.unknown_fields and any(
            item.mass_kg is None for item in external_loads
        ):
            raise ValueError("external load condition requires exact mass")
        return self


class RecordCandidatesRequest(ClosedModel):
    context: RequestContext
    exercise_id: UUID | None = None
    metric_kind: RecordMetricKind | None = None
    status: Literal["pending", "accepted", "rejected", "invalidated"] = "pending"
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)


class RecordDecisionDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: UUID
    decision: Literal["accept", "reject"]
    technique_self_confirmation: bool
    reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_decision(self) -> RecordDecisionDraftRequest:
        if self.decision == "accept" and not self.technique_self_confirmation:
            raise ValueError("accept requires technique self-confirmation")
        return self


class HistoricalRecordCreateDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["create"]
    historical: HistoricalRecordInput


class HistoricalRecordCorrectDraftRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["correct"]
    historical: HistoricalRecordInput
    target_record_id: UUID
    expected_current: bool


HistoricalRecordDraftRequest = Annotated[
    HistoricalRecordCreateDraftRequest | HistoricalRecordCorrectDraftRequest,
    Field(discriminator="intent"),
]


class RecordCandidateItem(ClosedModel):
    candidate_id: UUID
    source_session_ref: ObjectRef
    source_set_ref: ObjectRef
    exercise_ref: ObjectRef
    achieved_on: date
    metric_kind: RecordMetricKind
    candidate_value: RecordMetricValue
    condition: RecordConditionView
    status: Literal["pending", "accepted", "rejected", "invalidated"]
    current_record_ref: ObjectRef | None
    invalidated_reason: Literal["source_corrected", "source_revision_superseded"] | None
    decision_confirmation_ref: ObjectRef | None
    eligible_now: bool


class RecordCandidatesResponse(ClosedModel):
    items: list[RecordCandidateItem]
    next_cursor: str | None


class RecordQueryRequest(ClosedModel):
    context: RequestContext
    exercise_id: UUID | None = None
    metric_kind: RecordMetricKind | None = None
    condition_id: UUID | None = None
    view: Literal["current", "history", "all"]
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)


class RecordResultItem(ClosedModel):
    record_id: UUID
    exercise_ref: ObjectRef
    metric_kind: RecordMetricKind
    value: RecordMetricValue
    achieved_on: date
    condition: RecordConditionView
    is_current: bool
    is_historical_import: bool
    validity: Literal["valid", "source_invalidated", "replaced_by_correction"]
    source_session_ref: ObjectRef | None
    source_set_ref: ObjectRef | None
    source_candidate_ref: ObjectRef | None
    confirmation_ref: ObjectRef
    supersedes_record_ref: ObjectRef | None
    body_mass_kg: Decimal | None
    current_exit_reason: (
        Literal["better_record", "source_corrected", "historical_unknown", "replaced_by_correction"]
        | None
    )
    current_exit_change_id: UUID | None
    comment: str | None

    @model_validator(mode="after")
    def validate_record_source_and_history(self) -> RecordResultItem:
        refs = (self.source_session_ref, self.source_set_ref, self.source_candidate_ref)
        if self.is_historical_import and any(item is not None for item in refs):
            raise ValueError("historical record cannot have workout source refs")
        if not self.is_historical_import and any(item is None for item in refs):
            raise ValueError("workout record requires all exact source refs")
        if self.is_current and (
            self.current_exit_reason is not None or self.current_exit_change_id is not None
        ):
            raise ValueError("current record cannot have current exit details")
        if not self.is_current and (
            self.current_exit_reason is None or self.current_exit_change_id is None
        ):
            raise ValueError("non-current record requires its exit reason and event")
        return self


class RecordResultsResponse(ClosedModel):
    items: list[RecordResultItem]
    next_cursor: str | None


class WorkoutCorrectionPatch(ClosedModel):
    op: Literal["replace"]
    path: Literal["/report"]
    value: WorkoutReportInput


class WorkoutCorrectionRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: ObjectRef
    patch: list[WorkoutCorrectionPatch] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_target_and_patch(self) -> WorkoutCorrectionRequest:
        if self.target.object_type != "workout_session" or self.target.revision_id is None:
            raise ValueError("correction target requires an exact workout_session revision")
        if len(self.patch) != 1 or self.patch[0].path != "/report":
            raise ValueError("workout correction requires one full report replacement")
        return self


class OperationIdLocator(ClosedModel):
    kind: Literal["operation_id"]
    operation_id: UUID


class OperationIdempotencyLocator(ClosedModel):
    kind: Literal["idempotency"]
    method_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=200)


class OperationCorrelationLocator(ClosedModel):
    kind: Literal["correlation"]
    method_id: str = Field(min_length=1, max_length=100)
    correlation_id: UUID


OperationLocator = Annotated[
    OperationIdLocator | OperationIdempotencyLocator | OperationCorrelationLocator,
    Field(discriminator="kind"),
]


class OperationStatusRequest(ClosedModel):
    context: RequestContext
    locator: OperationLocator


class OperationStatusResponse(ClosedModel):
    operation_id: UUID
    state: Literal["received", "executing", "committed", "failed", "recovery_required"]
    commit_result: CommitResult | None
    error: ErrorDetail | None
    updated_at: datetime

    @model_validator(mode="after")
    def validate_terminal_result(self) -> OperationStatusResponse:
        if self.state == "failed" and self.error is None:
            raise ValueError("failed operation requires its safe error")
        if self.state not in {"committed", "failed"} and (self.commit_result or self.error):
            raise ValueError("non-terminal operation cannot include a result or error")
        return self


class PlanningContextRequest(ClosedModel):
    context: RequestContext
    target_date: date
    program_id: UUID | None = None
    temporary_conditions: TemporaryPlanConditionsInput | None = None


class PlanLifecycleRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["cancel", "miss", "reschedule"]
    source_ref: ObjectRef
    expected_current_revision_id: UUID
    reason: str | None = Field(default=None, max_length=2000)
    replacement_plan: PlanTreeInput | None = None
    replacement_recommendations: list[ProgramRecommendationInput] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def validate_lifecycle_shape(self) -> PlanLifecycleRequest:
        reschedule = self.intent == "reschedule"
        if reschedule != (self.replacement_plan is not None):
            raise ValueError("reschedule requires exactly one replacement plan")
        if reschedule != (self.replacement_recommendations is not None):
            raise ValueError("reschedule requires replacement recommendations")
        return self


class PlanQueryRequest(ClosedModel):
    context: RequestContext
    mode: Literal["current", "exact"]
    plan_id: UUID
    revision_id: UUID | None = None

    @model_validator(mode="after")
    def validate_revision(self) -> PlanQueryRequest:
        if (self.mode == "exact") != (self.revision_id is not None):
            raise ValueError("exact requires revision_id; current forbids it")
        return self


class PlanningFact(ClosedModel):
    ref: ObjectRef
    priority: int
    source_kind: Literal["user", "system", "calculation", "curator", "external_metadata"]
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanningContextResponse(ClosedModel):
    facts: list[PlanningFact]
    conflicts: list[Gap]
    gaps: list[Gap]
    eligible_conclusion_version_ids: list[UUID]
    effective_at: datetime
    target_date: date
    timezone: str | None
    effective_equipment_types: list[PlanningEquipmentTypeCode]
    equipment_inventory_snapshot: list[EquipmentContextItem]
    equipment_inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanLifecycleEvent(ClosedModel):
    event_kind: Literal["created", "rescheduled", "cancelled", "missed"]
    revision_ref: ObjectRef
    supersedes_revision_ref: ObjectRef | None
    source_plan_ref: ObjectRef | None
    replacement_plan_ref: ObjectRef | None
    reason: str | None
    confirmation_ref: ObjectRef
    confirmed_at: datetime


class PlanContextSnapshot(ClosedModel):
    profile_ref: ObjectRef
    goal_refs: list[ObjectRef]
    program_version_ref: ObjectRef | None
    day_template_ref: ObjectRef | None
    effective_at: datetime
    timezone: str
    planned_date: date
    planned_time: time | None
    temporary_conditions: LegacyTemporaryPlanConditionsView
    effective_equipment_types: list[str]
    availability_place_id: UUID | None
    equipment_availability_snapshot: dict[str, object]
    equipment_availability_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: list[DependencyRef]


class GlobalPlanContextSnapshot(ClosedModel):
    inventory_snapshot_version: Literal["global_inventory_v2"]
    profile_ref: ObjectRef
    goal_refs: list[ObjectRef]
    program_version_ref: ObjectRef | None
    day_template_ref: ObjectRef | None
    effective_at: datetime
    timezone: str
    planned_date: date
    planned_time: time | None
    temporary_conditions: TemporaryPlanConditionsInput
    effective_equipment_types: list[PlanningEquipmentTypeCode]
    equipment_inventory_snapshot: list[EquipmentContextItem]
    equipment_inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: list[DependencyRef]


class PlanRecommendationFactLink(ClosedModel):
    ref: ObjectRef
    role: Literal["input", "constraint", "baseline", "context"]
    reason: str
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanRecommendationCalculationLink(ClosedModel):
    derived_value_id: UUID
    role: Literal["input", "supporting", "comparison"]


class PlanRecommendationConclusionLink(ClosedModel):
    conclusion_version_id: UUID
    status_revision_id: UUID
    role: Literal["supports", "contradicts", "contextualizes"]
    applicability_assessment: dict[str, object]
    status_at_decision: Literal["draft", "verified", "outdated", "rejected"]


class PlanRecommendation(ClosedModel):
    recommendation_id: UUID
    recommendation_kind: str
    decision_text: str
    confidence: Literal["high", "moderate", "low", "insufficient"]
    assumptions: dict[str, object]
    alternatives: dict[str, object]
    revisit_condition: str
    fact_links: list[PlanRecommendationFactLink]
    calculation_links: list[PlanRecommendationCalculationLink]
    conclusion_links: list[PlanRecommendationConclusionLink]
    short_explanation: str
    detailed_explanation: str | None


class PlanResponse(ClosedModel):
    plan_ref: ObjectRef
    tree: PlanTreeView
    lifecycle: Literal["draft", "planned", "rescheduled", "cancelled", "completed", "missed"]
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_snapshot: PlanContextSnapshot | GlobalPlanContextSnapshot
    context_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    transitions: list[PlanLifecycleEvent]
    recommendation_ids: list[UUID]
    recommendations: list[PlanRecommendation]
    current: bool


class PreviewRequest(ClosedModel):
    context: RequestContext
    revision_id: UUID
    detail_level: Literal["short", "full", "inventory", "details", "exercise", "groups"]
    entry_name: str | None = Field(default=None, min_length=1, max_length=200)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_section_target(self) -> PreviewRequest:
        if (self.detail_level == "exercise") != (self.entry_name is not None):
            raise ValueError("entry_name is required only for exercise detail")
        return self


class PreviewShownRequest(ClosedModel):
    context: RequestContext
    revision_id: UUID
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    shown_message_id: str = Field(min_length=1, max_length=200)
    displayed_text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cross_dialog: bool
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfirmRequest(ClosedModel):
    context: RequestContext
    revision_id: UUID
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_kind: Literal["normal", "publication", "owner_verification"]
    confirmation_message_id: str = Field(min_length=1, max_length=200)
    explicit_intent: str = Field(min_length=1, max_length=1000)
    confirmation_mode: Literal["same_dialog", "cross_dialog"]
    referenced_preview_id: UUID | None
    reshown_message_id: str | None
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_confirmation_shape(self) -> ConfirmRequest:
        if self.confirmation_mode == "same_dialog" and (
            self.referenced_preview_id is not None or self.reshown_message_id is not None
        ):
            raise ValueError("same-dialog confirmation cannot use cross-dialog references")
        if self.confirmation_mode == "cross_dialog" and (
            self.referenced_preview_id is None or self.reshown_message_id is None
        ):
            raise ValueError("cross-dialog confirmation requires explicit preview references")
        return self


class ProfileContextRequest(ClosedModel):
    context: RequestContext
    purpose: str


class PreviewCandidate(ClosedModel):
    summary: str
    confirmation_kind: Literal["normal", "publication", "owner_verification"]
    complete: bool


class DraftResult(ClosedModel):
    draft_id: UUID
    revision_id: UUID
    revision_no: int
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: list[DependencyRef]
    gaps: list[Gap]
    preview_candidate: PreviewCandidate


class PreviewResult(ClosedModel):
    preview_id: UUID
    revision_id: UUID
    conversation_id: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_kind: Literal["normal", "publication", "owner_verification"]
    complete: bool
    canonical_payload: dict[str, object]
    canonical_text: str
    changed_objects: list[ObjectRef]
    dependencies: list[DependencyRef]
    gaps: list[Gap]
    shown_message_id: str | None = None
    shown_at: datetime | None = None
    expires_at: datetime


class ShownResult(ClosedModel):
    preview_id: UUID
    shown_message_id: str
    shown_at: datetime


class CommitChange(ClosedModel):
    object_type: str
    object_id: UUID
    revision_id: UUID | None = None


class CommitResult(ClosedModel):
    commit_result_id: UUID
    confirmation_id: UUID
    operation_id: UUID
    status: Literal["committed", "already_committed"]
    domain_changes: list[ObjectRef]
    created_revisions: list[ObjectRef]
    invalidated_calculations: list[ObjectRef]
    committed_at: datetime


class GoalContextItem(ClosedModel):
    ref: ObjectRef
    goal: GoalInput


class PlaceContextItem(ClosedModel):
    ref: ObjectRef
    place: PlaceInput


class EquipmentContextItem(ClosedModel):
    ref: ObjectRef
    equipment: EquipmentCardView

    @model_validator(mode="after")
    def validate_exact_ref(self) -> EquipmentContextItem:
        if self.ref.object_type != "equipment_item" or self.ref.revision_id is None:
            raise ValueError("equipment requires an exact equipment_item ref")
        return self


class PlaceEquipmentAvailabilityContext(ClosedModel):
    availability_id: UUID
    place_ref: ObjectRef
    equipment_ref: ObjectRef
    valid_from: datetime
    valid_to: datetime | None
    quantity: Decimal | None
    context: str | None


class ProfileContextResponse(ClosedModel):
    profile_ref: ObjectRef | None
    profile: ProfileInput | None
    goals: list[GoalContextItem]
    places: list[PlaceContextItem]
    equipment: list[EquipmentContextItem]
    familiar_exercises: list[FamiliarExerciseView]
    effective_at: datetime
    gaps: list[Gap]


class MuscleGroupRef(ClosedModel):
    muscle_group_id: UUID
    name: str
    normalized_name: str
    status_code: str


class ExerciseMuscleRoleView(ClosedModel):
    muscle_group: MuscleGroupRef
    role: Literal["primary", "secondary", "stabilizer"]


class ExerciseAggregateView(ClosedModel):
    exercise_ref: ObjectRef
    canonical_name: str
    description: str | None
    category_code: str
    unilateral: bool
    status_code: str
    comment: str | None
    aliases: list[ExerciseAliasInput]
    muscle_roles: list[ExerciseMuscleRoleView]
    measurements: list[ExerciseMeasurementInput]
    equipment_requirements: list[ExerciseEquipmentRequirementInput]
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExerciseMatch(ClosedModel):
    exercise_ref: ObjectRef
    name: str
    aliases: list[str]
    category: str
    metric_kinds: list[str]
    equipment_types: list[str]
    rank: Decimal


class ExerciseSearchResponse(ClosedModel):
    items: list[ExerciseMatch]
    next_cursor: str | None


class ExerciseExactResponse(ClosedModel):
    aggregate: ExerciseAggregateView
    current: bool


class DataEnvelope[PayloadT](ClosedModel):
    data: PayloadT
    meta: ResponseMeta
