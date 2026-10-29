"""Closed CMD-11 and QRY-13 wire models."""

from __future__ import annotations

from datetime import date, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from trainer.domain.measurements import HEIGHT, MASS, PAIRED, normalize_session
from trainer.transport.http.contracts import ClosedModel, RequestContext

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
DecimalString = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$")]


class UnknownMethod(ClosedModel):
    mode: Literal["unknown"]


class NewMethod(ClosedModel):
    mode: Literal["new"]
    description: str = Field(min_length=1, max_length=500)


class ReuseMethod(ClosedModel):
    mode: Literal["reuse"]
    source_measurement_value_id: UUID


MethodInput = Annotated[UnknownMethod | NewMethod | ReuseMethod, Field(discriminator="mode")]


class MeasurementValueInput(ClosedModel):
    measurement_kind: MeasurementKind
    original_value: DecimalString
    original_unit: Literal["kilogram", "pound", "centimeter", "millimeter", "inch"]
    side: Literal["left", "right"] | None
    method: MethodInput | None
    comment: str | None = Field(min_length=1, max_length=2000)


class MeasurementSessionInput(ClosedModel):
    measured_on: date
    measured_time: time | None
    conditions: str | None = Field(min_length=1, max_length=2000)
    comment: str | None = Field(min_length=1, max_length=2000)
    values: list[MeasurementValueInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_values(self) -> MeasurementSessionInput:
        normalize_session(self.model_dump(mode="json"))
        return self


class MeasurementCreateRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["create"]
    session: MeasurementSessionInput


class MeasurementCorrectRequest(ClosedModel):
    context: RequestContext
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent: Literal["correct"]
    measurement_root_id: UUID
    expected_current_session_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    session: MeasurementSessionInput


MeasurementDraftRequest = Annotated[
    MeasurementCreateRequest | MeasurementCorrectRequest, Field(discriminator="intent")
]


class _DatedRequest(ClosedModel):
    date_from: date | None
    date_to: date | None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_dates(self) -> _DatedRequest:
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("date range must be both null or both set")
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from after date_to")
        return self


class MeasurementSessionsRequest(_DatedRequest):
    context: RequestContext
    mode: Literal["sessions"]
    view: Literal["current", "history", "all"]
    measurement_kind: MeasurementKind | None
    side: Literal["left", "right"] | None

    @model_validator(mode="after")
    def validate_filter(self) -> MeasurementSessionsRequest:
        if (
            self.side is not None
            and self.measurement_kind is not None
            and self.measurement_kind not in PAIRED
        ):
            raise ValueError("side requires paired measurement kind")
        return self


class MeasurementSeriesRequest(_DatedRequest):
    context: RequestContext
    mode: Literal["series"]
    measurement_kind: MeasurementKind
    side: Literal["left", "right"] | None
    method_source_value_id: UUID | None

    @model_validator(mode="after")
    def validate_filter(self) -> MeasurementSeriesRequest:
        if (self.measurement_kind in PAIRED) != (self.side is not None):
            raise ValueError("side incompatible with measurement kind")
        if (self.measurement_kind not in (MASS, HEIGHT)) != (
            self.method_source_value_id is not None
        ):
            raise ValueError("circumference requires exact method source")
        return self


class MeasurementBmiRequest(ClosedModel):
    context: RequestContext
    mode: Literal["bmi"]


MeasurementQueryRequest = Annotated[
    MeasurementSessionsRequest | MeasurementSeriesRequest | MeasurementBmiRequest,
    Field(discriminator="mode"),
]


class MeasurementValueView(ClosedModel):
    measurement_value_id: UUID
    measurement_kind: MeasurementKind
    original_value: str
    original_unit: str
    normalized_value: str | None
    normalized_unit: str | None
    conversion_version: str | None
    side: Literal["left", "right"] | None
    method_description: str | None
    method_id: UUID | None
    comment: str | None
    normalization_status: Literal["verified_v1", "legacy_unverified"]


class MeasurementSessionView(ClosedModel):
    measurement_root_id: UUID
    session_id: UUID
    supersedes_session_id: UUID | None
    current: bool
    measured_on: date
    measured_time: time | None
    conditions: str | None
    comment: str | None
    confirmation_id: UUID
    values: list[MeasurementValueView]


class SessionsResponse(ClosedModel):
    items: list[MeasurementSessionView]
    next_cursor: str | None


class SeriesPoint(ClosedModel):
    session_id: UUID
    measurement_value_id: UUID
    measured_on: date
    measured_time: time | None
    original_value: str
    original_unit: str
    normalized_value: str
    normalized_unit: str
    conversion_version: Literal["body_units_v1"]
    side: Literal["left", "right"] | None
    method_description: str | None
    method_id: UUID | None


class SeriesResponse(ClosedModel):
    points: list[SeriesPoint]
    next_cursor: str | None


class BmiSourceView(ClosedModel):
    session_id: UUID
    measurement_value_id: UUID
    confirmation_id: UUID
    measured_on: date
    measured_time: time | None
    original_value: str
    original_unit: str
    normalized_value: str
    normalized_unit: str
    conversion_version: Literal["body_units_v1"]


class BMIResponse(ClosedModel):
    value: str
    unit_code: Literal["kg_per_m2"]
    method: Literal["bmi_latest_confirmed_height_v1"]
    body_mass: BmiSourceView
    height: BmiSourceView
