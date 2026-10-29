"""Closed saved plan formats; historical D snapshots are never reconstructed."""

from __future__ import annotations

from typing import Any

from trainer.domain.context import DomainValidationError, digest_payload
from trainer.domain.inventory import PLANNING_EQUIPMENT_TYPES, inventory_snapshot, validate_card

COMMON_FIELDS = frozenset(
    {
        "profile_ref",
        "goal_refs",
        "program_version_ref",
        "day_template_ref",
        "effective_at",
        "timezone",
        "planned_date",
        "planned_time",
        "temporary_conditions",
        "effective_equipment_types",
        "dependencies",
    }
)
LEGACY_FIELDS = COMMON_FIELDS | {
    "availability_place_id",
    "equipment_availability_snapshot",
    "equipment_availability_digest",
}
GLOBAL_FIELDS = COMMON_FIELDS | {
    "inventory_snapshot_version",
    "equipment_inventory_snapshot",
    "equipment_inventory_digest",
}


def saved_inventory_format(snapshot: dict[str, Any]) -> str:
    if set(snapshot) == LEGACY_FIELDS:
        if (
            digest_payload(snapshot["equipment_availability_snapshot"])
            != snapshot["equipment_availability_digest"]
        ):
            raise DomainValidationError("legacy availability digest is inconsistent")
        return "legacy"
    if (
        set(snapshot) != GLOBAL_FIELDS
        or snapshot.get("inventory_snapshot_version") != "global_inventory_v2"
    ):
        raise DomainValidationError("mixed or unknown saved inventory format")
    items = snapshot["equipment_inventory_snapshot"]
    if not isinstance(items, list):
        raise DomainValidationError("global inventory snapshot is not a list")
    for item in items:
        if set(item) != {"ref", "equipment"}:
            raise DomainValidationError("global inventory item has unexpected fields")
        ref = item["ref"]
        if (
            set(ref) != {"object_type", "object_id", "revision_id"}
            or ref["object_type"] != "equipment_item"
            or ref["revision_id"] is None
        ):
            raise DomainValidationError("global inventory requires exact equipment refs")
        if validate_card(item["equipment"]) != item["equipment"]:
            raise DomainValidationError("global inventory card is not canonical")
    ordered, digest, base = inventory_snapshot(items)
    if ordered != items or digest != snapshot["equipment_inventory_digest"]:
        raise DomainValidationError("global inventory order or digest is inconsistent")
    overlay = snapshot["temporary_conditions"]
    additions, removals = overlay["equipment_additions"], overlay["equipment_removals"]
    if (
        not set(additions + removals) <= set(PLANNING_EQUIPMENT_TYPES)
        or len(additions) != len(set(additions))
        or len(removals) != len(set(removals))
        or set(additions) & set(removals)
    ):
        raise DomainValidationError("global inventory overlay is inconsistent")
    effective = sorted((set(base) | set(additions)) - set(removals))
    if snapshot["effective_equipment_types"] != effective:
        raise DomainValidationError("global inventory effective types are inconsistent")
    return "global_inventory_v2"
