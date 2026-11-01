from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClosedModel(BaseModel):
    """Local MCP wire types; deliberately independent from the API implementation."""

    model_config = ConfigDict(extra="forbid")


class ObjectRef(ClosedModel):
    object_type: str
    object_id: UUID
    revision_id: UUID | None = None


class ProfileInput(ClosedModel):
    birth_date: date | None = None
    sex: Literal["male", "female", "other", "not_stated"] | None = None
    timezone: str = Field(min_length=1, max_length=100)
    started_on: date | None = None
    desired_sessions_per_week: int | None = Field(default=None, ge=1, le=14)
    usual_place_id: UUID | None = None
    comment: str | None = None


class ExerciseRevisionRef(ClosedModel):
    object_type: Literal["exercise"]
    object_id: UUID
    revision_id: UUID


class FamiliarExerciseSelection(ClosedModel):
    exercise_ref: ExerciseRevisionRef


class FamiliarExerciseBatchItem(ClosedModel):
    source_name: str = Field(min_length=1, max_length=200)
    exercise_ref: ExerciseRevisionRef | None = None
    new_draft_revision_id: UUID | None = None

    @model_validator(mode="after")
    def one_resolution(self) -> FamiliarExerciseBatchItem:
        if (self.exercise_ref is None) == (self.new_draft_revision_id is None):
            raise ValueError("choose exactly one existing exercise or new exercise draft")
        return self


class GoalInput(ClosedModel):
    goal_id: UUID | None = None
    parent_goal_id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    goal_type: str = Field(min_length=1, max_length=100, description="Use performance or strength; strength includes a one-repetition maximum goal.")
    start_value: Decimal | None = None
    target_value: Decimal | None = None
    unit_code: str | None = None
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


EquipmentTypeCode = Literal[
    "barbell",
    "weight_plate",
    "bench",
    "pull_up_bar",
    "parallel_bars",
    "resistance_band",
    "hyperextension_bench",
    "push_up_handles",
    "dumbbell",
    "exercise_mat",
    "treadmill",
    "jump_rope",
    "ab_wheel",
    "boxing_reflex_target",
    "hand_gripper",
    "sportswear",
    "training_gloves",
    "wrist_wraps",
]


class EquipmentCharacteristicMap(ClosedModel):
    mass_per_item_kg: str | None
    bar_sleeve_diameter_mm: str | None
    plate_bore_diameter_mm: str | None


class EquipmentCardInput(ClosedModel):
    equipment_type: EquipmentTypeCode
    name: str = Field(min_length=1, max_length=200)
    maker: str | None
    brand: str | None
    model: str | None
    quantity: int | None = Field(default=None, ge=1, le=999999999)
    quantity_unit_code: Literal["piece", "pair", "set"] | None
    condition: Literal["usable", "unusable"] | None
    characteristics: EquipmentCharacteristicMap
    comment: str | None


class ContextEquipmentInput(EquipmentCardInput):
    equipment_id: UUID


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
    metric_kind: str = Field(
        min_length=1, max_length=100,
        description=(
            "Exercise measurement: repetitions, duration, or distance. "
            "External mass belongs to planned/performed set loads."
        ),
    )
    requiredness: Literal["required", "optional"]
    unit_family: str = Field(
        min_length=1, max_length=100,
        description="count for repetitions, time for duration, length for distance",
    )


class ExerciseEquipmentRequirementInput(ClosedModel):
    equipment_type: str = Field(min_length=1, max_length=100)
    optionality: Literal["required", "optional", "alternative"]
    alternative_group: str | None = Field(default=None, min_length=1, max_length=100)


class ExerciseCatalogInput(ClosedModel):
    exercise_id: UUID | None = None
    canonical_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category_code: str = Field(min_length=1, max_length=100)
    unilateral: bool
    status_code: str = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=2000)
    aliases: list[ExerciseAliasInput] = Field(max_length=100)
    muscle_roles: list[ExerciseMuscleRoleInput] = Field(max_length=100)
    measurements: list[ExerciseMeasurementInput] = Field(min_length=1, max_length=100)
    equipment_requirements: list[ExerciseEquipmentRequirementInput] = Field(max_length=100)


PlanningEquipmentTypeCode = Literal[
    "barbell",
    "weight_plate",
    "bench",
    "pull_up_bar",
    "parallel_bars",
    "resistance_band",
    "hyperextension_bench",
    "push_up_handles",
]


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
        if len(set(self.equipment_additions)) != len(self.equipment_additions):
            raise ValueError("equipment additions must be unique")
        if len(set(self.equipment_removals)) != len(self.equipment_removals):
            raise ValueError("equipment removals must be unique")
        if set(self.equipment_additions) & set(self.equipment_removals):
            raise ValueError("equipment additions and removals cannot overlap")
        return self


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
    assumptions: dict[str, Any]
    alternatives: dict[str, Any]
    short_explanation: str = Field(min_length=1, max_length=2000)
    detailed_explanation: str | None = Field(default=None, min_length=1, max_length=8000)


class ProgramVersionDraftInput(ClosedModel):
    version_label: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=4000)
    period_start: date
    period_end: date
    templates: list[ProgramDayTemplateInput] = Field(min_length=1, max_length=100)
    recommendations: list[ProgramRecommendationInput]


class PlanMetricInput(ClosedModel):
    metric_kind: str = Field(min_length=1, max_length=100)
    exact_value: Decimal | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    unit_code: str = Field(min_length=1, max_length=100)
    custom_definition: dict[str, Any] | None = None


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


WorkoutLifecycle = Literal["in_progress", "completed", "interrupted"]
RecordMetricKind = Literal[
    "record_full_reps",
    "record_external_load",
    "record_hold_duration",
    "record_cardio_time",
]
ProgressMetricKind = Literal[
    "record_full_reps",
    "record_external_load",
    "record_hold_duration",
    "record_cardio_time",
    "estimated_1rm",
    "external_working_set_volume",
]
MeasurementKind = Literal[
    "body_mass",
    "height",
    "waist_circumference",
    "chest_circumference",
    "neck_circumference",
    "upper_arm_circumference",
    "forearm_circumference",
    "thigh_circumference",
    "calf_circumference",
]


class WorkoutHistoryInput(ClosedModel):
    mode: Literal["range", "all_confirmed"] = "range"
    date_from: date | None = None
    date_to: date | None = None
    exercise_id: UUID | None = None
    lifecycle: list[WorkoutLifecycle] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> WorkoutHistoryInput:
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


class ProgressQueryInput(ClosedModel):
    metric_kind: ProgressMetricKind
    date_from: date
    date_to: date
    exercise_id: UUID | None = None
    condition_id: UUID | None = None
    include_history: bool = True
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_range_and_condition(self) -> ProgressQueryInput:
        if self.date_to < self.date_from:
            raise ValueError("date_to precedes date_from")
        if self.metric_kind == "external_working_set_volume" and self.condition_id:
            raise ValueError("read-time external volume has no comparison condition")
        return self


class RecordCandidatesInput(ClosedModel):
    exercise_id: UUID | None = None
    metric_kind: RecordMetricKind | None = None
    status: Literal["pending", "accepted", "rejected", "invalidated"] = "pending"
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)


class RecordsInput(ClosedModel):
    exercise_id: UUID | None = None
    metric_kind: RecordMetricKind | None = None
    condition_id: UUID | None = None
    view: Literal["current", "history", "all"]
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)


class RecordDecisionInput(ClosedModel):
    candidate_id: UUID
    decision: Literal["accept", "reject"]
    technique_self_confirmation: bool
    reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_acceptance(self) -> RecordDecisionInput:
        if self.decision == "accept" and not self.technique_self_confirmation:
            raise ValueError("accept requires technique self-confirmation")
        return self


class HistoricalLoadInput(ClosedModel):
    load_kind: Literal["external", "bodyweight", "band"]
    direction: Literal["assistance", "resistance"] | None = None
    equipment_type: str | None = None
    equipment_id: UUID | None = None
    item_count: Decimal | None = Field(default=None, gt=0)
    mass_kg: Decimal | None = Field(default=None, ge=0)
    mass_meaning: Literal["total_external_mass", "per_item_mass"] | None = None

    @model_validator(mode="after")
    def validate_load(self) -> HistoricalLoadInput:
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
    loads: list[HistoricalLoadInput] = Field(max_length=20)
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
        allowed_unknown = {"load", "side"}
        if self.metric_kind == "record_external_load":
            allowed_unknown.add("reps")
        elif self.metric_kind == "record_cardio_time":
            allowed_unknown.add("distance")
        if set(self.condition.unknown_fields) - allowed_unknown:
            raise ValueError("unknown condition field is not applicable to metric")
        if self.metric_kind == "record_external_load":
            if self.condition.fixed_reps is None and "reps" not in self.condition.unknown_fields:
                raise ValueError("external load requires fixed reps")
            if self.condition.fixed_distance_m is not None:
                raise ValueError("external-load metric forbids fixed distance")
            external_loads = [item for item in self.condition.loads if item.load_kind == "external"]
            if "load" not in self.condition.unknown_fields and len(external_loads) != 1:
                raise ValueError("external load requires exactly one external component")
            if (
                external_loads
                and external_loads[0].mass_kg is not None
                and external_loads[0].mass_kg != self.value
            ):
                raise ValueError("external load condition mass must match the record value")
        elif self.metric_kind == "record_cardio_time":
            if (
                self.condition.fixed_distance_m is None
                and "distance" not in self.condition.unknown_fields
            ):
                raise ValueError("cardio time requires fixed distance")
            if self.condition.fixed_reps is not None:
                raise ValueError("cardio-time metric forbids fixed reps")
        elif self.condition.fixed_reps is not None or self.condition.fixed_distance_m is not None:
            raise ValueError("metric forbids unrelated fixed conditions")
        if (
            self.metric_kind in {"record_full_reps", "record_hold_duration"}
            and not self.condition.loads
            and "load" not in self.condition.unknown_fields
        ):
            raise ValueError("metric requires an explicit load mode")
        external_loads = [item for item in self.condition.loads if item.load_kind == "external"]
        if self.metric_kind == "record_external_load":
            if "load" not in self.condition.unknown_fields and len(external_loads) != 1:
                raise ValueError("external load requires exactly one external component")
        elif "load" not in self.condition.unknown_fields and any(
            item.mass_kg is None for item in external_loads
        ):
            raise ValueError("condition requires exact external mass")
        return self


class HistoricalRecordCreateInput(ClosedModel):
    intent: Literal["create"]
    historical: HistoricalRecordInput


class HistoricalRecordCorrectInput(ClosedModel):
    intent: Literal["correct"]
    historical: HistoricalRecordInput
    target_record_id: UUID
    expected_current: bool


HistoricalRecordDraftInput = Annotated[
    HistoricalRecordCreateInput | HistoricalRecordCorrectInput,
    Field(discriminator="intent"),
]


class CalculationActualSetSourceInput(ClosedModel):
    kind: Literal["actual_set"]
    session_id: UUID
    session_revision_id: UUID
    exercise_id: UUID
    exercise_revision_id: UUID
    actual_set_id: UUID


class CalculationExerciseSessionSourceInput(ClosedModel):
    kind: Literal["exercise_session"]
    session_id: UUID
    session_revision_id: UUID
    exercise_id: UUID


CalculationSourceInput = Annotated[
    CalculationActualSetSourceInput | CalculationExerciseSessionSourceInput,
    Field(discriminator="kind"),
]


class CalculationExecutionAttestationInput(ClosedModel):
    source_actual_set_id: UUID
    grip_condition: str = Field(min_length=1, max_length=200)
    bench_pause_condition: Literal["no_pause", "intentional_chest_pause"]
    standard_confirmed: Literal[True]
    spotter_released_before_rep: Literal[True]
    no_assistance_during_rep: Literal[True]


class CalculationInput(ClosedModel):
    method_id: Literal["e1rm_brzycki", "external_working_set_volume"]
    method_version: Literal["1"]
    mode: Literal["ephemeral", "persist"]
    source: CalculationSourceInput
    execution_attestation: CalculationExecutionAttestationInput | None = None
    prior_derived_value_id: UUID | None = None

    @model_validator(mode="after")
    def validate_method_shape(self) -> CalculationInput:
        e1rm = self.method_id == "e1rm_brzycki"
        if e1rm != (self.source.kind == "actual_set"):
            raise ValueError("method and exact source kind do not match")
        if e1rm != (self.execution_attestation is not None):
            raise ValueError("e1RM requires calculation-time execution attestation")
        if (
            isinstance(self.source, CalculationActualSetSourceInput)
            and self.execution_attestation is not None
            and self.execution_attestation.source_actual_set_id != self.source.actual_set_id
        ):
            raise ValueError("attestation must be bound to the selected ActualSet")
        if self.prior_derived_value_id is not None and (self.mode != "persist" or not e1rm):
            raise ValueError("an exact prior calculation is allowed only for persistent e1RM")
        if self.method_id == "external_working_set_volume" and self.mode != "ephemeral":
            raise ValueError("external working-set volume is read-time only")
        return self


class ActualMetricInput(ClosedModel):
    metric_kind: str = Field(min_length=1, max_length=100)
    value: Decimal
    unit_code: str = Field(min_length=1, max_length=100)
    custom_definition: dict[str, object] | None = None
    source_kind: Literal["user", "system", "curator", "external_metadata"]


class ActualLoadInput(ClosedModel):
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
    def validate_load(self) -> ActualLoadInput:
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
    metrics: list[ActualMetricInput] = Field(max_length=100)
    loads: list[ActualLoadInput] = Field(max_length=100)
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
        if len({item.position for item in self.exercises}) != len(self.exercises):
            raise ValueError("exercise positions must be unique within a block")
        for exercise in self.exercises:
            if len({item.position for item in exercise.sets}) != len(exercise.sets):
                raise ValueError("set positions must be unique within an exercise")
            for actual_set in exercise.sets:
                if len({item.position for item in actual_set.loads}) != len(actual_set.loads):
                    raise ValueError("load positions must be unique within a set")
                if len({item.metric_kind for item in actual_set.metrics}) != len(
                    actual_set.metrics
                ):
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
    def validate_report(self) -> WorkoutReportInput:
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
        if len({item.position for item in self.blocks}) != len(self.blocks):
            raise ValueError("block positions must be unique")
        if len({item.metric_kind for item in self.workout_metrics}) != len(self.workout_metrics):
            raise ValueError("workout metric kinds must be unique")
        for values, label in (
            (self.confirmed_unknown_planned_exercise_ids, "planned exercise"),
            (self.confirmed_unknown_planned_set_ids, "planned set"),
            (self.confirmed_unknown_planned_load_component_ids, "planned load component"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"confirmed unknown {label} ids must be unique")
        metric_keys = [
            (item.block_position, item.exercise_position, item.set_position, item.metric_kind)
            for item in self.confirmed_unknown_metrics
        ]
        if len(metric_keys) != len(set(metric_keys)):
            raise ValueError("confirmed unknown metric paths must be unique")
        return self


class WorkoutCorrectionInput(ClosedModel):
    op: Literal["replace"]
    path: Literal["/report"]
    value: WorkoutReportInput


class WorkoutCorrectionDraftInput(ClosedModel):
    target: ObjectRef
    patch: list[WorkoutCorrectionInput] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_target_patch(self) -> WorkoutCorrectionDraftInput:
        if self.target.object_type != "workout_session" or self.target.revision_id is None:
            raise ValueError("correction target requires an exact workout_session revision")
        if len(self.patch) != 1 or self.patch[0].path != "/report":
            raise ValueError("workout correction requires one full report replacement")
        return self


MeasurementDecimalString = Annotated[
    str,
    Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$"),
]


class UnknownMeasurementMethodInput(ClosedModel):
    mode: Literal["unknown"]


class NewMeasurementMethodInput(ClosedModel):
    mode: Literal["new"]
    description: str = Field(min_length=1, max_length=500)


class ReuseMeasurementMethodInput(ClosedModel):
    mode: Literal["reuse"]
    source_measurement_value_id: UUID


MeasurementMethodInput = Annotated[
    UnknownMeasurementMethodInput | NewMeasurementMethodInput | ReuseMeasurementMethodInput,
    Field(discriminator="mode"),
]


class MeasurementValueDraftInput(ClosedModel):
    measurement_kind: MeasurementKind
    original_value: MeasurementDecimalString
    original_unit: Literal["kilogram", "pound", "centimeter", "millimeter", "inch"]
    side: Literal["left", "right"] | None
    method: MeasurementMethodInput | None
    comment: str | None = Field(min_length=1, max_length=2000)


class MeasurementSessionDraftInput(ClosedModel):
    measured_on: date
    measured_time: time | None
    conditions: str | None = Field(min_length=1, max_length=2000)
    comment: str | None = Field(min_length=1, max_length=2000)
    values: list[MeasurementValueDraftInput] = Field(min_length=1, max_length=100)


class MeasurementCreateDraftInput(ClosedModel):
    intent: Literal["create"]
    session: MeasurementSessionDraftInput


class MeasurementCorrectDraftInput(ClosedModel):
    intent: Literal["correct"]
    measurement_root_id: UUID
    expected_current_session_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    session: MeasurementSessionDraftInput


MeasurementDraftInput = Annotated[
    MeasurementCreateDraftInput | MeasurementCorrectDraftInput,
    Field(discriminator="intent"),
]


class _MeasurementDateRange(ClosedModel):
    date_from: date | None
    date_to: date | None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_dates(self) -> _MeasurementDateRange:
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("date range must be both null or both set")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from after date_to")
        return self


class MeasurementSessionsInput(_MeasurementDateRange):
    mode: Literal["sessions"]
    view: Literal["current", "history", "all"]
    measurement_kind: MeasurementKind | None
    side: Literal["left", "right"] | None

    @model_validator(mode="after")
    def validate_filter(self) -> MeasurementSessionsInput:
        paired = {
            "upper_arm_circumference",
            "forearm_circumference",
            "thigh_circumference",
            "calf_circumference",
        }
        if (
            self.side is not None
            and self.measurement_kind is not None
            and self.measurement_kind not in paired
        ):
            raise ValueError("side requires paired measurement kind")
        return self


class MeasurementSeriesInput(_MeasurementDateRange):
    mode: Literal["series"]
    measurement_kind: MeasurementKind
    side: Literal["left", "right"] | None
    method_source_value_id: UUID | None

    @model_validator(mode="after")
    def validate_filter(self) -> MeasurementSeriesInput:
        paired = {
            "upper_arm_circumference",
            "forearm_circumference",
            "thigh_circumference",
            "calf_circumference",
        }
        if (self.measurement_kind in paired) != (self.side is not None):
            raise ValueError("side incompatible with measurement kind")
        if (self.measurement_kind not in {"body_mass", "height"}) != (
            self.method_source_value_id is not None
        ):
            raise ValueError("circumference requires exact method source")
        return self


class MeasurementBmiInput(ClosedModel):
    mode: Literal["bmi"]


MeasurementQueryInput = Annotated[
    MeasurementSessionsInput | MeasurementSeriesInput | MeasurementBmiInput,
    Field(discriminator="mode"),
]
