"""Closed current inventory card schemas, independent of legacy snapshots."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trainer.domain.inventory import validate_card

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


class EquipmentCharacteristicMap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mass_per_item_kg: str | None
    bar_sleeve_diameter_mm: str | None
    plate_bore_diameter_mm: str | None


class EquipmentCardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    equipment_type: EquipmentTypeCode
    name: str = Field(min_length=1, max_length=200)
    maker: str | None = Field(min_length=1, max_length=200)
    brand: str | None = Field(min_length=1, max_length=200)
    model: str | None = Field(min_length=1, max_length=200)
    quantity: Annotated[int, Field(strict=True, ge=1, le=999999999)] | None
    quantity_unit_code: Literal["piece", "pair", "set"] | None
    condition: Literal["usable", "unusable"] | None
    characteristics: EquipmentCharacteristicMap
    comment: str | None = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_full_card(self) -> EquipmentCardInput:
        values = self.model_dump(exclude={"equipment_id"})
        normalized = validate_card(values)
        self.characteristics = EquipmentCharacteristicMap.model_validate(
            normalized["characteristics"]
        )
        return self


class ContextEquipmentInput(EquipmentCardInput):
    equipment_id: UUID


class EquipmentCardView(EquipmentCardInput):
    pass
