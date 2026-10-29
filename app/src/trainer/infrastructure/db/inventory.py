"""Global inventory writes using the common draft and confirmation transaction."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from trainer.domain.context import DomainValidationError, digest_payload
from trainer.domain.inventory import inventory_snapshot, project_card, validate_card
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError


class InventoryRepository:
    def __init__(self, context: ContextRepository) -> None:
        self.context = context
        self.session = context.session

    def lock_profile(self, profile_id: UUID | str) -> None:
        current = self.session.execute(
            text(
                "SELECT profile_id FROM trainer.profiles WHERE profile_id=:id "
                "AND archived_at IS NULL FOR UPDATE"
            ),
            {"id": profile_id},
        ).scalar_one_or_none()
        if current is None:
            raise ContextRepositoryError("stale_dependency", "inventory profile changed", 409)

    def current(self, profile_id: UUID | str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                "SELECT r.* FROM trainer.equipment_items e "
                "JOIN trainer.equipment_item_revisions r "
                "ON r.equipment_revision_id=e.current_revision_id "
                "WHERE e.profile_id=:profile AND e.archived_at IS NULL "
                "AND e.logically_deleted_at IS NULL ORDER BY e.equipment_id"
            ),
            {"profile": profile_id},
        ).mappings()
        try:
            items = [
                {
                    "ref": self.context._ref(
                        "equipment_item", row["equipment_id"], row["equipment_revision_id"]
                    ),
                    "equipment": project_card(dict(row)),
                }
                for row in rows
            ]
            return inventory_snapshot(items)[0]
        except DomainValidationError as error:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "current inventory is inconsistent", 500
            ) from error

    def create_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self.context._reserve("CMD-09", key, payload["request_digest"])
        if replay is not None:
            self.context._audit_replay(
                operation_id, "CMD-09", correlation_id, causation_id, key, replay
            )
            return replay
        profile = (
            self.session.execute(
                text(
                    "SELECT profile_id,current_revision_id FROM trainer.profiles "
                    "WHERE archived_at IS NULL"
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile is None:
            raise ContextRepositoryError("insufficient_data", "confirmed profile required", 422)
        after = validate_card(payload["equipment"])
        intent = payload["intent"]
        if intent not in ("create", "correct"):
            raise ContextRepositoryError("validation_error", "unknown inventory intent")
        item_id = str(uuid4()) if intent == "create" else payload["equipment_id"]
        before = None
        if intent == "correct":
            candidates = [
                item
                for item in self.current(profile["profile_id"])
                if item["ref"]["object_id"] == str(item_id)
            ]
            if not candidates:
                raise ContextRepositoryError("not_found", "active inventory item not found", 404)
            previous = candidates[0]
            if previous["ref"]["revision_id"] != payload["expected_current_revision_id"]:
                error = ContextRepositoryError(
                    "stale_dependency", "inventory revision changed", 409
                )
                error.required_next_action = "read_current_profile_context"
                raise error
            before = previous
            if not str(payload.get("reason", "")).strip():
                raise ContextRepositoryError("validation_error", "correction reason required")
        typed = {
            "inventory_change": intent,
            "equipment_id": str(item_id),
            "profile_id": str(profile["profile_id"]),
            "before": before,
            "after": after,
            "reason": payload.get("reason"),
            "expected_current_revision_id": payload.get("expected_current_revision_id"),
            "change_plan": [
                {
                    "object_type": "equipment_item",
                    "object_id": str(item_id),
                    "revision_id": str(uuid4()),
                }
            ],
        }
        draft_id, revision_id, now = uuid4(), uuid4(), datetime.now(UTC)
        digest = digest_payload(typed)
        self.session.execute(
            text(
                "INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,"
                "latest_revision_id,expires_at,created_at) "
                "VALUES (:draft,'inventory',:profile,'open',:revision,:expires,:now)"
            ),
            {
                "draft": draft_id,
                "profile": profile["profile_id"],
                "revision": revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text(
                "INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,"
                "typed_payload,content_digest,trust_state,confirmation_kind,created_at) "
                "VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)"
            ),
            {
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(typed),
                "digest": digest,
                "now": now,
            },
        )
        dependencies = [
            self.context._add_dependency(
                revision_id,
                "profile",
                profile["profile_id"],
                profile["current_revision_id"],
                "inventory owner",
                now,
            )
        ]
        if before is not None:
            dependencies.append(
                self.context._add_dependency(
                    revision_id,
                    "equipment_item",
                    UUID(str(item_id)),
                    UUID(before["ref"]["revision_id"]),
                    "correct current card",
                    now,
                )
            )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": dependencies,
            "gaps": [],
            "preview_candidate": {
                "summary": "Полная карточка инвентаря готова к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self.context._finish(operation_id, response)
        self.context._audit(
            operation_id,
            "CMD-09",
            correlation_id,
            causation_id,
            key,
            [{"object_type": "draft", "object_id": str(draft_id), "revision_id": str(revision_id)}],
        )
        return response

    def insert_revision(
        self,
        card: dict[str, Any],
        change: dict[str, Any],
        confirmation: UUID,
        now: datetime,
        revision_no: int = 1,
        supersedes: UUID | None = None,
    ) -> None:
        card = validate_card(card)
        self.session.execute(
            text(
                "INSERT INTO trainer.equipment_item_revisions(equipment_revision_id,equipment_id,"
                "revision_no,supersedes_revision_id,equipment_type,name,maker,brand,model,quantity,"
                "quantity_unit_code,condition,availability,characteristics,comment,confirmation_id,"
                "confirmed_at,created_at) VALUES (:revision,:id,:no,:supersedes,:equipment_type,"
                ":name,:maker,:brand,:model,:quantity,:quantity_unit_code,:condition,NULL,"
                "CAST(:characteristics AS jsonb),:comment,:confirmation,:now,:now)"
            ),
            {
                **card,
                "characteristics": json.dumps(card["characteristics"]),
                "revision": change["revision_id"],
                "id": change["object_id"],
                "no": revision_no,
                "supersedes": supersedes,
                "confirmation": confirmation,
                "now": now,
            },
        )

    def commit(
        self,
        typed: dict[str, Any],
        confirmation: UUID,
        result: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        self.lock_profile(typed["profile_id"])
        change = changes[0]
        item_id = UUID(typed["equipment_id"])
        supersedes, revision_no = None, 1
        if typed["inventory_change"] == "create":
            self.session.execute(
                text(
                    "INSERT INTO trainer.equipment_items(equipment_id,profile_id,"
                    "current_revision_id,created_at) VALUES (:id,:profile,:revision,:now)"
                ),
                {
                    "id": item_id,
                    "profile": typed["profile_id"],
                    "revision": change["revision_id"],
                    "now": now,
                },
            )
        else:
            current = (
                self.session.execute(
                    text(
                        "SELECT r.revision_no,r.equipment_revision_id "
                        "FROM trainer.equipment_items e "
                        "JOIN trainer.equipment_item_revisions r "
                        "ON r.equipment_revision_id=e.current_revision_id "
                        "WHERE e.equipment_id=:id AND e.profile_id=:profile "
                        "AND e.archived_at IS NULL AND e.logically_deleted_at IS NULL "
                        "FOR UPDATE OF e"
                    ),
                    {"id": item_id, "profile": typed["profile_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if (
                current is None
                or str(current["equipment_revision_id"]) != typed["expected_current_revision_id"]
            ):
                raise ContextRepositoryError(
                    "stale_preview", "inventory changed after preview", 409
                )
            supersedes = current["equipment_revision_id"]
            revision_no = current["revision_no"] + 1
        self.insert_revision(typed["after"], change, confirmation, now, revision_no, supersedes)
        if supersedes is not None:
            self.session.execute(
                text(
                    "UPDATE trainer.equipment_items SET current_revision_id=:revision "
                    "WHERE equipment_id=:id"
                ),
                {"id": item_id, "revision": change["revision_id"]},
            )
        after = {"ref": deepcopy(change), "equipment": deepcopy(typed["after"])}
        self.context._insert_change(
            change, result, typed["inventory_change"], typed["before"], after, typed["reason"], now
        )
