from __future__ import annotations

from typing import Any
from uuid import uuid4

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import ContextRepository


def test_current_equipment_dependency_uses_integer_quantity_and_closed_projection() -> None:
    equipment_id, revision_id = uuid4(), uuid4()
    card = {
        "equipment_type": "barbell",
        "name": "Synthetic barbell",
        "maker": None,
        "brand": None,
        "model": None,
        "quantity": 2,
        "quantity_unit_code": "piece",
        "condition": "usable",
        "comment": None,
        "characteristics": {
            "mass_per_item_kg": None,
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": None,
        },
    }
    raw = {
        **card,
        "quantity": 2.0,
        "equipment_id": str(equipment_id),
        "equipment_revision_id": str(revision_id),
        "rating": 5,
        "characteristics": {**card["characteristics"], "seller_claim": "hidden"},
    }

    class Result:
        def scalar_one_or_none(self) -> dict[str, Any]:
            return raw

    class Session:
        def execute(self, *_: Any) -> Result:
            return Result()

    repository = ContextRepository(Session())  # type: ignore[arg-type]
    expected = {
        "ref": {
            "object_type": "equipment_item",
            "object_id": str(equipment_id),
            "revision_id": str(revision_id),
        },
        "equipment": card,
    }
    actual = repository._revision_digest("equipment_item", revision_id)
    assert actual == digest_payload(expected)
    assert actual != digest_payload(raw)
    assert raw["quantity"] == 2.0 and "seller_claim" in raw["characteristics"]
