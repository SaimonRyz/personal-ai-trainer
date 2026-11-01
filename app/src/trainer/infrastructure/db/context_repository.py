# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from trainer.domain.context import (
    DomainValidationError,
    digest_payload,
    materialize_onboarding_defaults,
    validate_goal,
    validate_onboarding,
)
from trainer.domain.exercise import NORMALIZATION_VERSION, normalize_name, validate_exercise
from trainer.domain.inventory import PLANNING_EQUIPMENT_TYPES, inventory_snapshot, project_card
from trainer.domain.inventory_snapshots import saved_inventory_format
from trainer.domain.plan import validate_plan_draft
from trainer.domain.program import assess_applicability, validate_program_draft
from trainer.domain.records import canonical_condition_key, strictly_improves


class ContextRepositoryError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.required_next_action: str | None = {
            "source_draft_not_open": "prepare_program_draft",
            "source_revision_not_current": "read_current_draft_revision",
            "stale_dependency": "revise_program_draft",
            "publication_already_current": "read_current_program",
        }.get(code)


class ContextRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_inventory_change_draft(self, **kwargs: Any) -> dict[str, Any]:
        from trainer.infrastructure.db.inventory import InventoryRepository

        return InventoryRepository(self).create_draft(**kwargs)

    def create_measurement_session_draft(self, **kwargs: Any) -> dict[str, Any]:
        from trainer.infrastructure.db.measurements import MeasurementRepository

        return MeasurementRepository(self).create_draft(**kwargs)

    def get_body_measurements(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        from trainer.infrastructure.db.measurements import MeasurementRepository

        return MeasurementRepository(self).query(payload=payload)

    @staticmethod
    def _saved_inventory_format(snapshot: dict[str, Any]) -> str:
        try:
            return saved_inventory_format(snapshot)
        except (DomainValidationError, ValueError, TypeError, KeyError) as error:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "saved inventory snapshot is inconsistent", 500
            ) from error

    def _plan_content_digest(
        self, tree: dict[str, Any], recommendation_ids: list[str], snapshot: dict[str, Any]
    ) -> str:
        content = {
            "tree": self._canonical_plan_tree(tree),
            "recommendation_ids": sorted(recommendation_ids),
        }
        if self._saved_inventory_format(snapshot) == "global_inventory_v2":
            content["context_snapshot"] = snapshot
            content["equipment_refs"] = [
                item["ref"] for item in snapshot["equipment_inventory_snapshot"]
            ]
        return digest_payload(content)

    def get_plan(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        query = payload
        where_revision = (
            "AND r.plan_revision_id=:revision"
            if query["mode"] == "exact"
            else "AND r.plan_revision_id=p.current_revision_id"
        )
        row = (
            self.session.execute(
                text(f"""
                SELECT p.plan_id,p.profile_id,p.current_revision_id,p.archived_at,
                       r.plan_revision_id,r.revision_no,r.program_id,r.program_version_id,
                       r.day_template_id,r.planned_date,r.planned_time,r.plan_type,r.name,
                       r.purpose,r.lifecycle,r.context_snapshot,r.context_snapshot_digest,
                       r.content_digest,r.comment,r.supersedes_revision_id,r.confirmation_id,
                       r.created_at
                FROM trainer.workout_plans p
                JOIN trainer.workout_plan_revisions r ON r.plan_id=p.plan_id
                WHERE p.plan_id=:plan {where_revision}
                  AND p.archived_at IS NULL AND p.logically_deleted_at IS NULL
            """),
                {"plan": query["plan_id"], "revision": query.get("revision_id")},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "plan or revision not found", 404)
        current = row["plan_revision_id"] == row["current_revision_id"]
        lifecycle = list(
            self.session.execute(
                text("""
            SELECT r.plan_revision_id,r.revision_no,r.lifecycle,r.supersedes_revision_id,
                   r.confirmation_id,c.confirmed_at,d.reason
            FROM trainer.workout_plan_revisions r
            JOIN trainer.confirmations c ON c.confirmation_id=r.confirmation_id
            LEFT JOIN trainer.domain_changes d ON d.object_type='workout_plan'
              AND d.object_id=r.plan_id AND d.new_revision_id=r.plan_revision_id
            WHERE r.plan_id=:plan ORDER BY r.revision_no,r.created_at
        """),
                {"plan": row["plan_id"]},
            ).mappings()
        )
        reschedule = (
            self.session.execute(
                text("""
            SELECT l.source_plan_id,l.replacement_plan_id,l.reason,l.confirmation_id,l.created_at,
                   replacement.current_revision_id AS replacement_revision_id
            FROM trainer.plan_reschedule_links l
            JOIN trainer.workout_plans replacement ON replacement.plan_id=l.replacement_plan_id
            WHERE l.source_plan_id=:plan OR l.replacement_plan_id=:plan
        """),
                {"plan": row["plan_id"]},
            )
            .mappings()
            .all()
        )
        snapshot = dict(row["context_snapshot"])
        self._saved_inventory_format(snapshot)
        if digest_payload(snapshot) != row["context_snapshot_digest"]:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "plan context snapshot digest is invalid", 500
            )
        transitions = []
        for event in lifecycle:
            source_link = next(
                (link for link in reschedule if link["source_plan_id"] == row["plan_id"]), None
            )
            replacement_link = next(
                (link for link in reschedule if link["replacement_plan_id"] == row["plan_id"]), None
            )
            source_ref = None
            replacement_ref = None
            if source_link:
                replacement_ref = {
                    "object_type": "workout_plan",
                    "object_id": str(source_link["replacement_plan_id"]),
                    "revision_id": str(source_link["replacement_revision_id"]),
                }
            if replacement_link:
                source_current = self.session.execute(
                    text("SELECT current_revision_id FROM trainer.workout_plans WHERE plan_id=:id"),
                    {"id": replacement_link["source_plan_id"]},
                ).scalar_one()
                source_ref = {
                    "object_type": "workout_plan",
                    "object_id": str(replacement_link["source_plan_id"]),
                    "revision_id": str(source_current),
                }
            transitions.append(
                {
                    "event_kind": event["lifecycle"]
                    if event["lifecycle"] in {"rescheduled", "cancelled", "missed"}
                    else "created",
                    "revision_ref": {
                        "object_type": "workout_plan",
                        "object_id": str(row["plan_id"]),
                        "revision_id": str(event["plan_revision_id"]),
                    },
                    "supersedes_revision_ref": {
                        "object_type": "workout_plan",
                        "object_id": str(row["plan_id"]),
                        "revision_id": str(event["supersedes_revision_id"]),
                    }
                    if event["supersedes_revision_id"]
                    else None,
                    "source_plan_ref": source_ref,
                    "replacement_plan_ref": replacement_ref,
                    "reason": source_link["reason"]
                    if source_link and event["lifecycle"] == "rescheduled"
                    else event["reason"],
                    "confirmation_ref": {
                        "object_type": "confirmation",
                        "object_id": str(event["confirmation_id"]),
                        "revision_id": None,
                    },
                    "confirmed_at": event["confirmed_at"].isoformat(),
                }
            )
        tree = self._json_row(
            {
                "planned_date": row["planned_date"].isoformat(),
                "planned_time": row["planned_time"].isoformat() if row["planned_time"] else None,
                "program_id": str(row["program_id"]) if row["program_id"] else None,
                "program_version_id": str(row["program_version_id"])
                if row["program_version_id"]
                else None,
                "day_template_id": str(row["day_template_id"]) if row["day_template_id"] else None,
                "plan_type": row["plan_type"],
                "name": row["name"],
                "purpose": row["purpose"],
                "comment": row["comment"],
                "temporary_conditions": {
                    k: v
                    for k, v in snapshot["temporary_conditions"].items()
                    if k in {"place", "equipment_additions", "equipment_removals"}
                },
                "blocks": self._load_planned_blocks(row["plan_revision_id"], snapshot),
            }
        )
        recommendations = self._load_plan_recommendations(row["plan_revision_id"])
        recommendation_ids = [str(item["recommendation_id"]) for item in recommendations]
        if (
            self._plan_content_digest(tree, recommendation_ids, snapshot) != row["content_digest"]
            and not self._verified_legacy_plan_scale(
                tree=tree,
                recommendation_ids=recommendation_ids,
                snapshot=snapshot,
                stored_digest=row["content_digest"],
                confirmation_id=row["confirmation_id"],
                plan_id=row["plan_id"],
                revision_id=row["plan_revision_id"],
            )
        ):
            raise ContextRepositoryError(
                "inconsistent_domain_state",
                "plan content digest does not match its immutable tree",
                500,
            )
        return {
            "plan_ref": {
                "object_type": "workout_plan",
                "object_id": str(row["plan_id"]),
                "revision_id": str(row["plan_revision_id"]),
            },
            "tree": tree,
            "lifecycle": row["lifecycle"],
            "context_snapshot": snapshot,
            "context_snapshot_digest": row["context_snapshot_digest"],
            "content_digest": row["content_digest"],
            "transitions": transitions,
            "recommendation_ids": recommendation_ids,
            "recommendations": recommendations,
            "current": current,
        }

    def _verified_legacy_plan_scale(
        self,
        *,
        tree: dict[str, Any],
        recommendation_ids: list[str],
        snapshot: dict[str, Any],
        stored_digest: str,
        confirmation_id: UUID,
        plan_id: UUID,
        revision_id: UUID,
    ) -> bool:
        """Allow an old numeric scale only when its confirmed source proves the saved digest."""
        typed = self.session.execute(
            text("""
                SELECT dr.typed_payload FROM trainer.confirmations c
                JOIN trainer.draft_revisions dr ON dr.draft_revision_id=c.draft_revision_id
                WHERE c.confirmation_id=:confirmation
            """),
            {"confirmation": confirmation_id},
        ).scalar_one_or_none()
        if not isinstance(typed, dict) or not isinstance(typed.get("plan"), dict):
            return False
        if str(typed.get("plan_id")) != str(plan_id) or str(typed.get("plan_revision_id")) != str(
            revision_id
        ):
            return False
        original = typed["plan"]
        if self._plan_content_digest(original, recommendation_ids, snapshot) != stored_digest:
            return False
        return self._plan_tree_with_normalized_set_decimals(original) == (
            self._plan_tree_with_normalized_set_decimals(tree)
        )

    @staticmethod
    def _plan_tree_with_normalized_set_decimals(plan: dict[str, Any]) -> dict[str, Any]:
        tree = ContextRepository._canonical_plan_tree(plan)
        for block in tree["blocks"]:
            for exercise in block["exercises"]:
                for planned_set in exercise["sets"]:
                    for key in (
                        "target_rpe_min", "target_rpe_max", "target_rir_min",
                        "target_rir_max", "pause_seconds",
                    ):
                        if planned_set.get(key) is not None:
                            planned_set[key] = ContextRepository._canonical_decimal(planned_set[key])
        return tree

    def get_planning_context(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        effective_at = datetime.fromisoformat(payload["context"]["effective_at"])
        context = self.get_profile_context(effective_at=effective_at)
        empty = {
            "facts": [],
            "conflicts": [],
            "gaps": context["gaps"],
            "eligible_conclusion_version_ids": [],
            "effective_at": effective_at.isoformat(),
            "target_date": payload["target_date"],
            "timezone": None,
            "effective_equipment_types": [],
            "equipment_inventory_snapshot": [],
            "equipment_inventory_digest": digest_payload([]),
        }
        if context["profile"] is None:
            return empty
        facts: list[dict[str, Any]] = []
        profile_ref = context["profile_ref"]
        profile_source = self.session.execute(
            text("SELECT source_kind FROM trainer.profile_revisions WHERE profile_revision_id=:id"),
            {"id": profile_ref["revision_id"]},
        ).scalar_one()
        facts.append({
            "ref": profile_ref,
            "priority": 100,
            "source_kind": profile_source,
            "snapshot_digest": self._planning_fact_digest(profile_ref),
        })
        for goal in context["goals"]:
            source_kind = self.session.execute(
                text("SELECT source_kind FROM trainer.goal_revisions WHERE goal_revision_id=:id"),
                {"id": goal["ref"]["revision_id"]},
            ).scalar_one()
            facts.append(
                {
                    "ref": goal["ref"],
                    "priority": max(1, 60 - int(goal["goal"]["priority"])),
                    "source_kind": source_kind,
                    "snapshot_digest": self._planning_fact_digest(goal["ref"]),
                }
            )
        if payload.get("program_id"):
            program = (
                self.session.execute(
                    text(
                        "SELECT p.profile_id,p.current_version_id FROM trainer.programs p JOIN trainer.program_versions v ON v.program_version_id=p.current_version_id WHERE p.program_id=:id AND p.archived_at IS NULL AND p.logically_deleted_at IS NULL AND v.publication_state='published'"
                    ),
                    {"id": payload["program_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if program is None or str(program["profile_id"]) != str(profile_ref["object_id"]):
                raise ContextRepositoryError(
                    "not_found", "current published program not found for profile", 404
                )
            facts.append(
                {
                    "ref": {
                        "object_type": "program_version",
                        "object_id": str(payload["program_id"]),
                        "revision_id": str(program["current_version_id"]),
                    },
                    "priority": 80,
                    "source_kind": "system",
                    "snapshot_digest": self._planning_fact_digest({
                        "object_type": "program_version",
                        "object_id": str(payload["program_id"]),
                        "revision_id": str(program["current_version_id"]),
                    }),
                }
            )
        overlay = payload.get("temporary_conditions")
        additions: set[str] = set()
        removals: set[str] = set()
        if overlay is not None:
            # Validate catalog references without creating or updating identities.
            additions = set(overlay["equipment_additions"])
            removals = set(overlay["equipment_removals"])
            for code in additions | removals:
                if code not in PLANNING_EQUIPMENT_TYPES:
                    raise ContextRepositoryError(
                        "validation_error", "non-planning equipment type in overlay", 422
                    )
                if (
                    self.session.execute(
                        text(
                            "SELECT 1 FROM trainer.equipment_type_catalog WHERE code=:code AND archived_at IS NULL"
                        ),
                        {"code": code},
                    ).first()
                    is None
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unknown equipment type in temporary conditions", 422
                    )
            place = overlay.get("place")
            if place and place["kind"] == "saved":
                ref = place["place_ref"]
                saved = (
                    self.session.execute(
                        text("""
                    SELECT p.current_revision_id,r.status FROM trainer.training_places p
                    JOIN trainer.training_place_revisions r ON r.place_revision_id=p.current_revision_id
                    WHERE p.place_id=:id AND p.profile_id=:profile AND p.archived_at IS NULL
                      AND p.logically_deleted_at IS NULL
                """),
                        {"id": ref["object_id"], "profile": profile_ref["object_id"]},
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    saved is None
                    or str(saved["current_revision_id"]) != str(ref["revision_id"])
                    or saved["status"] != "active"
                ):
                    raise ContextRepositoryError(
                        "stale_preview", "saved place reference is not current", 409
                    )
            elif place:
                if (
                    self.session.execute(
                        text(
                            "SELECT 1 FROM trainer.place_type_catalog WHERE code=:code AND archived_at IS NULL"
                        ),
                        {"code": place["place_type"]},
                    ).first()
                    is None
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unknown temporary place_type", 422
                    )

        full_inventory, inventory_digest, base_types = inventory_snapshot(context["equipment"])
        effective_types = sorted((set(base_types) | additions) - removals)
        for item in full_inventory:
            facts.append({
                "ref": item["ref"],
                "priority": 40,
                "source_kind": "user",
                "snapshot_digest": self._planning_fact_digest(item["ref"]),
            })
        facts.sort(
            key=lambda item: (
                -item["priority"],
                item["ref"]["object_type"],
                item["ref"]["object_id"],
            )
        )
        applicability_facts = {
            **context["profile"],
            "profile_id": profile_ref["object_id"],
            "goals": [item["goal"] for item in context["goals"]],
            "effective_equipment_types": effective_types,
        }
        eligible_conclusions: list[UUID] = []
        versions = self.session.execute(
            text("""
            SELECT c.current_version_id FROM trainer.conclusions c
            WHERE c.archived_at IS NULL AND c.current_version_id IS NOT NULL
            ORDER BY c.current_version_id
        """)
        ).scalars()
        for version_id in versions:
            snapshot = self._knowledge_snapshot(UUID(str(version_id)), effective_at)
            if snapshot is None:
                continue
            status = snapshot["status"]
            assessment = assess_applicability(snapshot["scopes"], applicability_facts)
            eligible = (
                status["status"] == "verified"
                and snapshot["is_current"]
                and snapshot["version"]["review_due_on"] >= effective_at.date().isoformat()
                and assessment["outcome"] == "applicable"
            )
            if eligible:
                eligible_conclusions.append(UUID(str(version_id)))
        return {
            "facts": facts,
            "conflicts": [],
            "gaps": context["gaps"],
            "eligible_conclusion_version_ids": eligible_conclusions,
            "effective_at": effective_at.isoformat(),
            "target_date": payload["target_date"],
            "timezone": context["profile"]["timezone"],
            "effective_equipment_types": effective_types,
            "equipment_inventory_snapshot": full_inventory,
            "equipment_inventory_digest": inventory_digest,
        }

    def _planning_fact_digest(self, ref: dict[str, Any]) -> str:
        """Digest the exact stored revision used by plan recommendation fact links."""
        tables = {
            "profile": ("profile_revisions", "profile_revision_id", "profile_id"),
            "goal": ("goal_revisions", "goal_revision_id", "goal_id"),
            "equipment_item": ("equipment_item_revisions", "equipment_revision_id", "equipment_id"),
            "program_version": ("program_versions", "program_version_id", "program_id"),
        }
        try:
            table, revision_column, identity_column = tables[ref["object_type"]]
        except KeyError as error:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "unsupported planning fact type", 500
            ) from error
        snapshot = self.session.execute(
            text(
                f"SELECT to_jsonb(r) FROM trainer.{table} r "
                f"WHERE {revision_column}=:revision AND {identity_column}=:object"
            ),
            {"revision": ref["revision_id"], "object": ref["object_id"]},
        ).scalar_one_or_none()
        if snapshot is None:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "planning fact revision is unavailable", 500
            )
        return digest_payload(snapshot)

    @staticmethod
    def _optional_ref(object_type: str, object_id: Any, revision_id: Any) -> dict[str, str] | None:
        if object_id is None or revision_id is None:
            return None
        return {
            "object_type": object_type,
            "object_id": str(object_id),
            "revision_id": str(revision_id),
        }

    def _plan_availability_snapshot(
        self, place_id: str | UUID | None, effective_at: datetime
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        if place_id is not None:
            items = [
                self._json_row(row)
                for row in self.session.execute(
                    text("""
                    SELECT a.availability_id,a.place_id,a.equipment_id,a.valid_from,a.valid_to,a.context,
                           i.profile_id,i.current_revision_id AS equipment_revision_id,
                           r.equipment_type,r.availability,r.condition,r.name,r.characteristics
                    FROM trainer.place_equipment_availability a
                    JOIN trainer.equipment_items i ON i.equipment_id=a.equipment_id
                    JOIN trainer.equipment_item_revisions r ON r.equipment_revision_id=i.current_revision_id
                    WHERE a.place_id=:place AND a.valid_from<=:at AND (a.valid_to IS NULL OR a.valid_to>:at)
                      AND i.archived_at IS NULL AND i.logically_deleted_at IS NULL
                    ORDER BY r.equipment_type,a.availability_id
                """),
                    {"place": place_id, "at": effective_at},
                ).mappings()
            ]
        return {
            "place_id": str(place_id) if place_id is not None else None,
            "effective_at": effective_at.isoformat(),
            "items": items,
        }

    def _load_planned_blocks(
        self, revision_id: UUID, context_snapshot: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        blocks = self.session.execute(
            text(
                "SELECT * FROM trainer.planned_blocks WHERE plan_revision_id=:id ORDER BY position"
            ),
            {"id": revision_id},
        ).mappings()
        for block in blocks:
            exercises_out: list[dict[str, Any]] = []
            exercises = self.session.execute(
                text("""
                SELECT e.*,x.current_revision_id FROM trainer.planned_exercises e
                JOIN trainer.exercises x ON x.exercise_id=e.exercise_id
                WHERE e.planned_block_id=:id ORDER BY e.position
            """),
                {"id": block["planned_block_id"]},
            ).mappings()
            for exercise in exercises:
                sets_out: list[dict[str, Any]] = []
                sets = self.session.execute(
                    text(
                        "SELECT * FROM trainer.planned_sets WHERE planned_exercise_id=:id ORDER BY position"
                    ),
                    {"id": exercise["planned_exercise_id"]},
                ).mappings()
                for planned_set in sets:
                    metrics = [
                        dict(item)
                        for item in self.session.execute(
                            text(
                                "SELECT metric_kind,exact_value,min_value,max_value,unit_code,custom_definition FROM trainer.planned_metric_values WHERE planned_set_id=:id ORDER BY metric_kind"
                            ),
                            {"id": planned_set["planned_set_id"]},
                        ).mappings()
                    ]
                    loads = [
                        dict(item)
                        for item in self.session.execute(
                            text(
                                "SELECT planned_load_component_id,position,load_kind,equipment_id,equipment_snapshot,item_count,mass,unit_code,mass_meaning,direction,raw_ambiguous_text FROM trainer.planned_load_components WHERE planned_set_id=:id ORDER BY position"
                            ),
                            {"id": planned_set["planned_set_id"]},
                        ).mappings()
                    ]
                    for load in loads:
                        equip_id = load.pop("equipment_id")
                        dep = (
                            next(
                                (
                                    d
                                    for d in context_snapshot.get("dependencies", [])
                                    if d["dependency_type"] == "equipment_item"
                                    and d["object_id"] == str(equip_id)
                                ),
                                None,
                            )
                            if equip_id
                            else None
                        )
                        if equip_id and dep is None:
                            raise ContextRepositoryError(
                                "inconsistent_domain_state",
                                "plan snapshot is missing an exact equipment reference",
                                500,
                            )
                        load["equipment_id"] = equip_id
                    sets_out.append(
                        {
                            "planned_set_id": planned_set["planned_set_id"],
                            "position": planned_set["position"],
                            "round_no": planned_set["round_no"],
                            "logical_set_no": planned_set["logical_set_no"],
                            "side": planned_set["side"],
                            "set_type_code": planned_set["set_type"],
                            "target_rpe_min": planned_set["target_rpe_min"],
                            "target_rpe_max": planned_set["target_rpe_max"],
                            "target_rir_min": planned_set["target_rir_min"],
                            "target_rir_max": planned_set["target_rir_max"],
                            "tempo": planned_set["tempo"],
                            "pause_seconds": planned_set["pause_seconds"],
                            "rest_after_seconds": planned_set["rest_after_seconds"],
                            "metrics": metrics,
                            "loads": loads,
                        }
                    )
                dependency = next(
                    (
                        dep
                        for dep in context_snapshot.get("dependencies", [])
                        if dep["dependency_type"] == "exercise"
                        and dep["object_id"] == str(exercise["exercise_id"])
                    ),
                    None,
                )
                if dependency is None:
                    raise ContextRepositoryError(
                        "inconsistent_domain_state",
                        "plan snapshot is missing an exact exercise reference",
                        500,
                    )
                exercises_out.append(
                    {
                        "planned_exercise_id": exercise["planned_exercise_id"],
                        "position": exercise["position"],
                        "exercise_id": exercise["exercise_id"],
                        "circuit_position": exercise["circuit_position"],
                        "equipment_snapshot": exercise["equipment_snapshot"],
                        "comment": exercise["comment"],
                        "sets": sets_out,
                    }
                )
            result.append(
                {
                    "planned_block_id": block["planned_block_id"],
                    "position": block["position"],
                    "block_type_code": block["block_type"],
                    "name": block["name"],
                    "declared_rounds": block["declared_rounds"],
                    "rest_seconds": block["rest_seconds"],
                    "comment": block["comment"],
                    "exercises": exercises_out,
                }
            )
        return result

    def _load_plan_recommendations(self, revision_id: UUID) -> list[dict[str, Any]]:
        # Recommendation links are immutable children of the exact plan revision.
        result = []
        for item in self.session.execute(
            text("""
                SELECT recommendation_id,recommendation_kind,decision_text,confidence,assumptions,
                       alternatives,revisit_condition
                FROM trainer.recommendations WHERE plan_revision_id=:id ORDER BY recommendation_id
            """),
            {"id": revision_id},
        ).mappings():
            recommendation_id = item["recommendation_id"]
            fact_links = [
                {
                    "ref": {
                        "object_type": link["object_type"],
                        "object_id": link["object_id"],
                        "revision_id": link["revision_id"],
                    },
                    "role": link["role"],
                    "reason": link["reason"],
                    "snapshot_digest": link["snapshot_digest"],
                }
                for link in self.session.execute(
                    text("""
                    SELECT object_type,object_id,revision_id,role,reason,snapshot_digest
                    FROM trainer.recommendation_fact_links WHERE recommendation_id=:id
                    ORDER BY object_type,object_id,revision_id,role
                """),
                    {"id": recommendation_id},
                ).mappings()
            ]
            calculation_links = [
                dict(link)
                for link in self.session.execute(
                    text("""
                SELECT derived_value_id,role FROM trainer.recommendation_calculation_links
                WHERE recommendation_id=:id ORDER BY derived_value_id,role
            """),
                    {"id": recommendation_id},
                ).mappings()
            ]
            conclusion_links = [
                dict(link)
                for link in self.session.execute(
                    text("""
                SELECT conclusion_version_id,status_revision_id,role,applicability_assessment,status_at_decision
                FROM trainer.recommendation_conclusion_links
                WHERE recommendation_id=:id ORDER BY conclusion_version_id,role
            """),
                    {"id": recommendation_id},
                ).mappings()
            ]
            explanations = {
                link["detail_level"]: link["content"]
                for link in self.session.execute(
                    text("""
                    SELECT detail_level,content FROM trainer.recommendation_explanations
                    WHERE recommendation_id=:id ORDER BY detail_level
                """),
                    {"id": recommendation_id},
                ).mappings()
            }
            result.append(
                {
                    **dict(item),
                    "fact_links": fact_links,
                    "calculation_links": calculation_links,
                    "conclusion_links": conclusion_links,
                    "short_explanation": explanations.get("short", ""),
                    "detailed_explanation": explanations.get("detailed"),
                }
            )
        return result

    def _prepare_plan_candidate(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve all mutable plan dependencies once for the canonical preview."""
        validate_plan_draft(body)
        profile_id = UUID(body["profile_id"])
        profile = (
            self.session.execute(
                text(
                    "SELECT profile_id,current_revision_id FROM trainer.profiles WHERE profile_id=:id AND archived_at IS NULL"
                ),
                {"id": profile_id},
            )
            .mappings()
            .one_or_none()
        )
        if profile is None:
            raise ContextRepositoryError("not_found", "active profile not found", 404)
        plan = body["plan"]
        overlay = dict(plan["temporary_conditions"])
        plan["temporary_conditions"] = overlay
        for code in set(overlay["equipment_additions"]) | set(overlay["equipment_removals"]):
            if code not in PLANNING_EQUIPMENT_TYPES:
                raise ContextRepositoryError(
                    "validation_error", "non-planning equipment type in overlay", 422
                )
            if (
                self.session.execute(
                    text(
                        "SELECT 1 FROM trainer.equipment_type_catalog WHERE code=:code AND archived_at IS NULL"
                    ),
                    {"code": code},
                ).first()
                is None
            ):
                raise ContextRepositoryError(
                    "validation_error", "unknown equipment type in temporary conditions", 422
                )
        if (
            self.session.execute(
                text(
                    "SELECT 1 FROM trainer.plan_type_catalog WHERE code=:code AND archived_at IS NULL"
                ),
                {"code": plan["plan_type"]},
            ).first()
            is None
        ):
            raise ContextRepositoryError("validation_error", "unknown plan_type", 422)
        place = overlay.get("place")
        if place and place["kind"] == "saved":
            ref = place["place_ref"]
            saved = (
                self.session.execute(
                    text(
                        "SELECT p.current_revision_id,r.status FROM trainer.training_places p JOIN trainer.training_place_revisions r ON r.place_revision_id=p.current_revision_id WHERE p.place_id=:id AND p.profile_id=:profile AND p.archived_at IS NULL AND p.logically_deleted_at IS NULL"
                    ),
                    {"id": ref["object_id"], "profile": profile_id},
                )
                .mappings()
                .one_or_none()
            )
            if (
                saved is None
                or str(saved["current_revision_id"]) != str(ref["revision_id"])
                or saved["status"] != "active"
            ):
                raise ContextRepositoryError(
                    "stale_preview", "saved place reference is not current", 409
                )
        else:
            if (
                place
                and self.session.execute(
                    text(
                        "SELECT 1 FROM trainer.place_type_catalog WHERE code=:code AND archived_at IS NULL"
                    ),
                    {"code": place["place_type"]},
                ).first()
                is None
            ):
                raise ContextRepositoryError(
                    "validation_error", "unknown temporary place_type", 422
                )
        effective_at = datetime.fromisoformat(body["context"]["effective_at"])
        context = self.get_profile_context(effective_at=effective_at)
        full_inventory, inventory_digest, base_types = inventory_snapshot(context["equipment"])
        effective_types = set(base_types)
        effective_types = (effective_types | set(overlay["equipment_additions"])) - set(
            overlay["equipment_removals"]
        )
        body["context_snapshot"] = {
            "inventory_snapshot_version": "global_inventory_v2",
            "profile_ref": {
                "object_type": "profile",
                "object_id": str(profile_id),
                "revision_id": str(profile["current_revision_id"]),
            },
            "goal_refs": [
                {
                    "object_type": "goal",
                    "object_id": g["ref"]["object_id"],
                    "revision_id": g["ref"]["revision_id"],
                }
                for g in context["goals"]
            ],
            "program_version_ref": {
                "object_type": "program_version",
                "object_id": str(plan["program_id"]),
                "revision_id": str(plan["program_version_id"]),
            }
            if plan.get("program_version_id")
            else None,
            "day_template_ref": {
                "object_type": "day_template",
                "object_id": str(plan["day_template_id"]),
                "revision_id": str(plan["program_version_id"]),
            }
            if plan.get("day_template_id")
            else None,
            "effective_at": body["context"]["effective_at"],
            "timezone": context["profile"]["timezone"],
            "planned_date": plan["planned_date"],
            "planned_time": plan.get("planned_time"),
            "temporary_conditions": overlay,
            "effective_equipment_types": sorted(effective_types),
            "equipment_inventory_snapshot": full_inventory,
            "equipment_inventory_digest": inventory_digest,
            "dependencies": [],
        }
        dependencies: list[dict[str, Any]] = []

        def add_dependency(kind: str, object_id: str, revision_id: str, reason: str) -> None:
            digest = self._revision_digest(kind, UUID(revision_id))
            dependencies.append(
                {
                    "dependency_type": kind,
                    "object_id": object_id,
                    "revision_id": revision_id,
                    "digest": digest,
                    "reason": reason,
                }
            )

        add_dependency(
            "profile",
            str(profile_id),
            str(profile["current_revision_id"]),
            "current profile and timezone",
        )
        for goal in context["goals"]:
            add_dependency(
                "goal",
                goal["ref"]["object_id"],
                goal["ref"]["revision_id"],
                "current goal in planning context",
            )
        for item in full_inventory:
            add_dependency(
                "equipment_item",
                item["ref"]["object_id"],
                item["ref"]["revision_id"],
                "exact current global inventory revision",
            )
        if place and place["kind"] == "saved":
            overlay_ref = place["place_ref"]
            add_dependency(
                "training_place",
                str(overlay_ref["object_id"]),
                str(overlay_ref["revision_id"]),
                "exact saved place in temporary plan overlay",
            )
        if plan.get("program_version_id"):
            program = (
                self.session.execute(
                    text(
                        "SELECT profile_id,current_version_id FROM trainer.programs WHERE program_id=:id AND archived_at IS NULL AND logically_deleted_at IS NULL"
                    ),
                    {"id": plan["program_id"]},
                )
                .mappings()
                .one_or_none()
            )
            version = (
                self.session.execute(
                    text(
                        "SELECT publication_state FROM trainer.program_versions WHERE program_id=:program AND program_version_id=:version"
                    ),
                    {"program": plan["program_id"], "version": plan["program_version_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if (
                program is None
                or str(program["profile_id"]) != str(profile_id)
                or str(program["current_version_id"]) != str(plan["program_version_id"])
                or version is None
                or version["publication_state"] != "published"
            ):
                raise ContextRepositoryError(
                    "stale_preview",
                    "program version is not current and published for this profile",
                    409,
                )
            add_dependency(
                "program_version",
                str(plan["program_id"]),
                str(plan["program_version_id"]),
                "exact published program version",
            )
            if (
                plan.get("day_template_id")
                and not self.session.execute(
                    text(
                        "SELECT 1 FROM trainer.day_templates WHERE day_template_id=:id AND program_version_id=:version"
                    ),
                    {"id": plan["day_template_id"], "version": plan["program_version_id"]},
                ).first()
            ):
                raise ContextRepositoryError(
                    "validation_error", "day template does not belong to program version", 422
                )
        if place and place["kind"] == "saved":
            add_dependency(
                "training_place",
                str(place["place_ref"]["object_id"]),
                str(place["place_ref"]["revision_id"]),
                "exact temporary place",
            )
        self._materialize_plan_tree(body, effective_types)
        seen: set[tuple[str, str]] = set()
        for block in plan["blocks"]:
            for exercise in block["exercises"]:
                ref = exercise["exercise_ref"]
                add_dependency(
                    "exercise", ref["object_id"], ref["revision_id"], "exact planned exercise"
                )
                seen.add(("exercise", ref["object_id"]))
                for planned_set in exercise["sets"]:
                    for load in planned_set["loads"]:
                        ref = load.get("equipment_ref")
                        if ref and ("equipment_item", ref["object_id"]) not in seen:
                            add_dependency(
                                "equipment_item",
                                ref["object_id"],
                                ref["revision_id"],
                                "exact equipment item in planned load",
                            )
                            seen.add(("equipment_item", ref["object_id"]))
        self._materialize_plan_recommendations(body, dependencies, effective_types)
        unique = {(d["dependency_type"], d["object_id"], d["revision_id"]): d for d in dependencies}
        dependencies = [unique[key] for key in sorted(unique)]
        body["context_snapshot"]["dependencies"] = dependencies
        body["content_digest"] = self._plan_content_digest(
            plan,
            [r["recommendation_id"] for r in body["recommendations"]],
            body["context_snapshot"],
        )
        return dependencies

    def create_plan_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-05", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-05", correlation_id, causation_id, key, replay)
            return replay
        body = {k: v for k, v in payload.items() if k != "request_digest"}
        profile = self.session.execute(
            text("SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL")
        ).scalar_one_or_none()
        if profile is None:
            raise ContextRepositoryError("not_found", "active profile not found", 404)
        body.update(profile_id=str(profile), plan_id=str(uuid4()), plan_revision_id=str(uuid4()))
        dependencies = self._prepare_plan_candidate(body)
        profile_id = UUID(str(profile))
        change_plan = [
            {
                "object_type": "workout_plan",
                "object_id": body["plan_id"],
                "revision_id": body["plan_revision_id"],
            }
        ]
        body["change_plan"] = change_plan
        revision_id = UUID(body["plan_revision_id"])
        draft_id = uuid4()
        now = datetime.now(UTC)
        digest = digest_payload(body)
        self.session.execute(
            text(
                "INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at) VALUES (:draft,'plan',:profile,'open',:revision,:expires,:now)"
            ),
            {
                "draft": draft_id,
                "profile": profile_id,
                "revision": revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text(
                "INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,content_digest,trust_state,confirmation_kind,created_at) VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)"
            ),
            {
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(body),
                "digest": digest,
                "now": now,
            },
        )
        for dep in dependencies:
            self.session.execute(
                text(
                    "INSERT INTO trainer.draft_dependencies(draft_dependency_id,draft_revision_id,dependency_type,dependency_object_id,dependency_revision_id,dependency_digest,reason,created_at) VALUES (:id,:revision,:type,:object,:dep_revision,:digest,:reason,:now)"
                ),
                {
                    "id": uuid4(),
                    "revision": revision_id,
                    "type": dep["dependency_type"],
                    "object": UUID(dep["object_id"]),
                    "dep_revision": UUID(dep["revision_id"]),
                    "digest": dep["digest"],
                    "reason": dep["reason"],
                    "now": now,
                },
            )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": dependencies,
            "gaps": [],
            "preview_candidate": {
                "summary": "План готов к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self._finish(operation_id, response)
        self._audit(operation_id, "CMD-05", correlation_id, causation_id, key, change_plan)
        return response

    def create_workout_report_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        from trainer.infrastructure.db.workouts import WorkoutRepository

        return WorkoutRepository(self).create_draft(
            payload=payload,
            key=key,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def get_workout_history(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("mode", "range") == "all_confirmed":
            from trainer.infrastructure.db.workout_history_snapshots import (
                SNAPSHOT_REGISTRY,
                WorkoutHistorySnapshotRepository,
            )

            return WorkoutHistorySnapshotRepository(self.session, SNAPSHOT_REGISTRY).query(payload)

        from base64 import urlsafe_b64decode
        from binascii import Error as Base64DecodeError

        from trainer.infrastructure.db.workouts import WorkoutRepository

        query = payload
        if query.get("cursor"):
            try:
                decoded = urlsafe_b64decode(query["cursor"] + "=" * (-len(query["cursor"]) % 4))
                cursor = json.loads(decoded)
                if (
                    not isinstance(cursor, list)
                    or len(cursor) != 3
                    or not isinstance(cursor[0], str)
                    or not isinstance(cursor[1], str)
                    or type(cursor[2]) is not int
                ):
                    raise ValueError("history cursor has an invalid shape")
                cursor_date, cursor_id, cursor_revision_no = cursor
                UUID(cursor_id)
                date.fromisoformat(cursor_date)
            except (Base64DecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise ContextRepositoryError(
                    "validation_error", "history cursor is invalid", 400
                ) from error
        else:
            cursor_date = cursor_id = cursor_revision_no = None

        filters = [
            "s.archived_at IS NULL",
            "s.logically_deleted_at IS NULL",
            "r.local_start_date BETWEEN :date_from AND :date_to",
        ]
        params: dict[str, Any] = {
            "date_from": query["date_from"],
            "date_to": query["date_to"],
            "limit": query["limit"] + 1,
        }
        if query.get("lifecycle"):
            filters.append("r.lifecycle = ANY(CAST(:lifecycles AS text[]))")
            params["lifecycles"] = query["lifecycle"]
        if query.get("exercise_id"):
            filters.append("""EXISTS (
                SELECT 1 FROM trainer.actual_blocks b
                JOIN trainer.exercise_executions e USING(actual_block_id)
                WHERE b.session_revision_id=r.session_revision_id AND e.exercise_id=:exercise
            )""")
            params["exercise"] = query["exercise_id"]
        if cursor_date is not None:
            filters.append(
                "(r.local_start_date, s.session_id, r.revision_no) < (:cursor_date, :cursor_id, :cursor_revision_no)"
            )
            params.update(
                cursor_date=cursor_date, cursor_id=cursor_id, cursor_revision_no=cursor_revision_no
            )
        rows = (
            self.session.execute(
                text(f"""
            SELECT s.session_id,s.profile_id,s.current_revision_id,r.session_revision_id,r.revision_no,
                   r.supersedes_revision_id,r.plan_revision_id,r.local_start_date,r.lifecycle,
                   r.confirmation_id,c.confirmed_at,dc.reason,dr.content_digest,
                   dc.after_snapshot,
                   p.plan_id,pr.content_digest AS plan_content_digest
            FROM trainer.workout_sessions s
            JOIN trainer.workout_session_revisions r ON r.session_id=s.session_id
            JOIN trainer.confirmations c ON c.confirmation_id=r.confirmation_id
            JOIN trainer.draft_revisions dr ON dr.draft_revision_id=c.draft_revision_id
            JOIN trainer.drafts d ON d.draft_id=dr.draft_id AND d.owner_profile_id=s.profile_id
            LEFT JOIN trainer.domain_changes dc ON dc.object_type='workout_session'
              AND dc.object_id=s.session_id AND dc.new_revision_id=r.session_revision_id
            LEFT JOIN trainer.workout_plans p ON p.plan_id=s.plan_id
            LEFT JOIN trainer.workout_plan_revisions pr ON pr.plan_revision_id=r.plan_revision_id
            WHERE {" AND ".join(filters)}
              AND s.profile_id=(SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL)
            ORDER BY r.local_start_date DESC,s.session_id DESC,r.revision_no DESC
            LIMIT :limit
        """),
                params,
            )
            .mappings()
            .all()
        )
        more = len(rows) > query["limit"]
        rows = rows[: query["limit"]]
        repo = WorkoutRepository(self)
        items = []
        for row in rows:
            revision_id = UUID(str(row["session_revision_id"]))
            session_id = UUID(str(row["session_id"]))
            tree = repo._load_saved_tree(session_id, revision_id)
            tree = self._json_row(tree)
            if row["after_snapshot"] is None or self._json_row(row["after_snapshot"]) != tree:
                raise ContextRepositoryError(
                    "inconsistent_domain_state",
                    "workout history snapshot differs from its confirmed tree",
                    500,
                )
            provenance = self._workout_revision_provenance(revision_id)
            items.append(
                {
                    "session_ref": self._ref("workout_session", session_id, revision_id),
                    "current": revision_id == row["current_revision_id"],
                    "supersedes_revision_ref": self._ref(
                        "workout_session", session_id, UUID(str(row["supersedes_revision_id"]))
                    )
                    if row["supersedes_revision_id"]
                    else None,
                    "local_start_date": row["local_start_date"].isoformat(),
                    "lifecycle": row["lifecycle"],
                    "confirmed_at": row["confirmed_at"].isoformat(),
                    "confirmation_ref": {
                        "object_type": "confirmation",
                        "object_id": str(row["confirmation_id"]),
                        "revision_id": None,
                    },
                    "reason": row["reason"],
                    "plan_ref": self._ref("workout_plan", row["plan_id"], row["plan_revision_id"])
                    if row["plan_id"] is not None
                    else None,
                    "plan_content_digest": row["plan_content_digest"],
                    "workout": tree,
                    "provenance": provenance,
                }
            )
        next_cursor = None
        if more and rows:
            last = rows[-1]
            token = json.dumps(
                [
                    last["local_start_date"].isoformat(),
                    str(last["session_id"]),
                    int(last["revision_no"]),
                ]
            )
            from base64 import urlsafe_b64encode

            next_cursor = urlsafe_b64encode(token.encode()).decode().rstrip("=")
        return {"items": items, "next_cursor": next_cursor}

    def query_record_candidates(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        from trainer.infrastructure.db.workouts import WorkoutRepository

        return WorkoutRepository(self).query_record_candidates(payload)

    def query_records(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        from trainer.infrastructure.db.workouts import WorkoutRepository

        return WorkoutRepository(self).query_records(payload)

    def create_record_decision_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-08", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-08", correlation_id, causation_id, key, replay)
            return replay
        candidate = (
            self.session.execute(
                text("""
            SELECT c.*,cc.readable_snapshot,cc.normalized_key,r.local_start_date,r.session_id,
                   e.exercise_revision_id,s.status AS set_status,s.partial_reps
            FROM trainer.record_candidates c
            JOIN trainer.comparison_conditions cc USING(condition_id)
            JOIN trainer.workout_session_revisions r USING(session_revision_id)
            JOIN trainer.actual_sets s USING(actual_set_id)
            JOIN trainer.exercise_executions e USING(execution_id)
            WHERE c.candidate_id=:id
              AND c.profile_id=(SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL)
            FOR UPDATE OF c
        """),
                {"id": payload["candidate_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if candidate is None:
            raise ContextRepositoryError("not_found", "record candidate not found", 404)
        if candidate["status"] != "pending":
            raise ContextRepositoryError(
                "candidate_not_eligible", "record candidate is no longer pending", 409
            )
        if payload["decision"] == "accept" and (
            candidate["set_status"] != "performed"
            or (candidate["partial_reps"] is not None and candidate["partial_reps"] > 0)
        ):
            raise ContextRepositoryError(
                "candidate_not_eligible", "record source is not eligible", 409
            )
        current = (
            self.session.execute(
                text("""
            SELECT r.record_id,r.value,r.condition_id,c.rule_version,
                   coalesce(dc.after_snapshot->>'validity','valid') AS validity
            FROM trainer.actual_records r JOIN trainer.comparison_conditions c USING(condition_id)
            LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
              WHERE d.object_type='actual_record' AND d.object_id=r.record_id
              ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
            WHERE r.profile_id=:profile AND r.exercise_id=:exercise AND r.metric_kind=:metric
              AND r.condition_id=:condition AND r.is_current FOR UPDATE OF r
        """),
                {
                    "profile": candidate["profile_id"],
                    "exercise": candidate["exercise_id"],
                    "metric": candidate["metric_kind"],
                    "condition": candidate["condition_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        if (
            payload["decision"] == "accept"
            and current is not None
            and not strictly_improves(
                candidate["metric_kind"],
                Decimal(str(candidate["value"])),
                Decimal(str(current["value"])),
            )
        ):
            raise ContextRepositoryError(
                "record_not_improved", "record candidate no longer improves current", 409
            )
        record_id = uuid4() if payload["decision"] == "accept" else None
        typed = {
            "intent": "record_decision",
            "profile_id": str(candidate["profile_id"]),
            "decision": payload["decision"],
            "technique_self_confirmation": payload["technique_self_confirmation"],
            "reason": payload.get("reason"),
            "candidate_id": str(candidate["candidate_id"]),
            "record_id": str(record_id) if record_id else None,
            "candidate_snapshot": {
                "profile_id": str(candidate["profile_id"]),
                "exercise_id": str(candidate["exercise_id"]),
                "exercise_revision_id": str(candidate["exercise_revision_id"]),
                "metric_kind": candidate["metric_kind"],
                "value": str(candidate["value"]),
                "unit_code": candidate["unit_code"],
                "condition_id": str(candidate["condition_id"]),
                "session_id": str(candidate["session_id"]),
                "session_revision_id": str(candidate["session_revision_id"]),
                "actual_set_id": str(candidate["actual_set_id"]),
                "achieved_on": candidate["local_start_date"].isoformat(),
                "condition": dict(candidate["readable_snapshot"]),
                "partial_reps": candidate["partial_reps"],
            },
            "current_snapshot": {
                "record_id": str(current["record_id"]),
                "value": str(current["value"]),
                "condition_id": str(current["condition_id"]),
                "rule_version": current["rule_version"],
                "validity": current["validity"],
            }
            if current
            else None,
            "change_plan": [
                {
                    "object_type": "record_candidate",
                    "object_id": str(candidate["candidate_id"]),
                    "revision_id": None,
                },
                *(
                    [
                        {
                            "object_type": "actual_record",
                            "object_id": str(current["record_id"]),
                            "revision_id": None,
                        }
                    ]
                    if current
                    else []
                ),
                *(
                    [
                        {
                            "object_type": "actual_record",
                            "object_id": str(record_id),
                            "revision_id": None,
                        }
                    ]
                    if record_id
                    else []
                ),
            ],
            "gaps": [],
        }
        return self._create_record_draft(
            typed=typed,
            intent_type="record_decision",
            operation_id=operation_id,
            method="CMD-08",
            payload=payload,
            key=key,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def create_historical_record_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-24", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-24", correlation_id, causation_id, key, replay)
            return replay
        historical = payload["historical"]
        profile_id = self.session.execute(
            text("SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL")
        ).scalar_one_or_none()
        if profile_id is None:
            raise ContextRepositoryError("not_found", "active profile not found", 404)
        exercise_revision = (
            self.session.execute(
                text("""
            SELECT exercise_id,exercise_revision_id FROM trainer.exercise_revisions
            WHERE exercise_id=:exercise AND exercise_revision_id=:revision
        """),
                {
                    "exercise": historical["exercise_id"],
                    "revision": historical["exercise_revision_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        if exercise_revision is None:
            raise ContextRepositoryError(
                "validation_error", "exact exercise revision not found", 422
            )
        if historical["achieved_on"] > date.today().isoformat():
            raise ContextRepositoryError(
                "validation_error", "historical date cannot be in the future", 422
            )
        target = None
        if payload["intent"] == "correct":
            target = (
                self.session.execute(
                    text("""
                SELECT * FROM trainer.actual_records WHERE record_id=:id AND profile_id=:profile
                  AND is_historical_import FOR UPDATE
            """),
                    {"id": payload["target_record_id"], "profile": profile_id},
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise ContextRepositoryError("not_found", "historical record not found", 404)
            if bool(target["is_current"]) != payload["expected_current"]:
                raise ContextRepositoryError(
                    "stale_dependency", "historical record state changed", 409
                )
        target_before = None
        if target is not None:
            target_condition = self.session.execute(
                text(
                    "SELECT readable_snapshot FROM trainer.comparison_conditions WHERE condition_id=:id"
                ),
                {"id": target["condition_id"]},
            ).scalar_one()
            target_before = {
                "record_id": str(target["record_id"]),
                "exercise_id": str(target["exercise_id"]),
                "exercise_revision_id": str(target["exercise_revision_id"]),
                "metric_kind": target["metric_kind"],
                "value": str(target["value"]),
                "unit_code": target["unit_code"],
                "achieved_on": target["achieved_on"].isoformat(),
                "condition_id": str(target["condition_id"]),
                "condition": dict(target_condition),
                "body_mass_kg": str(target["body_mass"])
                if target["body_mass"] is not None
                else None,
                "comment": target["comment"],
                "is_current": bool(target["is_current"]),
            }
        record_id = uuid4()
        condition_input = historical["condition"]
        unknown = condition_input.get("unknown_fields", [])
        if unknown:
            key_object: dict[str, Any] = {
                "rule_version": "record_v1",
                "scope": "historical_unknown",
                "import_id": str(record_id),
            }
            readable = {
                **condition_input,
                "scope": "historical_unknown",
                "rule_version": "record_v1",
                "metric_kind": historical["metric_kind"],
                "unit_code": historical["unit_code"],
            }
        else:
            load_signature = [dict(item) for item in condition_input["loads"]]
            if historical["metric_kind"] == "record_external_load" and "load" not in unknown:
                external_components = [
                    item for item in load_signature if item["load_kind"] == "external"
                ]
                if len(external_components) != 1:
                    raise ContextRepositoryError(
                        "invalid_historical_import",
                        "external-load history requires one external component",
                        422,
                    )
                external_components[0]["mass_kg"] = None
            key_object = {
                "metric_kind": historical["metric_kind"],
                "unit_code": historical["unit_code"],
                "side": condition_input.get("side"),
                "load_signature": load_signature,
                "fixed_reps": condition_input.get("fixed_reps"),
                "fixed_distance_m": condition_input.get("fixed_distance_m"),
                "rule_version": "record_v1",
            }
            readable = {**key_object, "scope": "comparable", "unknown_fields": []}
        normalized_key = canonical_condition_key(key_object)
        # Read an existing comparable group for an exact preview snapshot. A
        # new ComparisonCondition remains a CMD-00 write.
        condition_id = self.session.execute(
            text("""SELECT condition_id FROM trainer.comparison_conditions
                   WHERE exercise_id=:exercise AND rule_version='record_v1'
                     AND normalized_key=:key"""),
            {"exercise": historical["exercise_id"], "key": normalized_key},
        ).scalar_one_or_none()
        current_rows: list[Any]
        if condition_id is None:
            # A key absent from the table cannot have a current record.
            current_rows = []
        else:
            current_rows = list(
                self.session.execute(
                    text("""
                    SELECT r.record_id,r.value,r.condition_id,c.rule_version,
                           coalesce(dc.after_snapshot->>'validity','valid') AS validity
                    FROM trainer.actual_records r JOIN trainer.comparison_conditions c USING(condition_id)
                    LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                      WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                      ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                    WHERE r.profile_id=:profile AND r.exercise_id=:exercise AND r.metric_kind=:metric
                      AND r.condition_id=:condition AND r.is_current ORDER BY r.record_id
                """),
                    {
                        "profile": profile_id,
                        "exercise": historical["exercise_id"],
                        "metric": historical["metric_kind"],
                        "condition": condition_id,
                    },
                )
                .mappings()
                .all()
            )
        target_current_snapshot: list[dict[str, str]] = []
        target_fallback_records: list[dict[str, Any]] = []
        if target is not None:
            target_rows = (
                self.session.execute(
                    text("""
                SELECT r.record_id,r.value,r.condition_id,c.rule_version,
                       coalesce(dc.after_snapshot->>'validity','valid') AS validity
                FROM trainer.actual_records r JOIN trainer.comparison_conditions c USING(condition_id)
                LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                  WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                  ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                WHERE r.profile_id=:profile AND r.exercise_id=:exercise AND r.metric_kind=:metric
                  AND r.condition_id=:condition AND r.is_current ORDER BY r.record_id FOR UPDATE OF r
            """),
                    {
                        "profile": profile_id,
                        "exercise": target["exercise_id"],
                        "metric": target["metric_kind"],
                        "condition": target["condition_id"],
                    },
                )
                .mappings()
                .all()
            )
            target_current_snapshot = [
                {
                    "record_id": str(item["record_id"]),
                    "value": str(item["value"]),
                    "condition_id": str(item["condition_id"]),
                    "rule_version": item["rule_version"],
                    "validity": item["validity"],
                }
                for item in target_rows
            ]
            target_fallback_records = [
                {
                    "record_id": str(item["record_id"]),
                    "value": str(item["value"]),
                    "achieved_on": item["achieved_on"].isoformat(),
                    "created_at": item["created_at"].isoformat(),
                    "is_current": bool(item["is_current"]),
                    "validity": item["validity"],
                }
                for item in self.session.execute(
                    text("""
                    SELECT r.record_id,r.value,r.achieved_on,r.created_at,r.is_current,
                      coalesce(dc.after_snapshot->>'validity','valid') AS validity
                    FROM trainer.actual_records r
                    LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                      WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                      ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                    WHERE r.profile_id=:profile AND r.exercise_id=:exercise AND r.metric_kind=:metric
                      AND r.condition_id=:condition AND NOT r.is_current
                      AND coalesce(dc.after_snapshot->>'validity','valid')='valid'
                    ORDER BY r.achieved_on,r.created_at,r.record_id
                """),
                    {
                        "profile": profile_id,
                        "exercise": target["exercise_id"],
                        "metric": target["metric_kind"],
                        "condition": target["condition_id"],
                    },
                ).mappings()
            ]
        typed = {
            "intent": "historical_record",
            "profile_id": str(profile_id),
            "intent_mode": payload["intent"],
            "historical": historical,
            "record_id": str(record_id),
            "condition_id": str(condition_id) if condition_id else None,
            "condition_key": normalized_key,
            "condition_readable": readable,
            "supersedes_record_id": str(target["record_id"]) if target else None,
            "expected_current": bool(target["is_current"]) if target else None,
            "target_current_snapshot": target_current_snapshot,
            "target_fallback_records": target_fallback_records,
            "target_before": target_before,
            "current_snapshot": [
                {
                    "record_id": str(item["record_id"]),
                    "value": str(item["value"]),
                    "condition_id": str(item["condition_id"]),
                    "rule_version": item["rule_version"],
                    "validity": item["validity"],
                }
                for item in current_rows
            ],
            "change_plan": [
                {"object_type": "actual_record", "object_id": str(record_id), "revision_id": None}
            ],
            "gaps": [],
        }
        if target:
            typed["change_plan"].append(
                {
                    "object_type": "actual_record",
                    "object_id": str(target["record_id"]),
                    "revision_id": None,
                }
            )
            typed["change_plan"].extend(
                {
                    "object_type": "actual_record",
                    "object_id": item["record_id"],
                    "revision_id": None,
                }
                for item in target_fallback_records
            )
        if not unknown and (
            not current_rows
            or strictly_improves(
                historical["metric_kind"],
                Decimal(str(historical["value"])),
                Decimal(str(current_rows[0]["value"])),
            )
        ):
            typed["change_plan"].extend(
                {
                    "object_type": "actual_record",
                    "object_id": str(item["record_id"]),
                    "revision_id": None,
                }
                for item in current_rows
            )
        return self._create_record_draft(
            typed=typed,
            intent_type="historical_record",
            operation_id=operation_id,
            method="CMD-24",
            payload=payload,
            key=key,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def _create_record_draft(
        self,
        *,
        typed: dict[str, Any],
        intent_type: str,
        operation_id: UUID,
        method: str,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        draft_id, revision_id = uuid4(), uuid4()
        change_refs: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        for item in typed["change_plan"]:
            identity = (item["object_type"], str(item["object_id"]), item.get("revision_id"))
            change_refs[identity] = item
        typed["change_plan"] = list(change_refs.values())
        typed.update(draft_id=str(draft_id), revision_id=str(revision_id), revision_no=1)
        digest = digest_payload(typed)
        self.session.execute(
            text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at)
            VALUES (:draft,:intent,:profile,'open',:revision,:expires,:now)
        """),
            {
                "draft": draft_id,
                "intent": intent_type,
                "profile": typed["profile_id"],
                "revision": revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text("""
            INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,
              content_digest,trust_state,confirmation_kind,created_at)
            VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)
        """),
            {
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(typed),
                "digest": digest,
                "now": now,
            },
        )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": [],
            "gaps": [],
            "preview_candidate": {
                "summary": "Решение по рекорду готово к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self._finish(operation_id, response)
        self._audit(operation_id, method, correlation_id, causation_id, key, typed["change_plan"])
        return response

    def _commit_record_intent(
        self,
        typed: dict[str, Any],
        confirmation_id: UUID,
        result_id: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        change_by_id = {str(item["object_id"]): item for item in changes}
        if typed["intent"] == "record_decision":
            candidate_id = UUID(typed["candidate_id"])
            candidate = (
                self.session.execute(
                    text("""
                SELECT c.*,cc.normalized_key,r.local_start_date,r.session_id,s.status AS set_status,
                       s.partial_reps,s.full_reps
                FROM trainer.record_candidates c
                JOIN trainer.comparison_conditions cc USING(condition_id)
                JOIN trainer.workout_session_revisions r USING(session_revision_id)
                JOIN trainer.workout_sessions ws ON ws.session_id=r.session_id
                JOIN trainer.actual_sets s USING(actual_set_id)
                WHERE c.candidate_id=:id AND c.profile_id=:profile FOR UPDATE OF c,ws
            """),
                    {"id": candidate_id, "profile": typed["profile_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if candidate is None or candidate["status"] != "pending":
                raise ContextRepositoryError(
                    "candidate_not_eligible", "record candidate is not pending", 409
                )
            if candidate["session_id"] is None:
                raise ContextRepositoryError(
                    "candidate_not_eligible", "record source is unavailable", 409
                )
            current_revision = self.session.execute(
                text(
                    "SELECT current_revision_id FROM trainer.workout_sessions WHERE session_id=:id"
                ),
                {"id": candidate["session_id"]},
            ).scalar_one()
            if current_revision != candidate["session_revision_id"]:
                raise ContextRepositoryError(
                    "stale_dependency", "record source revision changed", 409
                )
            group = ":".join(
                (
                    str(candidate["profile_id"]),
                    str(candidate["exercise_id"]),
                    candidate["metric_kind"],
                    candidate["normalized_key"],
                )
            )
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": group}
            )
            current = (
                self.session.execute(
                    text("""
                SELECT r.*,c.rule_version,coalesce(dc.after_snapshot->>'validity','valid') AS current_validity
                FROM trainer.actual_records r JOIN trainer.comparison_conditions c USING(condition_id)
                LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                  WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                  ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                WHERE r.profile_id=:profile AND r.exercise_id=:exercise
                  AND r.metric_kind=:metric AND r.condition_id=:condition AND r.is_current FOR UPDATE OF r
            """),
                    {
                        "profile": candidate["profile_id"],
                        "exercise": candidate["exercise_id"],
                        "metric": candidate["metric_kind"],
                        "condition": candidate["condition_id"],
                    },
                )
                .mappings()
                .one_or_none()
            )
            expected = typed["current_snapshot"]
            actual_snapshot = (
                {
                    "record_id": str(current["record_id"]),
                    "value": str(current["value"]),
                    "condition_id": str(current["condition_id"]),
                    "rule_version": current["rule_version"],
                    "validity": current["current_validity"],
                }
                if current
                else None
            )
            if actual_snapshot != expected:
                raise ContextRepositoryError(
                    "stale_dependency", "current record changed after preview", 409
                )
            before_candidate = {"status": candidate["status"], "candidate_id": str(candidate_id)}
            if typed["decision"] == "reject":
                self.session.execute(
                    text("""
                    UPDATE trainer.record_candidates SET status='rejected',decision_confirmation_id=:confirmation
                    WHERE candidate_id=:id AND status='pending'
                """),
                    {"confirmation": confirmation_id, "id": candidate_id},
                )
                self._insert_change(
                    change_by_id[str(candidate_id)],
                    result_id,
                    "correct",
                    before_candidate,
                    {"status": "rejected", "candidate_id": str(candidate_id)},
                    typed.get("reason"),
                    now,
                )
                return
            if (
                not typed["technique_self_confirmation"]
                or candidate["set_status"] != "performed"
                or (candidate["partial_reps"] is not None and candidate["partial_reps"] > 0)
            ):
                raise ContextRepositoryError(
                    "candidate_not_eligible",
                    "accept requires a complete performed set and self-confirmation",
                    409,
                )
            if current is not None and not strictly_improves(
                candidate["metric_kind"],
                Decimal(str(candidate["value"])),
                Decimal(str(current["value"])),
            ):
                raise ContextRepositoryError(
                    "record_not_improved", "candidate no longer improves current", 409
                )
            record_id = UUID(typed["record_id"])
            if current is not None:
                self.session.execute(
                    text("UPDATE trainer.actual_records SET is_current=false WHERE record_id=:id"),
                    {"id": current["record_id"]},
                )
                old_change = change_by_id[str(current["record_id"])]
                self._insert_change(
                    old_change,
                    result_id,
                    "correct",
                    dict(current),
                    {
                        **dict(current),
                        "is_current": False,
                        "current_exit_reason": "better_record",
                        "current_record_id": str(record_id),
                    },
                    None,
                    now,
                )
            value = Decimal(str(candidate["value"]))
            metric = candidate["metric_kind"]
            condition = self.session.execute(
                text(
                    "SELECT readable_snapshot FROM trainer.comparison_conditions WHERE condition_id=:id"
                ),
                {"id": candidate["condition_id"]},
            ).scalar_one()
            condition = dict(condition)
            record = {
                "record_id": str(record_id),
                "profile_id": str(candidate["profile_id"]),
                "exercise_id": str(candidate["exercise_id"]),
                "exercise_revision_id": str(candidate["exercise_revision_id"]),
                "metric_kind": metric,
                "value": str(value),
                "unit_code": candidate["unit_code"],
                "condition_id": str(candidate["condition_id"]),
                "condition": condition,
                "session_id": str(candidate["session_id"]),
                "session_revision_id": str(candidate["session_revision_id"]),
                "actual_set_id": str(candidate["actual_set_id"]),
                "candidate_id": str(candidate_id),
            }
            from trainer.infrastructure.db.workouts import WorkoutRepository

            source_fingerprint = WorkoutRepository(self).source_fingerprint_for_set(
                UUID(str(candidate["actual_set_id"]))
            )
            self.session.execute(
                text("""
                INSERT INTO trainer.actual_records(record_id,profile_id,exercise_id,metric_kind,value,unit_code,
                  achieved_on,condition_id,session_id,session_revision_id,actual_set_id,weight_value,weight_unit,
                  reps,duration_seconds,body_mass,comment,is_historical_import,supersedes_record_id,is_current,
                  confirmation_id,created_at,exercise_revision_id,candidate_id,source_fingerprint)
                VALUES (:id,:profile,:exercise,:metric,:value,:unit,:date,:condition,:session,:revision,:set,
                  :weight,:weight_unit,:reps,:duration,NULL,NULL,false,:supersedes,true,:confirmation,:now,
                  :exercise_revision,:candidate,:fingerprint)
            """),
                {
                    "id": record_id,
                    "profile": candidate["profile_id"],
                    "exercise": candidate["exercise_id"],
                    "metric": metric,
                    "value": value,
                    "unit": candidate["unit_code"],
                    "date": candidate["local_start_date"],
                    "condition": candidate["condition_id"],
                    "session": candidate["session_id"],
                    "revision": candidate["session_revision_id"],
                    "set": candidate["actual_set_id"],
                    "weight": value if metric == "record_external_load" else None,
                    "weight_unit": "kilogram" if metric == "record_external_load" else None,
                    "reps": candidate["full_reps"] if metric == "record_external_load" else None,
                    "duration": int(value)
                    if metric in ("record_hold_duration", "record_cardio_time")
                    else None,
                    "supersedes": current["record_id"] if current else None,
                    "confirmation": confirmation_id,
                    "now": now,
                    "exercise_revision": candidate["exercise_revision_id"],
                    "candidate": candidate_id,
                    "fingerprint": source_fingerprint,
                },
            )
            self.session.execute(
                text("""
                UPDATE trainer.record_candidates SET status='accepted',decision_confirmation_id=:confirmation
                WHERE candidate_id=:id AND status='pending'
            """),
                {"confirmation": confirmation_id, "id": candidate_id},
            )
            self._insert_change(
                change_by_id[str(candidate_id)],
                result_id,
                "correct",
                before_candidate,
                {
                    "status": "accepted",
                    "candidate_id": str(candidate_id),
                    "actual_record_id": str(record_id),
                },
                None,
                now,
            )
            self._insert_change(
                change_by_id[str(record_id)],
                result_id,
                "create",
                None,
                {**record, "is_current": True, "validity": "valid"},
                None,
                now,
            )
            return

        self._commit_historical_record(typed, confirmation_id, result_id, change_by_id, now)

    def _commit_historical_record(
        self,
        typed: dict[str, Any],
        confirmation_id: UUID,
        result_id: UUID,
        change_by_id: dict[str, dict[str, Any]],
        now: datetime,
    ) -> None:
        historical = typed["historical"]
        profile_id = UUID(typed["profile_id"])
        exercise_id = UUID(historical["exercise_id"])
        condition_id = UUID(typed["condition_id"]) if typed.get("condition_id") else None
        unknown = bool(historical["condition"].get("unknown_fields"))
        target = None
        group_keys = [
            (str(profile_id), str(exercise_id), historical["metric_kind"], typed["condition_key"])
        ]
        if typed.get("supersedes_record_id"):
            target = (
                self.session.execute(
                    text("SELECT * FROM trainer.actual_records WHERE record_id=:id FOR UPDATE"),
                    {"id": typed["supersedes_record_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if (
                target is None
                or not target["is_historical_import"]
                or bool(target["is_current"]) != typed["expected_current"]
            ):
                raise ContextRepositoryError(
                    "stale_dependency", "historical correction target changed", 409
                )
            target_condition_key = self.session.execute(
                text(
                    "SELECT normalized_key FROM trainer.comparison_conditions WHERE condition_id=:id"
                ),
                {"id": target["condition_id"]},
            ).scalar_one()
            group_keys.append(
                (
                    str(profile_id),
                    str(target["exercise_id"]),
                    target["metric_kind"],
                    target_condition_key,
                )
            )
        for group in sorted(set(group_keys)):
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": ":".join(group)},
            )
        condition_id = self.session.execute(
            text("""
            INSERT INTO trainer.comparison_conditions(condition_id,exercise_id,rule_version,normalized_key,readable_snapshot,created_at)
            VALUES (:id,:exercise,'record_v1',:key,CAST(:snapshot AS jsonb),:now)
            ON CONFLICT(exercise_id,rule_version,normalized_key) DO NOTHING RETURNING condition_id
        """),
            {
                "id": uuid4(),
                "exercise": exercise_id,
                "key": typed["condition_key"],
                "snapshot": json.dumps(typed["condition_readable"]),
                "now": now,
            },
        ).scalar_one_or_none()
        if condition_id is None:
            condition_id = self.session.execute(
                text("""SELECT condition_id FROM trainer.comparison_conditions
                       WHERE exercise_id=:exercise AND rule_version='record_v1' AND normalized_key=:key"""),
                {"exercise": exercise_id, "key": typed["condition_key"]},
            ).scalar_one()
        if str(typed["record_id"]) not in change_by_id:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "record change plan missing", 500
            )
        current_rows = (
            self.session.execute(
                text("""
            SELECT r.record_id,r.value,r.condition_id,c.rule_version,
                   coalesce(dc.after_snapshot->>'validity','valid') AS validity
            FROM trainer.actual_records r JOIN trainer.comparison_conditions c USING(condition_id)
            LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
              WHERE d.object_type='actual_record' AND d.object_id=r.record_id
              ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
            WHERE r.profile_id=:profile AND r.exercise_id=:exercise AND r.metric_kind=:metric
              AND r.condition_id=:condition AND r.is_current ORDER BY r.record_id FOR UPDATE OF r
        """),
                {
                    "profile": profile_id,
                    "exercise": exercise_id,
                    "metric": historical["metric_kind"],
                    "condition": condition_id,
                },
            )
            .mappings()
            .all()
        )
        current_snapshot = [
            {
                "record_id": str(item["record_id"]),
                "value": str(item["value"]),
                "condition_id": str(item["condition_id"]),
                "rule_version": item["rule_version"],
                "validity": item["validity"],
            }
            for item in current_rows
        ]
        if condition_id is not None and current_snapshot != typed["current_snapshot"]:
            raise ContextRepositoryError(
                "stale_dependency", "record group changed after preview", 409
            )
        current = current_rows[0] if current_rows else None
        if target is not None:
            target_rows = (
                self.session.execute(
                    text("""
                SELECT r.record_id,r.value,r.condition_id,c.rule_version,
                       coalesce(dc.after_snapshot->>'validity','valid') AS validity
                FROM trainer.actual_records r JOIN trainer.comparison_conditions c USING(condition_id)
                LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                  WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                  ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                WHERE r.profile_id=:profile AND r.exercise_id=:exercise AND r.metric_kind=:metric
                  AND r.condition_id=:condition AND r.is_current ORDER BY r.record_id FOR UPDATE OF r
            """),
                    {
                        "profile": profile_id,
                        "exercise": target["exercise_id"],
                        "metric": target["metric_kind"],
                        "condition": target["condition_id"],
                    },
                )
                .mappings()
                .all()
            )
            target_snapshot = [
                {
                    "record_id": str(item["record_id"]),
                    "value": str(item["value"]),
                    "condition_id": str(item["condition_id"]),
                    "rule_version": item["rule_version"],
                    "validity": item["validity"],
                }
                for item in target_rows
            ]
            if target_snapshot != typed["target_current_snapshot"]:
                raise ContextRepositoryError(
                    "stale_dependency", "historical correction group changed after preview", 409
                )
            fallback_snapshot = [
                {
                    "record_id": str(item["record_id"]),
                    "value": str(item["value"]),
                    "achieved_on": item["achieved_on"].isoformat(),
                    "created_at": item["created_at"].isoformat(),
                    "is_current": bool(item["is_current"]),
                    "validity": item["validity"],
                }
                for item in self.session.execute(
                    text("""
                    SELECT r.record_id,r.value,r.achieved_on,r.created_at,r.is_current,
                      coalesce(dc.after_snapshot->>'validity','valid') AS validity
                    FROM trainer.actual_records r
                    LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                      WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                      ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                    WHERE r.profile_id=:profile AND r.exercise_id=:exercise AND r.metric_kind=:metric
                      AND r.condition_id=:condition AND NOT r.is_current
                      AND coalesce(dc.after_snapshot->>'validity','valid')='valid'
                    ORDER BY r.achieved_on,r.created_at,r.record_id FOR UPDATE OF r
                """),
                    {
                        "profile": profile_id,
                        "exercise": target["exercise_id"],
                        "metric": target["metric_kind"],
                        "condition": target["condition_id"],
                    },
                ).mappings()
            ]
            if fallback_snapshot != typed["target_fallback_records"]:
                raise ContextRepositoryError(
                    "stale_dependency", "historical fallback group changed after preview", 409
                )
        value = Decimal(str(historical["value"]))
        becomes_current = not unknown and (
            current is None
            or strictly_improves(historical["metric_kind"], value, Decimal(str(current["value"])))
        )
        record_id = UUID(typed["record_id"])
        if current is not None and becomes_current:
            self.session.execute(
                text("UPDATE trainer.actual_records SET is_current=false WHERE record_id=:id"),
                {"id": current["record_id"]},
            )
            before_current = (
                self.session.execute(
                    text("SELECT * FROM trainer.actual_records WHERE record_id=:id"),
                    {"id": current["record_id"]},
                )
                .mappings()
                .one()
            )
            self._insert_change(
                change_by_id[str(current["record_id"])],
                result_id,
                "correct",
                dict(before_current),
                {
                    **dict(before_current),
                    "is_current": False,
                    "current_exit_reason": "replaced_by_correction"
                    if typed.get("supersedes_record_id")
                    else "better_record",
                    "current_record_id": str(record_id),
                },
                historical["reason"],
                now,
            )
        if typed.get("supersedes_record_id"):
            if target is None:
                raise ContextRepositoryError(
                    "inconsistent_domain_state", "historical correction target disappeared", 500
                )
            target_id = UUID(typed["supersedes_record_id"])
            if target_id != (current["record_id"] if current else None) or not becomes_current:
                self.session.execute(
                    text(
                        "UPDATE trainer.actual_records SET is_current=false WHERE record_id=:id AND is_current"
                    ),
                    {"id": target_id},
                )
                target_after = (
                    self.session.execute(
                        text("SELECT * FROM trainer.actual_records WHERE record_id=:id"),
                        {"id": target_id},
                    )
                    .mappings()
                    .one()
                )
                self._insert_change(
                    change_by_id[str(target_id)],
                    result_id,
                    "correct",
                    dict(target),
                    {
                        **dict(target_after),
                        "validity": "replaced_by_correction",
                        "current_exit_reason": "replaced_by_correction",
                        "current_record_id": str(record_id) if becomes_current else None,
                    },
                    historical["reason"],
                    now,
                )
                old_group_is_replaced_elsewhere = (
                    target["condition_id"] != condition_id
                    or target["exercise_id"] != exercise_id
                    or target["metric_kind"] != historical["metric_kind"]
                )
                if bool(target["is_current"]) and (
                    old_group_is_replaced_elsewhere or not becomes_current
                ):
                    fallback_rows = (
                        self.session.execute(
                            text("""
                        SELECT r.*,coalesce(dc.after_snapshot->>'validity','valid') AS validity
                        FROM trainer.actual_records r
                        LEFT JOIN LATERAL (
                          SELECT after_snapshot FROM trainer.domain_changes d
                          WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                          ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1
                        ) dc ON true
                        WHERE r.profile_id=:profile AND r.exercise_id=:exercise
                          AND r.metric_kind=:metric AND r.condition_id=:condition AND NOT r.is_current
                          AND coalesce(dc.after_snapshot->>'validity','valid')='valid'
                          AND r.record_id<>:target
                        ORDER BY r.achieved_on,r.created_at,r.record_id FOR UPDATE OF r
                    """),
                            {
                                "profile": profile_id,
                                "exercise": target["exercise_id"],
                                "metric": target["metric_kind"],
                                "condition": target["condition_id"],
                                "target": target["record_id"],
                            },
                        )
                        .mappings()
                        .all()
                    )
                    winner = None
                    for row in fallback_rows:
                        if winner is None or strictly_improves(
                            target["metric_kind"],
                            Decimal(str(row["value"])),
                            Decimal(str(winner["value"])),
                        ):
                            winner = row
                    if winner is not None:
                        winner_change = change_by_id.get(str(winner["record_id"]))
                        if winner_change is None:
                            raise ContextRepositoryError(
                                "inconsistent_domain_state",
                                "fallback record missing from change plan",
                                500,
                            )
                        self.session.execute(
                            text(
                                "UPDATE trainer.actual_records SET is_current=true WHERE record_id=:id"
                            ),
                            {"id": winner["record_id"]},
                        )
                        self._insert_change(
                            winner_change,
                            result_id,
                            "correct",
                            {**dict(winner), "is_current": False},
                            {
                                "record_id": str(winner["record_id"]),
                                "is_current": True,
                                "validity": "valid",
                                "current_exit_reason": None,
                                "current_exit_change_id": None,
                                "restored_after_historical_correction": str(target["record_id"]),
                            },
                            historical["reason"],
                            now,
                        )
        self.session.execute(
            text("""
            INSERT INTO trainer.actual_records(record_id,profile_id,exercise_id,metric_kind,value,unit_code,
              achieved_on,condition_id,session_id,session_revision_id,actual_set_id,weight_value,weight_unit,
              reps,duration_seconds,body_mass,comment,is_historical_import,supersedes_record_id,is_current,
              confirmation_id,created_at,exercise_revision_id,candidate_id,source_fingerprint)
            VALUES (:id,:profile,:exercise,:metric,:value,:unit,:date,:condition,NULL,NULL,NULL,
              :weight,:weight_unit,:reps,:duration,:body_mass,:comment,true,:supersedes,:current,
              :confirmation,:now,:exercise_revision,NULL,:fingerprint)
        """),
            {
                "id": record_id,
                "profile": profile_id,
                "exercise": exercise_id,
                "metric": historical["metric_kind"],
                "value": value,
                "unit": historical["unit_code"],
                "date": historical["achieved_on"],
                "condition": condition_id,
                "weight": value if historical["metric_kind"] == "record_external_load" else None,
                "weight_unit": "kilogram"
                if historical["metric_kind"] == "record_external_load"
                else None,
                "reps": historical["condition"].get("fixed_reps"),
                "duration": int(value)
                if historical["metric_kind"] in ("record_hold_duration", "record_cardio_time")
                else None,
                "body_mass": historical.get("body_mass_kg"),
                "comment": historical.get("comment"),
                "supersedes": typed.get("supersedes_record_id"),
                "current": becomes_current,
                "confirmation": confirmation_id,
                "now": now,
                "exercise_revision": historical["exercise_revision_id"],
                "fingerprint": digest_payload(
                    {"record_id": str(record_id), "historical": historical}
                ),
            },
        )
        after = {
            "record_id": str(record_id),
            "profile_id": str(profile_id),
            "exercise_id": str(exercise_id),
            "exercise_revision_id": historical["exercise_revision_id"],
            "metric_kind": historical["metric_kind"],
            "value": str(value),
            "unit_code": historical["unit_code"],
            "condition_id": str(condition_id),
            "is_historical_import": True,
            "is_current": becomes_current,
            "validity": "valid",
            "current_exit_reason": "historical_unknown"
            if unknown
            else ("better_record" if not becomes_current else None),
            "supersedes_record_id": typed.get("supersedes_record_id"),
            "body_mass_kg": historical.get("body_mass_kg"),
        }
        self._insert_change(
            change_by_id[str(record_id)],
            result_id,
            "create",
            None,
            after,
            historical["reason"],
            now,
        )

    def create_correction_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        from trainer.infrastructure.db.workouts import WorkoutRepository

        return WorkoutRepository(self).create_correction_draft(
            payload=payload,
            key=key,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def get_operation_status(self, *, locator: dict[str, Any]) -> dict[str, Any]:
        kind = locator["kind"]
        params: dict[str, Any] = {}
        if kind == "operation_id":
            where = "o.operation_id=:operation_id AND o.scope LIKE 'local-owner:%'"
            params["operation_id"] = locator["operation_id"]
        elif kind == "idempotency":
            where = "o.scope='local-owner:'||:method AND o.idempotency_key=:key"
            params.update(method=locator["method_id"], key=locator["idempotency_key"])
        else:
            where = "o.scope='local-owner:'||:method AND a.correlation_id=:correlation"
            params.update(method=locator["method_id"], correlation=locator["correlation_id"])
        row = (
            self.session.execute(
                text(f"""
            SELECT o.operation_id,o.command_kind,o.state,o.response_payload,o.error_code,o.updated_at,
                   cr.result_payload,a.correlation_id
            FROM trainer.idempotent_operations o
            LEFT JOIN trainer.agent_operation_audit a ON a.operation_id=o.operation_id
            LEFT JOIN trainer.commit_results cr ON cr.operation_id=o.operation_id
            WHERE {where}
            ORDER BY o.updated_at DESC LIMIT 1
        """),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "operation not found", 404)
        result_payload = row["result_payload"]
        commit_result = None
        if row["state"] == "committed" and row["command_kind"] == "CMD-00" and result_payload:
            commit_result = dict(result_payload)
            commit_result.pop("_confirmed_preview_id", None)
        error = None
        if row["state"] == "failed":
            error = {
                "code": row["error_code"] or "operation_failed",
                "message_safe": "operation failed; no committed result is available",
                "retryable": False,
                "operation_state": "failed",
                "correlation_id": row["correlation_id"]
                or locator.get("correlation_id")
                or UUID(int=0),
                "operation_id": row["operation_id"],
                "idempotency_key": None,
                "field_errors": [],
                "changed_dependencies": [],
            }
        return {
            "operation_id": row["operation_id"],
            "state": row["state"],
            "commit_result": commit_result,
            "error": error,
            "updated_at": row["updated_at"],
        }

    def _workout_revision_provenance(self, revision_id: UUID) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text("""
            SELECT e.exercise_id,e.exercise_revision_id,e.planned_exercise_id,
                   m.actual_metric_value_id,m.metric_kind,m.value,m.unit_code,m.source_kind,
                   l.actual_load_component_id,l.equipment_id,l.equipment_snapshot,
                   l.mass,l.unit_code AS load_unit,l.mass_meaning,l.direction,
                   wm.metric_kind AS workout_metric,wm.value AS workout_value,
                   wm.unit_code AS workout_unit,wm.source_kind AS workout_source,
                   wm.derived_value_id
            FROM trainer.actual_blocks b
            JOIN trainer.exercise_executions e USING(actual_block_id)
            LEFT JOIN trainer.actual_sets s USING(execution_id)
            LEFT JOIN trainer.actual_metric_values m USING(actual_set_id)
            LEFT JOIN trainer.actual_load_components l USING(actual_set_id)
            LEFT JOIN trainer.workout_metrics wm ON wm.session_revision_id=b.session_revision_id
            WHERE b.session_revision_id=:revision
            ORDER BY b.position,e.position,s.position,m.metric_kind,l.position,wm.metric_kind
        """),
            {"revision": revision_id},
        ).mappings()
        seen: set[str] = set()
        provenance: list[dict[str, Any]] = []
        for row in rows:
            exercise_ref = self._ref("exercise", row["exercise_id"], row["exercise_revision_id"])
            key = f"exercise:{row['exercise_id']}:{row['exercise_revision_id']}"
            if key not in seen:
                provenance.append(
                    {
                        "kind": "exercise_revision",
                        "ref": exercise_ref,
                        "planned_exercise_id": str(row["planned_exercise_id"])
                        if row["planned_exercise_id"]
                        else None,
                    }
                )
                seen.add(key)
            if row["actual_metric_value_id"] is not None:
                key = str(row["actual_metric_value_id"])
                if key not in seen:
                    provenance.append(
                        {
                            "kind": "reported_value",
                            "metric_kind": row["metric_kind"],
                            "value": str(row["value"]),
                            "unit_code": row["unit_code"],
                            "source_kind": row["source_kind"],
                            "exercise_ref": exercise_ref,
                        }
                    )
                    seen.add(key)
            if row["actual_load_component_id"] is not None:
                key = str(row["actual_load_component_id"])
                if key not in seen:
                    provenance.append(
                        {
                            "kind": "load_snapshot",
                            "ref": self._ref("equipment_item", row["equipment_id"], None)
                            if row["equipment_id"]
                            else None,
                            "snapshot": self._json_row(row["equipment_snapshot"]),
                            "mass": str(row["mass"]) if row["mass"] is not None else None,
                            "unit_code": row["load_unit"],
                            "mass_meaning": row["mass_meaning"],
                            "direction": row["direction"],
                        }
                    )
                    seen.add(key)
            if row["workout_metric"] is not None:
                key = f"workout_metric:{row['workout_metric']}"
                if key not in seen:
                    provenance.append(
                        {
                            "kind": "workout_metric",
                            "metric_kind": row["workout_metric"],
                            "value": str(row["workout_value"]),
                            "unit_code": row["workout_unit"],
                            "source_kind": row["workout_source"],
                            "derived_value_id": str(row["derived_value_id"])
                            if row["derived_value_id"]
                            else None,
                        }
                    )
                    seen.add(key)
        return provenance

    def create_plan_lifecycle_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-23", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-23", correlation_id, causation_id, key, replay)
            return replay
        source_ref = payload["source_ref"]
        if source_ref["object_type"] != "workout_plan" or not source_ref.get("revision_id"):
            raise ContextRepositoryError(
                "validation_error", "CMD-23 requires an exact workout_plan ref", 422
            )
        if str(source_ref["revision_id"]) != str(payload["expected_current_revision_id"]):
            raise ContextRepositoryError(
                "validation_error", "source ref and expected current revision differ", 422
            )
        source = (
            self.session.execute(
                text("""
            SELECT p.profile_id,p.current_revision_id,r.revision_no,r.lifecycle,r.context_snapshot,
                   r.context_snapshot_digest,r.content_digest,r.planned_date,r.planned_time,r.plan_type,
                   r.name,r.purpose,r.comment,r.program_id,r.program_version_id,r.day_template_id
            FROM trainer.workout_plans p JOIN trainer.workout_plan_revisions r
              ON r.plan_id=p.plan_id AND r.plan_revision_id=p.current_revision_id
            WHERE p.plan_id=:plan AND p.archived_at IS NULL AND p.logically_deleted_at IS NULL
            FOR UPDATE OF p
        """),
                {"plan": source_ref["object_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise ContextRepositoryError("not_found", "current source plan not found", 404)
        if str(source["current_revision_id"]) != str(payload["expected_current_revision_id"]):
            raise ContextRepositoryError(
                "stale_preview", "source plan revision is no longer current", 409
            )
        if source["lifecycle"] != "planned":
            raise ContextRepositoryError(
                "already_superseded", "only a current planned revision can transition", 409
            )
        if digest_payload(dict(source["context_snapshot"])) != source["context_snapshot_digest"]:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "source plan context snapshot digest is invalid", 500
            )

        body = {key: value for key, value in payload.items() if key != "request_digest"}
        lifecycle = {
            "intent": payload["intent"],
            "source_plan_id": str(source_ref["object_id"]),
            "source_revision_id": str(source["current_revision_id"]),
            "source_revision_no": source["revision_no"],
            "source_next_revision_id": str(uuid4()),
            "source_context_snapshot": source["context_snapshot"],
            "source_context_snapshot_digest": source["context_snapshot_digest"],
            "source_content_digest": source["content_digest"],
            "source_tree": self.get_plan(
                payload={"plan_id": source_ref["object_id"], "mode": "current"}
            )["tree"],
            "reason": payload.get("reason"),
        }
        change_plan = [
            {
                "object_type": "workout_plan",
                "object_id": lifecycle["source_plan_id"],
                "revision_id": lifecycle["source_next_revision_id"],
            }
        ]
        source_plan_revision = UUID(str(source["current_revision_id"]))
        dependencies = [
            {
                "dependency_type": "workout_plan",
                "object_id": str(source_ref["object_id"]),
                "revision_id": str(source_plan_revision),
                "digest": self._revision_digest("workout_plan", source_plan_revision),
                "reason": "exact current source plan revision",
            }
        ]

        if payload["intent"] == "reschedule":
            profile_id = str(source["profile_id"])
            replacement = {
                **body,
                "profile_id": profile_id,
                "plan_id": str(uuid4()),
                "plan_revision_id": str(uuid4()),
                "plan": body.pop("replacement_plan"),
                "recommendations": body.pop("replacement_recommendations"),
            }
            replacement_dependencies = self._prepare_plan_candidate(replacement)
            body.update(
                {
                    "profile_id": profile_id,
                    "plan_id": replacement["plan_id"],
                    "plan_revision_id": replacement["plan_revision_id"],
                    "plan": replacement["plan"],
                    "recommendations": replacement["recommendations"],
                    "context_snapshot": replacement["context_snapshot"],
                    "content_digest": replacement["content_digest"],
                }
            )
            dependencies.extend(replacement_dependencies)
            lifecycle["replacement_plan_id"] = replacement["plan_id"]
            lifecycle["replacement_revision_id"] = replacement["plan_revision_id"]
            change_plan.append(
                {
                    "object_type": "workout_plan",
                    "object_id": replacement["plan_id"],
                    "revision_id": replacement["plan_revision_id"],
                }
            )
        else:
            body["profile_id"] = str(source["profile_id"])
            body["plan_lifecycle"] = lifecycle

        body["plan_lifecycle"] = lifecycle
        body["change_plan"] = change_plan
        unique = {
            (item["dependency_type"], item["object_id"], item["revision_id"]): item
            for item in dependencies
        }
        dependencies = [unique[item] for item in sorted(unique)]
        revision_id = UUID(lifecycle["source_next_revision_id"])
        draft_id = uuid4()
        now = datetime.now(UTC)
        body_digest = digest_payload(body)
        self.session.execute(
            text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at)
            VALUES (:draft,'plan_lifecycle',:profile,'open',:revision,:expires,:now)
        """),
            {
                "draft": draft_id,
                "profile": source["profile_id"],
                "revision": revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text("""
            INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,
                content_digest,trust_state,confirmation_kind,created_at)
            VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)
        """),
            {
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(body),
                "digest": body_digest,
                "now": now,
            },
        )
        for dep in dependencies:
            self.session.execute(
                text("""
                INSERT INTO trainer.draft_dependencies(draft_dependency_id,draft_revision_id,dependency_type,
                    dependency_object_id,dependency_revision_id,dependency_digest,reason,created_at)
                VALUES (:id,:revision,:type,:object,:dep_revision,:digest,:reason,:now)
            """),
                {
                    "id": uuid4(),
                    "revision": revision_id,
                    "type": dep["dependency_type"],
                    "object": UUID(dep["object_id"]),
                    "dep_revision": UUID(dep["revision_id"]),
                    "digest": dep["digest"],
                    "reason": dep["reason"],
                    "now": now,
                },
            )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": body_digest,
            "dependencies": dependencies,
            "gaps": [],
            "preview_candidate": {
                "summary": "Переход плана готов к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self._finish(operation_id, response)
        self._audit(operation_id, "CMD-23", correlation_id, causation_id, key, change_plan)
        return response

    @staticmethod
    def _canonical_plan_tree(plan: dict[str, Any]) -> dict[str, Any]:
        tree = deepcopy(plan)
        for block in tree["blocks"]:
            block.pop("planned_block_id", None)
            for exercise in block["exercises"]:
                exercise.pop("planned_exercise_id", None)
                exercise.pop("exercise_ref", None)
                for planned_set in exercise["sets"]:
                    planned_set.pop("planned_set_id", None)
                    for metric in planned_set["metrics"]:
                        for key in ("exact_value", "min_value", "max_value"):
                            if metric.get(key) is not None:
                                metric[key] = ContextRepository._canonical_decimal(metric[key])
                    for load in planned_set["loads"]:
                        load.pop("planned_load_component_id", None)
                        load.pop("equipment_ref", None)
                        for key in ("item_count", "mass"):
                            if load.get(key) is not None:
                                load[key] = ContextRepository._canonical_decimal(load[key])
        return ContextRepository._json_row(tree)

    @staticmethod
    def _canonical_decimal(value: Any) -> str:
        normalized = format(Decimal(str(value)).normalize(), "f")
        return "0" if normalized in {"-0", ""} else normalized

    def _materialize_plan_recommendations(
        self, body: dict[str, Any], dependencies: list[dict[str, Any]], effective_types: set[str]
    ) -> None:
        effective = datetime.fromisoformat(body["context"]["effective_at"])
        profile_id = UUID(body["profile_id"])
        profile_ref = body["context_snapshot"]["profile_ref"]
        profile = self.session.execute(
            text(
                "SELECT to_jsonb(r) FROM trainer.profile_revisions r WHERE r.profile_revision_id=:id"
            ),
            {"id": profile_ref["revision_id"]},
        ).scalar_one()
        planning_context = self.get_profile_context(effective_at=effective)
        facts: dict[str, Any] = {
            **profile,
            "profile_id": body["profile_id"],
            "goals": [item["goal"] for item in planning_context["goals"]],
            "effective_equipment_types": sorted(effective_types),
            "program_id": body["plan"].get("program_id"),
            "program_version_id": body["plan"].get("program_version_id"),
        }
        allowed_fact_tables = {
            "profile": ("profile_revisions", "profile_revision_id", "profile_id"),
            "goal": ("goal_revisions", "goal_revision_id", "goal_id"),
            "exercise": ("exercise_revisions", "exercise_revision_id", "exercise_id"),
            "training_place": ("training_place_revisions", "place_revision_id", "place_id"),
            "equipment_item": ("equipment_item_revisions", "equipment_revision_id", "equipment_id"),
            "program_version": ("program_versions", "program_version_id", "program_id"),
        }
        for index, recommendation in enumerate(body["recommendations"]):
            if (
                self.session.execute(
                    text(
                        "SELECT 1 FROM trainer.recommendation_kind_catalog WHERE code=:code AND archived_at IS NULL"
                    ),
                    {"code": recommendation["recommendation_kind"]},
                ).first()
                is None
            ):
                raise ContextRepositoryError("validation_error", "unknown recommendation kind", 422)
            recommendation["recommendation_id"] = recommendation.get("recommendation_id") or str(
                uuid4()
            )
            if not recommendation["fact_links"]:
                raise ContextRepositoryError(
                    "validation_error", "plan recommendation requires exact fact links", 422
                )
            supported = False
            for link in recommendation["fact_links"]:
                ref = link["ref"]
                object_type = ref["object_type"]
                if object_type not in allowed_fact_tables or ref.get("revision_id") is None:
                    raise ContextRepositoryError(
                        "validation_error", "unsupported or inexact recommendation fact", 422
                    )
                table, revision_column, identity_column = allowed_fact_tables[object_type]
                fact_row = self.session.execute(
                    text(
                        f"SELECT to_jsonb(r) FROM trainer.{table} r WHERE {revision_column}=:revision AND {identity_column}=:object"
                    ),
                    {"revision": ref["revision_id"], "object": ref["object_id"]},
                ).scalar_one_or_none()
                if fact_row is None:
                    raise ContextRepositoryError("not_found", "recommendation fact not found", 404)
                actual = digest_payload(fact_row)
                if actual != link["snapshot_digest"]:
                    raise ContextRepositoryError(
                        "validation_error", "recommendation fact digest mismatch", 422
                    )
                dependencies.append(
                    {
                        "dependency_type": object_type,
                        "object_id": str(ref["object_id"]),
                        "revision_id": str(ref["revision_id"]),
                        "digest": actual,
                        "reason": "exact recommendation fact",
                    }
                )
            for link in recommendation["calculation_links"]:
                value = self.session.execute(
                    text(
                        "SELECT to_jsonb(v) FROM trainer.derived_values v WHERE derived_value_id=:id"
                    ),
                    {"id": link["derived_value_id"]},
                ).scalar_one_or_none()
                if (
                    value is None
                    or str(value["profile_id"]) != str(profile_id)
                    or value["state"] != "current"
                ):
                    raise ContextRepositoryError(
                        "stale_preview",
                        "recommendation calculation is not current for profile",
                        409,
                    )
                dependencies.append(
                    {
                        "dependency_type": "derived_value",
                        "object_id": link["derived_value_id"],
                        "revision_id": link["derived_value_id"],
                        "digest": digest_payload(value),
                        "reason": "current recommendation calculation",
                    }
                )
            for link in recommendation["conclusion_links"]:
                version_id = UUID(link["conclusion_version_id"])
                snapshot = self._knowledge_snapshot(version_id, effective)
                if snapshot is None:
                    raise ContextRepositoryError(
                        "not_found", "recommendation conclusion not found", 404
                    )
                status = snapshot["status"]
                assessment = assess_applicability(snapshot["scopes"], facts)
                eligible = (
                    status["status"] == "verified"
                    and snapshot["is_current"]
                    and snapshot["version"]["review_due_on"] >= effective.date().isoformat()
                    and assessment["outcome"] == "applicable"
                )
                assessment["eligible"] = eligible
                link["status_at_decision"] = status["status"]
                link["applicability_assessment"] = assessment
                link["status_revision_ref"] = {
                    "object_type": "knowledge_status_revision",
                    "object_id": status["status_revision_id"],
                    "revision_id": status["status_revision_id"],
                }
                dependencies.append(
                    {
                        "dependency_type": "conclusion_version",
                        "object_id": str(version_id),
                        "revision_id": str(status["status_revision_id"]),
                        "digest": digest_payload(snapshot),
                        "reason": "current recommendation conclusion status and applicability",
                    }
                )
                supported = supported or (link["role"] == "supports" and eligible)
                if link["role"] == "supports" and not eligible:
                    raise ContextRepositoryError(
                        "validation_error",
                        "noneligible knowledge cannot be used as verified plan support",
                        422,
                    )
            if recommendation["conclusion_links"] and not supported:
                raise ContextRepositoryError(
                    "validation_error",
                    f"plan recommendation {index} has no applicable verified support",
                    422,
                )

    def _materialize_plan_tree(self, body: dict[str, Any], effective_types: set[str]) -> None:
        for block in body["plan"]["blocks"]:
            if not self.session.execute(
                text(
                    "SELECT 1 FROM trainer.block_type_catalog WHERE code=:code AND archived_at IS NULL"
                ),
                {"code": block["block_type_code"]},
            ).first():
                raise ContextRepositoryError("validation_error", "unknown planned block type", 422)
            rounds = block.get("declared_rounds")
            for exercise in block["exercises"]:
                exercise_id = UUID(str(exercise["exercise_id"]))
                exact = (
                    self.session.execute(
                        text("""
                    SELECT r.exercise_revision_id,r.unilateral,r.status_code FROM trainer.exercise_revisions r
                    JOIN trainer.exercises e ON e.exercise_id=r.exercise_id
                    WHERE r.exercise_id=:exercise AND e.current_revision_id=r.exercise_revision_id
                """),
                        {"exercise": exercise_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if exact is None or exact["status_code"] != "active":
                    raise ContextRepositoryError(
                        "validation_error", "planned exercise must be an exact active revision", 422
                    )
                revision_id = UUID(str(exact["exercise_revision_id"]))
                exercise["exercise_ref"] = {
                    "object_type": "exercise",
                    "object_id": str(exercise_id),
                    "revision_id": str(revision_id),
                }
                requirements = [
                    self._json_row(row)
                    for row in self.session.execute(
                        text("""
                    SELECT equipment_type,optionality,alternative_group
                    FROM trainer.exercise_equipment_requirements WHERE exercise_revision_id=:revision
                    ORDER BY equipment_type,optionality,coalesce(alternative_group,'')
                """),
                        {"revision": revision_id},
                    ).mappings()
                ]
                required = {
                    item["equipment_type"]
                    for item in requirements
                    if item["optionality"] == "required"
                }
                missing = required - effective_types
                alternative_groups: dict[str, set[str]] = {}
                for item in requirements:
                    if item["optionality"] == "alternative":
                        alternative_groups.setdefault(item["alternative_group"], set()).add(
                            item["equipment_type"]
                        )
                if missing or any(
                    not (types & effective_types) for types in alternative_groups.values()
                ):
                    raise ContextRepositoryError(
                        "validation_error",
                        "effective equipment does not satisfy exercise requirements",
                        422,
                    )
                exercise["equipment_snapshot"] = {"requirements": requirements}
                measurements = {
                    row["metric_kind"]: dict(row)
                    for row in self.session.execute(
                        text(
                            "SELECT metric_kind,requiredness,unit_family FROM trainer.exercise_measurements WHERE exercise_revision_id=:revision"
                        ),
                        {"revision": revision_id},
                    ).mappings()
                }
                if exact["unilateral"] and any(
                    planned_set.get("side") is None for planned_set in exercise["sets"]
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unilateral exercise requires explicit side", 422
                    )
                if not exact["unilateral"] and any(
                    planned_set.get("side") not in ("both", "not_applicable")
                    for planned_set in exercise["sets"]
                ):
                    raise ContextRepositoryError(
                        "validation_error",
                        "bilateral exercise requires both or not_applicable",
                        422,
                    )
                for planned_set in exercise["sets"]:
                    if not self.session.execute(
                        text(
                            "SELECT 1 FROM trainer.set_type_catalog WHERE code=:code AND archived_at IS NULL"
                        ),
                        {"code": planned_set["set_type_code"]},
                    ).first():
                        raise ContextRepositoryError(
                            "validation_error", "unknown planned set type", 422
                        )
                    if rounds is not None and any(
                        item.get("round_no") is not None and item["round_no"] > rounds
                        for item in [planned_set]
                    ):
                        raise ContextRepositoryError(
                            "validation_error", "round_no exceeds declared rounds", 422
                        )
                    metric_kinds = {metric["metric_kind"] for metric in planned_set["metrics"]}
                    missing_metrics = {
                        kind
                        for kind, spec in measurements.items()
                        if spec["requiredness"] == "required"
                    } - metric_kinds
                    if missing_metrics:
                        raise ContextRepositoryError(
                            "validation_error", "required exercise metric is missing", 422
                        )
                    for metric in planned_set["metrics"]:
                        spec = measurements.get(metric["metric_kind"])
                        family = self.session.execute(
                            text(
                                "SELECT family_code FROM trainer.unit_family_mapping WHERE unit_code=:unit"
                            ),
                            {"unit": metric["unit_code"]},
                        ).scalar_one_or_none()
                        if spec is None or family != spec["unit_family"]:
                            raise ContextRepositoryError(
                                "validation_error",
                                "metric is incompatible with exact exercise revision",
                                422,
                            )
                        if not self.session.execute(
                            text(
                                "SELECT 1 FROM trainer.metric_kind_catalog WHERE code=:code AND archived_at IS NULL"
                            ),
                            {"code": metric["metric_kind"]},
                        ).first():
                            raise ContextRepositoryError(
                                "validation_error", "unknown metric kind", 422
                            )
                        if not self.session.execute(
                            text(
                                "SELECT 1 FROM trainer.unit_catalog WHERE code=:code AND archived_at IS NULL"
                            ),
                            {"code": metric["unit_code"]},
                        ).first():
                            raise ContextRepositoryError(
                                "validation_error", "unknown unit code", 422
                            )
                    for load in planned_set["loads"]:
                        if not self.session.execute(
                            text(
                                "SELECT 1 FROM trainer.load_kind_catalog WHERE code=:code AND archived_at IS NULL"
                            ),
                            {"code": load["load_kind"]},
                        ).first():
                            raise ContextRepositoryError(
                                "validation_error", "unknown load kind", 422
                            )
                        equipment_id = load.get("equipment_id")
                        if equipment_id is None:
                            load["equipment_ref"] = None
                            load["equipment_snapshot"] = {}
                            continue
                        equipment = (
                            self.session.execute(
                                text("""
                            SELECT i.profile_id,i.current_revision_id,r.*
                            FROM trainer.equipment_items i
                            JOIN trainer.equipment_item_revisions r ON r.equipment_revision_id=i.current_revision_id
                            WHERE i.equipment_id=:id AND i.archived_at IS NULL AND i.logically_deleted_at IS NULL
                        """),
                                {"id": equipment_id},
                            )
                            .mappings()
                            .one_or_none()
                        )
                        if equipment is None or str(equipment["profile_id"]) != body["profile_id"]:
                            raise ContextRepositoryError(
                                "not_found", "equipment item does not belong to profile", 404
                            )
                        if (
                            equipment["equipment_type"] not in effective_types
                            or equipment["condition"] != "usable"
                        ):
                            raise ContextRepositoryError(
                                "validation_error",
                                "equipment item is not explicitly usable for this plan",
                                422,
                            )
                        exact_available_item = any(
                            str(item["ref"]["object_id"]) == str(equipment_id)
                            and str(item["ref"]["revision_id"])
                            == str(equipment["current_revision_id"])
                            and item["equipment"]["condition"] == "usable"
                            for item in body["context_snapshot"]["equipment_inventory_snapshot"]
                        )
                        if not exact_available_item:
                            raise ContextRepositoryError(
                                "validation_error",
                                "equipment item is not in exact current global inventory snapshot",
                                422,
                            )
                        load["equipment_ref"] = {
                            "object_type": "equipment_item",
                            "object_id": str(equipment_id),
                            "revision_id": str(equipment["current_revision_id"]),
                        }
                        load["equipment_snapshot"] = next(
                            deepcopy(item["equipment"])
                            for item in body["context_snapshot"]["equipment_inventory_snapshot"]
                            if item["ref"]["object_id"] == str(equipment_id)
                        )

    def _reserve(self, method: str, key: str, digest: str) -> tuple[UUID, dict[str, Any] | None]:
        operation_id = uuid4()
        now = datetime.now(UTC)
        self.session.execute(
            text("""
                INSERT INTO trainer.idempotent_operations(
                    operation_id,scope,idempotency_key,command_kind,request_digest,state,
                    received_at,updated_at,created_at
                ) VALUES (:id,'local-owner:'||:method,:key,:method,:digest,'received',:now,:now,:now)
                ON CONFLICT (scope,idempotency_key) DO NOTHING
            """),
            {"id": operation_id, "method": method, "key": key, "digest": digest, "now": now},
        )
        row = (
            self.session.execute(
                text("""
                SELECT operation_id,command_kind,request_digest,state,response_payload
                FROM trainer.idempotent_operations
                WHERE scope='local-owner:'||:method AND idempotency_key=:key FOR UPDATE
            """),
                {"method": method, "key": key},
            )
            .mappings()
            .one()
        )
        if row["command_kind"] != method or row["request_digest"] != digest:
            raise ContextRepositoryError(
                "idempotency_conflict", "idempotency key is bound to another request", 409
            )
        if row["operation_id"] != operation_id:
            if row["state"] == "committed" and row["response_payload"] is not None:
                return row["operation_id"], dict(row["response_payload"])
            raise ContextRepositoryError(
                "operation_in_progress", "operation has no reusable result yet", 409
            )
        return operation_id, None

    def _finish(self, operation_id: UUID, response: dict[str, Any]) -> None:
        self.session.execute(
            text("""
                UPDATE trainer.idempotent_operations
                SET state='committed',response_payload=CAST(:response AS jsonb),updated_at=now()
                WHERE operation_id=:operation_id
            """),
            {"operation_id": operation_id, "response": json.dumps(response)},
        )

    def _audit(
        self,
        operation_id: UUID,
        method: str,
        correlation_id: UUID,
        causation_id: UUID | None,
        key: str,
        refs: list[dict[str, str]],
        result: str = "succeeded",
        error: str | None = None,
        replayed: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        attempt_no = self.session.execute(
            text(
                "SELECT coalesce(max(attempt_no),0)+1 FROM trainer.agent_operation_audit "
                "WHERE operation_id=:operation"
            ),
            {"operation": operation_id},
        ).scalar_one()
        self.session.execute(
            text("""
                INSERT INTO trainer.agent_operation_audit(
                    audit_id,operation_id,method_id,object_refs,attempt_no,result_kind,
                    correlation_id,causation_id,idempotency_key_digest,error_code,started_at,finished_at,
                    latency_ms,safe_metadata,created_at
                ) VALUES (:id,:operation_id,:method,CAST(:refs AS jsonb),:attempt,:result,
                    :correlation,:causation,:key_digest,:error,:now,:now,0,
                    CAST(:metadata AS jsonb),:now)
            """),
            {
                "id": uuid4(),
                "operation_id": operation_id,
                "method": method,
                "refs": json.dumps(refs),
                "result": result,
                "attempt": attempt_no,
                "correlation": correlation_id,
                "causation": causation_id,
                "key_digest": hashlib.sha256(key.encode()).hexdigest(),
                "error": error,
                "metadata": json.dumps({"replayed": replayed}),
                "now": now,
            },
        )

    def _audit_replay(
        self,
        operation_id: UUID,
        method: str,
        correlation_id: UUID,
        causation_id: UUID | None,
        key: str,
        response: dict[str, Any],
    ) -> None:
        refs: list[dict[str, str]] = []
        if "draft_id" in response:
            refs.append(
                {
                    "object_type": "draft",
                    "object_id": str(response["draft_id"]),
                    "revision_id": str(response["revision_id"]),
                }
            )
        elif "preview_id" in response:
            refs.append({"object_type": "preview", "object_id": str(response["preview_id"])})
        else:
            refs.extend(
                {
                    "object_type": str(item["object_type"]),
                    "object_id": str(item["object_id"]),
                }
                for item in response.get("domain_changes", [])
            )
        self._audit(
            operation_id,
            method,
            correlation_id,
            causation_id,
            key,
            refs,
            replayed=True,
        )

    def record_failed_request(
        self,
        *,
        method: str,
        key: str | None,
        correlation_id: UUID,
        causation_id: UUID | None,
        error_code: str,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        self.session.execute(
            text("""
                INSERT INTO trainer.agent_operation_audit(
                    audit_id,operation_id,method_id,object_refs,attempt_no,result_kind,
                    correlation_id,causation_id,idempotency_key_digest,error_code,started_at,
                    finished_at,latency_ms,safe_metadata,created_at
                ) VALUES (:id,NULL,:method,'[]'::jsonb,1,'failed',:correlation,:causation,
                    :key_digest,:error,:now,:now,0,'{}'::jsonb,:now)
            """),
            {
                "id": uuid4(),
                "method": method,
                "correlation": correlation_id,
                "causation": causation_id,
                "key_digest": hashlib.sha256(key.encode()).hexdigest() if key else None,
                "error": error_code,
                "now": now,
            },
        )
        return {"recorded": True}

    def create_onboarding_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        body = materialize_onboarding_defaults(payload)
        validate_onboarding(body)
        operation_id, replay = self._reserve("CMD-01", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-01", correlation_id, causation_id, key, replay)
            return replay
        if self.session.execute(
            text("SELECT 1 FROM trainer.profiles WHERE archived_at IS NULL")
        ).first():
            raise ContextRepositoryError("revision_conflict", "active profile already exists", 409)
        body.pop("request_digest", None)
        body["profile_id"] = str(uuid4())
        for goal in body["goals"]:
            goal["goal_id"] = goal.get("goal_id") or str(uuid4())
        familiarity_refs: list[tuple[UUID, UUID]] = []
        for selection in body.get("familiar_exercises", []):
            exercise_ref = selection["exercise_ref"]
            exercise_id = UUID(str(exercise_ref["object_id"]))
            exercise_revision_id = UUID(str(exercise_ref["revision_id"]))
            eligible = (
                self.session.execute(
                    text("""
                    SELECT e.current_revision_id,e.archived_at,e.logically_deleted_at,
                           s.planning_eligible,r.canonical_name
                    FROM trainer.exercises e
                    JOIN trainer.exercise_revisions r
                      ON r.exercise_id=e.exercise_id
                     AND r.exercise_revision_id=:revision
                    JOIN trainer.exercise_status_catalog s ON s.code=r.status_code
                    WHERE e.exercise_id=:exercise
                """),
                    {"exercise": exercise_id, "revision": exercise_revision_id},
                )
                .mappings()
                .one_or_none()
            )
            if (
                eligible is None
                or eligible["current_revision_id"] != exercise_revision_id
                or eligible["archived_at"] is not None
                or eligible["logically_deleted_at"] is not None
                or not eligible["planning_eligible"]
            ):
                raise ContextRepositoryError(
                    "validation_error",
                    "familiar exercise must be an exact current planning-eligible revision",
                )
            selection["name"] = eligible["canonical_name"]
            familiarity_refs.append((exercise_id, exercise_revision_id))
        body["change_plan"] = self._new_onboarding_change_plan(body)
        validate_onboarding(body)
        draft_id, revision_id = uuid4(), uuid4()
        digest = digest_payload(body)
        now = datetime.now(UTC)
        self.session.execute(
            text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,
                expires_at,created_at) VALUES (:draft,'onboarding',NULL,'open',:revision,:expires,:now)
        """),
            {
                "draft": draft_id,
                "revision": revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text("""
            INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,
                content_digest,trust_state,confirmation_kind,created_at)
            VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)
        """),
            {
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(body),
                "digest": digest,
                "now": now,
            },
        )
        familiarity_dependencies: list[dict[str, Any]] = []
        for exercise_id, exercise_revision_id in familiarity_refs:
            familiarity_dependencies.append(
                self._add_dependency(
                    revision_id,
                    "exercise_revision",
                    exercise_id,
                    exercise_revision_id,
                    "selected familiar exercise must remain current and planning-eligible",
                    now,
                )
            )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": familiarity_dependencies,
            "gaps": [],
            "preview_candidate": {
                "summary": "Полный исходный контекст готов к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self._finish(operation_id, response)
        self._audit(
            operation_id,
            "CMD-01",
            correlation_id,
            causation_id,
            key,
            [{"object_type": "draft", "object_id": str(draft_id), "revision_id": str(revision_id)}],
        )
        return response

    def create_goal_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        validate_goal(payload["goal"])
        operation_id, replay = self._reserve("CMD-02", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-02", correlation_id, causation_id, key, replay)
            return replay
        profile = (
            self.session.execute(
                text(
                    "SELECT profile_id,current_revision_id FROM trainer.profiles WHERE archived_at IS NULL"
                ),
                {},
            )
            .mappings()
            .one_or_none()
        )
        if profile is None or profile["current_revision_id"] is None:
            raise ContextRepositoryError("insufficient_data", "confirmed profile is required", 422)
        body = {k: v for k, v in payload.items() if k != "request_digest"}
        intent = body["intent"]
        goal = body["goal"]
        if intent == "create":
            goal["goal_id"] = goal.get("goal_id") or str(uuid4())
            if body.get("expected_revision_id") is not None:
                raise ContextRepositoryError(
                    "validation_error", "create cannot have expected_revision_id"
                )
        else:
            if goal.get("goal_id") is None or body.get("expected_revision_id") is None:
                raise ContextRepositoryError(
                    "validation_error", "goal_id and expected_revision_id are required"
                )
            current = (
                self.session.execute(
                    text("""
                SELECT g.current_revision_id,g.parent_goal_id FROM trainer.goals g
                WHERE g.goal_id=:goal AND g.profile_id=:profile AND g.archived_at IS NULL
            """),
                    {"goal": goal["goal_id"], "profile": profile["profile_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise ContextRepositoryError("not_found", "goal not found", 404)
            if str(current["current_revision_id"]) != str(body["expected_revision_id"]):
                raise ContextRepositoryError("revision_conflict", "goal revision changed", 409)
            if intent == "cancel":
                goal["status"] = "cancelled"
        if goal.get("parent_goal_id") is not None:
            parent = self.session.execute(
                text(
                    "SELECT current_revision_id FROM trainer.goals "
                    "WHERE goal_id=:id AND profile_id=:profile AND archived_at IS NULL"
                ),
                {"id": goal["parent_goal_id"], "profile": profile["profile_id"]},
            ).scalar_one_or_none()
            if parent is None:
                raise ContextRepositoryError(
                    "validation_error", "parent goal must belong to the same profile"
                )
            self._assert_acyclic_parent(
                UUID(str(goal["goal_id"])), UUID(str(goal["parent_goal_id"]))
            )
        body["change_plan"] = [
            {
                "object_type": "goal",
                "object_id": goal["goal_id"],
                "revision_id": str(uuid4()),
            }
        ]
        draft_id, revision_id = uuid4(), uuid4()
        digest = digest_payload(body)
        now = datetime.now(UTC)
        self.session.execute(
            text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,
                expires_at,created_at) VALUES (:draft,'goal',:profile,'open',:revision,:expires,:now)
        """),
            {
                "draft": draft_id,
                "profile": profile["profile_id"],
                "revision": revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text("""
            INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,
                content_digest,trust_state,confirmation_kind,created_at)
            VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)
        """),
            {
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(body),
                "digest": digest,
                "now": now,
            },
        )
        dependencies: list[dict[str, Any]] = []
        dependency_rows = [
            ("profile", profile["profile_id"], profile["current_revision_id"], "current profile")
        ]
        if intent != "create":
            dependency_rows.append(
                (
                    "goal",
                    UUID(str(goal["goal_id"])),
                    UUID(str(body["expected_revision_id"])),
                    "current goal",
                )
            )
        if goal.get("parent_goal_id") is not None:
            ancestors = self.session.execute(
                text("""
                    WITH RECURSIVE ancestor_chain(goal_id,parent_goal_id,current_revision_id,depth) AS (
                        SELECT goal_id,parent_goal_id,current_revision_id,1
                        FROM trainer.goals WHERE goal_id=:parent
                        UNION ALL
                        SELECT g.goal_id,g.parent_goal_id,g.current_revision_id,a.depth+1
                        FROM trainer.goals g JOIN ancestor_chain a ON g.goal_id=a.parent_goal_id
                    )
                    SELECT goal_id,current_revision_id,depth FROM ancestor_chain ORDER BY depth
                """),
                {"parent": goal["parent_goal_id"]},
            ).mappings()
            dependency_rows.extend(
                (
                    "goal",
                    UUID(str(ancestor["goal_id"])),
                    UUID(str(ancestor["current_revision_id"])),
                    "current parent goal" if ancestor["depth"] == 1 else "current goal ancestor",
                )
                for ancestor in ancestors
            )
        for dep_type, object_id, dep_revision, reason in dependency_rows:
            dep_digest = self._revision_digest(dep_type, dep_revision)
            dep = {
                "dependency_type": dep_type,
                "object_id": str(object_id),
                "revision_id": str(dep_revision),
                "digest": dep_digest,
                "reason": reason,
            }
            dependencies.append(dep)
            self.session.execute(
                text("""
                INSERT INTO trainer.draft_dependencies(draft_dependency_id,draft_revision_id,
                    dependency_type,dependency_object_id,dependency_revision_id,dependency_digest,reason,created_at)
                VALUES (:id,:revision,:type,:object,:dep_revision,:digest,:reason,:now)
            """),
                {
                    "id": uuid4(),
                    "revision": revision_id,
                    "type": dep_type,
                    "object": object_id,
                    "dep_revision": dep_revision,
                    "digest": dep_digest,
                    "reason": reason,
                    "now": now,
                },
            )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": dependencies,
            "gaps": [],
            "preview_candidate": {
                "summary": f"Изменение цели ({intent}) готово к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self._finish(operation_id, response)
        self._audit(
            operation_id,
            "CMD-02",
            correlation_id,
            causation_id,
            key,
            [{"object_type": "draft", "object_id": str(draft_id), "revision_id": str(revision_id)}],
        )
        return response

    def create_exercise_catalog_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        validate_exercise(payload)
        operation_id, replay = self._reserve("CMD-22", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-22", correlation_id, causation_id, key, replay)
            return replay
        body = {k: v for k, v in payload.items() if k != "request_digest"}
        exercise = body["exercise"]
        normalized_name = normalize_name(exercise["canonical_name"])
        if body["intent"] == "create":
            exercise_id = uuid4()
            exercise["exercise_id"] = str(exercise_id)
            if self.session.execute(
                text("SELECT 1 FROM trainer.exercises WHERE active_normalized_name=:name"),
                {"name": normalized_name},
            ).first():
                raise ContextRepositoryError(
                    "revision_conflict", "active exercise name exists", 409
                )
            before = None
            revision_no = 1
        else:
            exercise_id = UUID(str(exercise["exercise_id"]))
            current = (
                self.session.execute(
                    text("""
                        SELECT current_revision_id FROM trainer.exercises
                        WHERE exercise_id=:id AND archived_at IS NULL AND logically_deleted_at IS NULL
                    """),
                    {"id": exercise_id},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise ContextRepositoryError("not_found", "exercise not found", 404)
            if str(current["current_revision_id"]) != str(body["expected_revision_id"]):
                raise ContextRepositoryError("revision_conflict", "exercise revision changed", 409)
            before = self._load_exercise_aggregate(
                exercise_id, UUID(str(body["expected_revision_id"]))
            )
            revision_no = (
                int(
                    self.session.execute(
                        text(
                            "SELECT revision_no FROM trainer.exercise_revisions WHERE exercise_revision_id=:id"
                        ),
                        {"id": body["expected_revision_id"]},
                    ).scalar_one()
                )
                + 1
            )

        self._assert_catalog_codes(exercise)
        resolved_roles: list[dict[str, Any]] = []
        created_groups: dict[str, dict[str, Any]] = {}
        for role in exercise["muscle_roles"]:
            if role["kind"] == "existing":
                group = self._muscle_group(UUID(str(role["muscle_group_id"])))
                if group is None:
                    raise ContextRepositoryError("validation_error", "muscle group not found", 422)
            else:
                normalized = normalize_name(role["name"])
                if role["status_code"] != "active":
                    raise ContextRepositoryError(
                        "validation_error", "invalid muscle group status", 422
                    )
                group = self._muscle_group_by_name(normalized)
                if group is None:
                    group = created_groups.setdefault(
                        normalized,
                        {
                            "muscle_group_id": str(uuid4()),
                            "name": role["name"].strip(),
                            "normalized_name": normalized,
                            "normalization_version": NORMALIZATION_VERSION,
                            "status_code": role["status_code"],
                        },
                    )
                elif group["status_code"] != role["status_code"]:
                    raise ContextRepositoryError(
                        "validation_error", "muscle group status is incompatible", 422
                    )
            resolved_roles.append({"muscle_group": dict(group), "role": role["role"]})

        revision_id = uuid4()
        exercise["canonical_name"] = exercise["canonical_name"].strip()
        exercise["normalized_name"] = normalized_name
        exercise["normalization_version"] = NORMALIZATION_VERSION
        body["resolved_muscle_roles"] = resolved_roles
        body["created_muscle_groups"] = list(created_groups.values())
        body["before"] = before
        body["revision_no"] = revision_no
        body["change_plan"] = [
            {
                "object_type": "exercise",
                "object_id": str(exercise_id),
                "revision_id": str(revision_id),
            }
        ] + [
            {
                "object_type": "muscle_group",
                "object_id": group["muscle_group_id"],
                "revision_id": None,
            }
            for group in created_groups.values()
        ]
        validate_exercise(body)
        draft_id, draft_revision_id = uuid4(), uuid4()
        content_digest = digest_payload(body)
        now = datetime.now(UTC)
        self.session.execute(
            text("""
                INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,
                    latest_revision_id,expires_at,created_at)
                VALUES (:draft,'exercise_catalog',NULL,'open',:revision,:expires,:now)
            """),
            {
                "draft": draft_id,
                "revision": draft_revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text("""
                INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,
                    typed_payload,content_digest,trust_state,confirmation_kind,created_at)
                VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)
            """),
            {
                "revision": draft_revision_id,
                "draft": draft_id,
                "payload": json.dumps(body),
                "digest": content_digest,
                "now": now,
            },
        )
        dependencies: list[dict[str, Any]] = []
        if body["intent"] == "correct":
            dependencies.append(
                self._add_dependency(
                    draft_revision_id,
                    "exercise",
                    exercise_id,
                    UUID(str(body["expected_revision_id"])),
                    "current exercise",
                    now,
                )
            )
        for role in resolved_roles:
            group = role["muscle_group"]
            if group["normalized_name"] not in created_groups:
                group_id = UUID(str(group["muscle_group_id"]))
                dependencies.append(
                    self._add_dependency(
                        draft_revision_id,
                        "muscle_group",
                        group_id,
                        group_id,
                        "existing muscle group",
                        now,
                    )
                )
        rules_id = uuid5(NAMESPACE_URL, "trainer:exercise-catalog-rules")
        rules_revision = uuid5(NAMESPACE_URL, self._catalog_rules_digest())
        dependencies.append(
            self._add_dependency(
                draft_revision_id,
                "exercise_catalog_rules",
                rules_id,
                rules_revision,
                "catalog and normalization rules",
                now,
            )
        )
        name_id = uuid5(NAMESPACE_URL, f"trainer:exercise-name:{normalized_name}")
        name_revision = self._active_name_revision(normalized_name)
        dependencies.append(
            self._add_dependency(
                draft_revision_id,
                "active_exercise_name",
                name_id,
                name_revision,
                "active normalized exercise name",
                now,
            )
        )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(draft_revision_id),
            "revision_no": 1,
            "content_digest": content_digest,
            "dependencies": dependencies,
            "gaps": [],
            "preview_candidate": {
                "summary": f"Полный агрегат упражнения ({body['intent']}) готов к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self._finish(operation_id, response)
        self._audit(
            operation_id,
            "CMD-22",
            correlation_id,
            causation_id,
            key,
            [
                {
                    "object_type": "draft",
                    "object_id": str(draft_id),
                    "revision_id": str(draft_revision_id),
                }
            ],
        )
        return response

    def create_familiar_exercise_batch_draft(
        self, *, payload: dict[str, Any], key: str,
        correlation_id: UUID, causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        from trainer.infrastructure.db.familiar_batch import FamiliarBatchRepository

        return FamiliarBatchRepository(self).create(
            payload=payload, key=key, correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def create_program_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
        _target: tuple[UUID, int] | None = None,
        _operation: UUID | None = None,
    ) -> dict[str, Any]:
        validate_program_draft(payload)
        method = "CMD-19" if _target is not None else "CMD-03"
        operation_id, replay = (
            (_operation, None)
            if _operation is not None
            else self._reserve(method, key, payload["request_digest"])
        )
        if replay is not None:
            self._audit_replay(operation_id, "CMD-03", correlation_id, causation_id, key, replay)
            return replay
        body = {k: v for k, v in payload.items() if k != "request_digest"}
        body["source_input"] = json.loads(json.dumps(body))
        profile_id = self.session.execute(
            text("SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL")
        ).scalar_one_or_none()
        if profile_id is None:
            raise ContextRepositoryError("not_found", "profile not found", 404)
        goal = (
            self.session.execute(
                text(
                    "SELECT goal_id,current_revision_id FROM trainer.goals WHERE current_revision_id=:revision AND archived_at IS NULL AND logically_deleted_at IS NULL"
                ),
                {"revision": body["goal_revision_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if goal is None:
            raise ContextRepositoryError("not_found", "goal revision not found", 404)
        now = datetime.now(UTC)
        if body["intent"] == "create":
            program_id, version_no, supersedes = uuid4(), 1, None
            body["program_snapshot"] = dict(body["program"])
        else:
            program_id = UUID(str(body["program_id"]))
            current = (
                self.session.execute(
                    text(
                        "SELECT goal_id,current_version_id,starts_on,ends_on FROM trainer.programs WHERE program_id=:id AND archived_at IS NULL AND logically_deleted_at IS NULL"
                    ),
                    {"id": program_id},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise ContextRepositoryError("not_found", "program not found", 404)
            if str(current["current_version_id"]) != str(body["expected_current_version_id"]):
                raise ContextRepositoryError(
                    "revision_conflict", "program current version changed", 409
                )
            if str(current["goal_id"]) != str(goal["goal_id"]):
                raise ContextRepositoryError(
                    "validation_error", "goal revision does not belong to program", 400
                )
            if not (
                current["starts_on"]
                <= date.fromisoformat(body["version"]["period_start"])
                <= date.fromisoformat(body["version"]["period_end"])
                <= current["ends_on"]
            ):
                raise ContextRepositoryError(
                    "validation_error", "version period must be inside program period", 400
                )
            supersedes = current["current_version_id"]
            card = (
                self.session.execute(
                    text(
                        "SELECT name,description,starts_on,ends_on,status,comment FROM trainer.programs WHERE program_id=:id"
                    ),
                    {"id": program_id},
                )
                .mappings()
                .one()
            )
            body["program_snapshot"] = {
                **self._fields(card, "name", "description", "starts_on", "ends_on", "comment"),
                "status_code": card["status"],
            }
            version_no = self.session.execute(
                text("SELECT max(version_no)+1 FROM trainer.program_versions WHERE program_id=:id"),
                {"id": program_id},
            ).scalar_one()
        version_id = uuid4()
        body["program_id"] = str(program_id)
        body["program_version_id"] = str(version_id)
        body["profile_id"] = str(profile_id)
        body["goal_id"] = str(goal["goal_id"])
        body["version_no"] = version_no
        body["supersedes_version_id"] = str(supersedes) if supersedes else None
        self._materialize_program_tree(body)
        from trainer.infrastructure.db.inventory import InventoryRepository

        full_inventory, inventory_digest, effective_types = inventory_snapshot(
            InventoryRepository(self).current(profile_id)
        )
        body.update(
            {
                "inventory_snapshot_version": "global_inventory_v2",
                "equipment_inventory_snapshot": full_inventory,
                "equipment_inventory_digest": inventory_digest,
                "effective_equipment_types": effective_types,
            }
        )
        body["gaps"] = self._materialize_program_evidence(body)
        body["change_plan"] = [
            {"object_type": "program", "object_id": str(program_id), "revision_id": None},
            {
                "object_type": "program_version",
                "object_id": str(version_id),
                "revision_id": str(version_id),
            },
        ]
        if supersedes:
            body["change_plan"].append(
                {
                    "object_type": "program_version",
                    "object_id": str(supersedes),
                    "revision_id": None,
                }
            )
        draft_id, revision_no = _target if _target is not None else (uuid4(), 1)
        revision_id = uuid4()
        digest = digest_payload(body)
        if _target is None:
            self.session.execute(
                text(
                    "INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at) VALUES (:draft,'program',:profile,'open',:revision,:expires,:now)"
                ),
                {
                    "draft": draft_id,
                    "profile": profile_id,
                    "revision": revision_id,
                    "expires": now + timedelta(hours=24),
                    "now": now,
                },
            )
        else:
            self.session.execute(
                text(
                    "UPDATE trainer.drafts SET latest_revision_id=:revision WHERE draft_id=:draft"
                ),
                {"revision": revision_id, "draft": draft_id},
            )
        self.session.execute(
            text(
                "INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,content_digest,trust_state,confirmation_kind,created_at) VALUES (:revision,:draft,:number,CAST(:payload AS jsonb),:digest,'draft','normal',:now)"
            ),
            {
                "revision": revision_id,
                "draft": draft_id,
                "number": revision_no,
                "payload": json.dumps(body),
                "digest": digest,
                "now": now,
            },
        )
        dependencies = [
            self._add_dependency(
                revision_id,
                "goal",
                UUID(str(goal["goal_id"])),
                UUID(str(body["goal_revision_id"])),
                "exact program goal",
                now,
            )
        ]
        profile_revision = self.session.execute(
            text("SELECT current_revision_id FROM trainer.profiles WHERE profile_id=:id"),
            {"id": profile_id},
        ).scalar_one()
        dependencies.append(
            self._add_dependency(
                revision_id,
                "profile",
                profile_id,
                profile_revision,
                "exact applicability profile",
                now,
            )
        )
        if supersedes:
            dependencies.append(
                self._add_program_dependency(revision_id, program_id, UUID(str(supersedes)), now)
            )
        for item in body["equipment_inventory_snapshot"]:
            dependencies.append(
                self._add_dependency(
                    revision_id,
                    "equipment_item",
                    UUID(item["ref"]["object_id"]),
                    UUID(item["ref"]["revision_id"]),
                    "exact current global inventory revision",
                    now,
                )
            )
        for dep in body.get("evidence_dependencies", []):
            dependencies.append(self._insert_evidence_dependency(revision_id, dep, now))
        seen_exercises: set[tuple[str, str]] = set()
        for day in body["version"]["templates"]:
            for block in day["blocks"]:
                for item in block["exercises"]:
                    ref = item["exercise_ref"]
                    if (ref["object_id"], ref["revision_id"]) in seen_exercises:
                        continue
                    seen_exercises.add((ref["object_id"], ref["revision_id"]))
                    dependencies.append(
                        self._add_dependency(
                            revision_id,
                            "exercise",
                            UUID(str(ref["object_id"])),
                            UUID(str(ref["revision_id"])),
                            "exact exercise revision",
                            now,
                        )
                    )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": revision_no,
            "content_digest": digest,
            "dependencies": dependencies,
            "gaps": body["gaps"],
            "preview_candidate": {
                "summary": "Технический черновик программы готов к предпросмотру",
                "confirmation_kind": "normal",
                "complete": not body["gaps"],
            },
        }
        self._finish(operation_id, response)
        self._audit(
            operation_id,
            method,
            correlation_id,
            causation_id,
            key,
            [{"object_type": "draft", "object_id": str(draft_id), "revision_id": str(revision_id)}],
        )
        return response

    def revise_program_draft(
        self,
        *,
        draft_id: UUID,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-19", key, payload["request_digest"])
        if replay is not None:
            if replay["draft_id"] != str(draft_id):
                raise ContextRepositoryError(
                    "idempotency_conflict", "key is bound to another draft", 409
                )
            self._audit_replay(operation_id, "CMD-19", correlation_id, causation_id, key, replay)
            return replay
        if payload.get("changes", [{}])[0].get("path") == "/report":
            from trainer.infrastructure.db.workouts import WorkoutRepository

            return WorkoutRepository(self).revise_draft(
                draft_id=draft_id,
                payload=payload,
                key=key,
                correlation_id=correlation_id,
                causation_id=causation_id,
                operation_id=operation_id,
            )
        row = (
            self.session.execute(
                text(
                    "SELECT d.state,d.expires_at,dr.* FROM trainer.drafts d JOIN trainer.draft_revisions dr ON dr.draft_revision_id=d.latest_revision_id WHERE d.draft_id=:id AND d.intent_type='program' FOR UPDATE"
                ),
                {"id": draft_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "program draft not found", 404)
        if row["state"] != "open" or row["expires_at"] <= datetime.now(UTC):
            raise ContextRepositoryError("already_superseded", "draft is not open", 409)
        if str(row["draft_revision_id"]) != str(payload["base_revision_id"]):
            raise ContextRepositoryError("revision_conflict", "draft base revision changed", 409)
        original = row["typed_payload"]
        revised = {
            k: original["source_input"].get(k)
            for k in (
                "intent",
                "program_id",
                "expected_current_version_id",
                "goal_revision_id",
                "program",
                "version",
            )
        }
        if revised["intent"] == "create":
            revised["program_id"] = None
        paths: set[str] = set()
        for change in payload["changes"]:
            path = change["path"]
            if path in paths or path == "/program" and revised["intent"] != "create":
                raise ContextRepositoryError(
                    "validation_error", "replacement path is forbidden or duplicated", 400
                )
            paths.add(path)
            revised[path[1:]] = change["value"]
        revised.update(context=payload["context"], request_digest=payload["request_digest"])
        result = self.create_program_draft(
            payload=revised,
            key=key,
            correlation_id=correlation_id,
            causation_id=causation_id,
            _target=(draft_id, row["revision_no"] + 1),
            _operation=operation_id,
        )
        self.session.execute(
            text(
                "UPDATE trainer.drafts d SET state='cancelled' FROM trainer.draft_revisions dr WHERE d.latest_revision_id=dr.draft_revision_id AND d.intent_type='program_publication' AND d.state='open' AND dr.typed_payload->>'source_draft_id'=:source"
            ),
            {"source": str(draft_id)},
        )
        return result

    def create_program_publication_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-04", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-04", correlation_id, causation_id, key, replay)
            return replay
        source = (
            self.session.execute(
                text(
                    "SELECT d.draft_id,d.owner_profile_id,d.state,d.expires_at,dr.* FROM trainer.drafts d JOIN trainer.draft_revisions dr ON dr.draft_revision_id=d.latest_revision_id WHERE d.draft_id=:draft AND d.intent_type='program' FOR UPDATE"
                ),
                {"draft": payload["source_draft_id"], "revision": payload["source_revision_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if source is not None and source["state"] == "confirmed":
            source_content = source["typed_payload"]
            current_published = self.session.execute(
                text("SELECT current_version_id FROM trainer.programs WHERE program_id=:id"),
                {"id": source_content["program_id"]},
            ).scalar_one_or_none()
            if str(current_published) == source_content["program_version_id"]:
                raise ContextRepositoryError(
                    "publication_already_current", "source version is already current", 409
                )
        if source is None or source["state"] != "open":
            raise ContextRepositoryError(
                "source_draft_not_open", "source program draft is not open", 409
            )
        if str(source["draft_revision_id"]) != str(payload["source_revision_id"]):
            raise ContextRepositoryError(
                "source_revision_not_current", "source program revision changed", 409
            )
        typed = dict(source["typed_payload"])
        if source["expires_at"] <= datetime.now(UTC):
            raise ContextRepositoryError(
                "source_draft_not_open", "source program draft expired", 409
            )
        if any(g["disposition"] == "block" for g in typed.get("gaps", [])):
            raise ContextRepositoryError(
                "validation_error", "program draft has blocking evidence gaps", 400
            )
        try:
            self._check_dependencies(source["draft_revision_id"])
        except ContextRepositoryError as error:
            if error.code == "stale_preview":
                raise ContextRepositoryError(
                    "stale_dependency", "source dependencies changed", 409
                ) from error
            raise
        if str(typed.get("expected_current_version_id")) != str(
            payload.get("expected_current_version_id")
        ):
            raise ContextRepositoryError(
                "stale_dependency", "expected current version differs", 409
            )
        body = {
            "publication_source": typed,
            "inventory_snapshot_version": typed["inventory_snapshot_version"],
            "equipment_inventory_snapshot": typed["equipment_inventory_snapshot"],
            "equipment_inventory_digest": typed["equipment_inventory_digest"],
            "effective_equipment_types": typed["effective_equipment_types"],
            "source_draft_id": str(source["draft_id"]),
            "source_revision_id": str(source["draft_revision_id"]),
            "publication_reason": payload.get("publication_reason"),
            "change_plan": typed["change_plan"],
            "gaps": [],
        }
        now = datetime.now(UTC)
        draft_id, revision_id = uuid4(), uuid4()
        digest = digest_payload(body)
        self.session.execute(
            text(
                "INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at) VALUES (:draft,'program_publication',:profile,'open',:revision,:expires,:now)"
            ),
            {
                "draft": draft_id,
                "profile": source["owner_profile_id"],
                "revision": revision_id,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text(
                "INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,content_digest,trust_state,confirmation_kind,created_at) VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','publication',:now)"
            ),
            {
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(body),
                "digest": digest,
                "now": now,
            },
        )
        deps = []
        for dep in self.session.execute(
            text(
                "SELECT dependency_type,dependency_object_id,dependency_revision_id,reason FROM trainer.draft_dependencies WHERE draft_revision_id=:id"
            ),
            {"id": source["draft_revision_id"]},
        ).mappings():
            if dep["dependency_type"] == "program_version":
                deps.append(
                    self._add_program_dependency(
                        revision_id, dep["dependency_object_id"], dep["dependency_revision_id"], now
                    )
                )
            elif dep["dependency_type"] in (
                "conclusion_version",
                "derived_value",
                "equipment_availability",
            ):
                original = (
                    self.session.execute(
                        text(
                            "SELECT dependency_digest,reason FROM trainer.draft_dependencies WHERE draft_revision_id=:id AND dependency_type=:type AND dependency_object_id=:object AND dependency_revision_id=:revision"
                        ),
                        {
                            "id": source["draft_revision_id"],
                            "type": dep["dependency_type"],
                            "object": dep["dependency_object_id"],
                            "revision": dep["dependency_revision_id"],
                        },
                    )
                    .mappings()
                    .one()
                )
                deps.append(
                    self._insert_evidence_dependency(
                        revision_id,
                        {
                            "dependency_type": dep["dependency_type"],
                            "object_id": str(dep["dependency_object_id"]),
                            "revision_id": str(dep["dependency_revision_id"]),
                            "digest": original["dependency_digest"],
                            "reason": original["reason"],
                        },
                        now,
                    )
                )
            else:
                deps.append(
                    self._add_dependency(
                        revision_id,
                        dep["dependency_type"]
                        if dep["dependency_type"] != "exercise_revision"
                        else "exercise",
                        dep["dependency_object_id"],
                        dep["dependency_revision_id"],
                        dep["reason"],
                        now,
                    )
                )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": deps,
            "gaps": [],
            "preview_candidate": {
                "summary": "Публикация программы готова к полному предпросмотру",
                "confirmation_kind": "publication",
                "complete": True,
            },
        }
        self._finish(operation_id, response)
        self._audit(
            operation_id,
            "CMD-04",
            correlation_id,
            causation_id,
            key,
            [{"object_type": "draft", "object_id": str(draft_id), "revision_id": str(revision_id)}],
        )
        return response

    def create_preview(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-20", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation_id, "CMD-20", correlation_id, causation_id, key, replay)
            return replay
        row = (
            self.session.execute(
                text("""
            SELECT dr.draft_revision_id,dr.typed_payload,dr.content_digest,dr.confirmation_kind,d.state,d.expires_at
            FROM trainer.draft_revisions dr JOIN trainer.drafts d ON d.draft_id=dr.draft_id
            WHERE dr.draft_revision_id=:revision AND d.latest_revision_id=dr.draft_revision_id
        """),
                {"revision": payload["revision_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "draft revision not found", 404)
        if row["state"] != "open" or row["expires_at"] <= datetime.now(UTC):
            raise ContextRepositoryError("already_superseded", "draft is not open", 409)
        canonical_payload = dict(row["typed_payload"])
        # A short request may render readable text, but it must remain
        # incomplete and cannot authorize a shown/confirm transition.
        detail_level = payload.get("detail_level", "full")
        dependencies = [
            {
                "dependency_type": item["dependency_type"],
                "object_id": str(item["object_id"]),
                "revision_id": str(item["revision_id"]),
                "digest": item["digest"],
                "reason": item["reason"],
            }
            for item in self.session.execute(
                text("""
                    SELECT dependency_type,dependency_object_id AS object_id,
                           dependency_revision_id AS revision_id,
                           dependency_digest AS digest,reason
                    FROM trainer.draft_dependencies
                    WHERE draft_revision_id=:revision
                    ORDER BY dependency_type,dependency_object_id,dependency_revision_id
                """),
                {"revision": row["draft_revision_id"]},
            ).mappings()
        ]
        # Keep the persisted canonical payload untouched. The preview digest
        # binds the readable text and the exact dependency snapshot; exercise
        # previews need not print internal dependency identifiers.
        presentation_payload = {**canonical_payload, "dependencies": dependencies}
        canonical_text = self._render_preview(presentation_payload, detail_level=detail_level)
        gaps = canonical_payload.get("gaps", [])
        if canonical_payload.get("intent") == "workout_report":
            is_complete = detail_level == "full" and all(
                gap.get("disposition") == "leave_unknown" for gap in gaps
            )
        else:
            is_complete = detail_level == "full" and not gaps
        preview_digest = digest_payload(
            {
                "revision_id": str(row["draft_revision_id"]),
                "canonical_payload": canonical_payload,
                "canonical_text": canonical_text,
                "dependencies": dependencies,
                "confirmation_kind": row["confirmation_kind"],
                "detail_level": detail_level,
            }
        )
        preview_id = uuid4()
        now = datetime.now(UTC)
        self.session.execute(
            text("""
            INSERT INTO trainer.previews(preview_id,draft_revision_id,canonical_payload,canonical_text,
                content_digest,conversation_id,detail_level,is_complete,created_at)
            VALUES (:id,:revision,CAST(:payload AS jsonb),:text,:digest,:conversation,:detail,:complete,:now)
        """),
            {
                "id": preview_id,
                "revision": row["draft_revision_id"],
                "payload": json.dumps(canonical_payload),
                "text": canonical_text,
                "digest": preview_digest,
                "conversation": payload["context"]["conversation_id"],
                "detail": detail_level,
                "complete": is_complete,
                "now": now,
            },
        )
        response = {
            "preview_id": str(preview_id),
            "revision_id": str(row["draft_revision_id"]),
            "conversation_id": payload["context"]["conversation_id"],
            "digest": preview_digest,
            "confirmation_kind": row["confirmation_kind"],
            "complete": is_complete,
            "canonical_payload": canonical_payload,
            "canonical_text": canonical_text,
            "changed_objects": canonical_payload["change_plan"],
            "dependencies": dependencies,
            "gaps": canonical_payload.get("gaps", []),
            "shown_message_id": None,
            "shown_at": None,
            "expires_at": row["expires_at"].isoformat(),
        }
        self._finish(operation_id, response)
        self._audit(
            operation_id,
            "CMD-20",
            correlation_id,
            causation_id,
            key,
            [{"object_type": "preview", "object_id": str(preview_id)}],
        )
        return response

    def mark_preview_shown(
        self,
        *,
        preview_id: UUID,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-20", key, payload["request_digest"])
        if replay is not None:
            if str(replay.get("preview_id")) != str(preview_id):
                raise ContextRepositoryError(
                    "idempotency_conflict", "idempotency key is bound to another preview", 409
                )
            self._audit_replay(operation_id, "CMD-20", correlation_id, causation_id, key, replay)
            return replay
        row = (
            self.session.execute(
                text("""
            SELECT p.*,d.expires_at,d.state,d.latest_revision_id FROM trainer.previews p
            JOIN trainer.draft_revisions dr ON dr.draft_revision_id=p.draft_revision_id
            JOIN trainer.drafts d ON d.draft_id=dr.draft_id
                WHERE p.preview_id=:id FOR UPDATE OF d
        """),
                {"id": preview_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "preview not found", 404)
        if row["detail_level"] != "full":
            raise ContextRepositoryError("incomplete_preview", "preview must be full", 400)
        if (
            row["state"] != "open"
            or row["expires_at"] <= datetime.now(UTC)
            or row["latest_revision_id"] != row["draft_revision_id"]
        ):
            raise ContextRepositoryError("stale_preview", "preview is no longer current", 409)
        if (
            payload["shown_message_id"] != payload["context"]["message_id"]
            or row["content_digest"] != payload["preview_digest"]
            or row["draft_revision_id"] != UUID(str(payload["revision_id"]))
            or hashlib.sha256(row["canonical_text"].encode()).hexdigest()
            != payload["displayed_text_digest"]
            or payload["cross_dialog"]
            != (row["conversation_id"] != payload["context"]["conversation_id"])
        ):
            raise ContextRepositoryError(
                "stale_preview", "preview digest or conversation changed", 409
            )
        now = datetime.now(UTC)
        if row["shown_message_id"] is None:
            self.session.execute(
                text(
                    "UPDATE trainer.previews SET shown_message_id=:message,returned_as_shown_at=:now WHERE preview_id=:id"
                ),
                {
                    "message": payload["shown_message_id"],
                    "now": now,
                    "id": preview_id,
                },
            )
            if row["is_complete"]:
                self.session.execute(
                    text(
                        "UPDATE trainer.draft_revisions SET trust_state='shown' "
                        "WHERE draft_revision_id=:revision AND trust_state='draft'"
                    ),
                    {"revision": row["draft_revision_id"]},
                )
        self.session.execute(
            text("""
                INSERT INTO trainer.preview_show_events(
                    show_event_id,preview_id,draft_revision_id,preview_digest,
                    displayed_text_digest,conversation_id,shown_message_id,cross_dialog,
                    shown_at,created_at
                ) VALUES (:id,:preview,:revision,:preview_digest,:displayed_digest,
                    :conversation,:message,:cross_dialog,:now,:now)
                ON CONFLICT (preview_id,conversation_id,shown_message_id) DO NOTHING
            """),
            {
                "id": uuid4(),
                "preview": preview_id,
                "revision": row["draft_revision_id"],
                "preview_digest": row["content_digest"],
                "displayed_digest": payload["displayed_text_digest"],
                "conversation": payload["context"]["conversation_id"],
                "message": payload["shown_message_id"],
                "cross_dialog": payload["cross_dialog"],
                "now": now,
            },
        )
        response = self._load_preview_response(
            preview_id, conversation_id=payload["context"]["conversation_id"]
        )
        self._finish(operation_id, response)
        self._audit(
            operation_id,
            "CMD-20",
            correlation_id,
            causation_id,
            key,
            [{"object_type": "preview", "object_id": str(preview_id)}],
        )
        return response

    def _load_preview_response(
        self, preview_id: UUID, *, conversation_id: str | None = None
    ) -> dict[str, Any]:
        row = (
            self.session.execute(
                text("""
                SELECT p.*,dr.confirmation_kind,d.expires_at FROM trainer.previews p
                JOIN trainer.draft_revisions dr ON dr.draft_revision_id=p.draft_revision_id
                JOIN trainer.drafts d ON d.draft_id=dr.draft_id
                WHERE p.preview_id=:id
            """),
                {"id": preview_id},
            )
            .mappings()
            .one()
        )
        dependencies = [
            {
                "dependency_type": item["dependency_type"],
                "object_id": str(item["object_id"]),
                "revision_id": str(item["revision_id"]),
                "digest": item["digest"],
                "reason": item["reason"],
            }
            for item in self.session.execute(
                text("""
                    SELECT dependency_type,dependency_object_id AS object_id,
                           dependency_revision_id AS revision_id,
                           dependency_digest AS digest,reason
                    FROM trainer.draft_dependencies
                    WHERE draft_revision_id=:revision
                    ORDER BY dependency_type,dependency_object_id,dependency_revision_id
                """),
                {"revision": row["draft_revision_id"]},
            ).mappings()
        ]
        canonical_payload = dict(row["canonical_payload"])
        show_event = None
        if conversation_id is not None:
            show_event = (
                self.session.execute(
                    text("""
                        SELECT conversation_id,shown_message_id,shown_at
                        FROM trainer.preview_show_events
                        WHERE preview_id=:preview AND conversation_id=:conversation
                        ORDER BY shown_at DESC,show_event_id DESC LIMIT 1
                    """),
                    {"preview": preview_id, "conversation": conversation_id},
                )
                .mappings()
                .one_or_none()
            )
        return {
            "preview_id": str(row["preview_id"]),
            "revision_id": str(row["draft_revision_id"]),
            "conversation_id": show_event["conversation_id"]
            if show_event
            else row["conversation_id"],
            "digest": row["content_digest"],
            "confirmation_kind": row["confirmation_kind"],
            "complete": row["is_complete"],
            "canonical_payload": canonical_payload,
            "canonical_text": row["canonical_text"],
            "changed_objects": canonical_payload["change_plan"],
            "dependencies": dependencies,
            "gaps": canonical_payload.get("gaps", []),
            "shown_message_id": show_event["shown_message_id"]
            if show_event
            else row["shown_message_id"],
            "shown_at": show_event["shown_at"].isoformat()
            if show_event is not None
            else row["returned_as_shown_at"].isoformat()
            if row["returned_as_shown_at"] is not None
            else None,
            "expires_at": row["expires_at"].isoformat(),
        }

    def confirm(
        self,
        *,
        preview_id: UUID,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
        fail_before_commit: bool = False,
    ) -> dict[str, Any]:
        operation_id, replay = self._reserve("CMD-00", key, payload["request_digest"])
        if replay is not None:
            if str(replay.get("_confirmed_preview_id")) != str(preview_id):
                raise ContextRepositoryError(
                    "idempotency_conflict", "idempotency key is bound to another preview", 409
                )
            self._audit_replay(operation_id, "CMD-00", correlation_id, causation_id, key, replay)
            replay.pop("_confirmed_preview_id", None)
            replay["status"] = "already_committed"
            return replay
        row = (
            self.session.execute(
                text("""
            SELECT p.*,dr.typed_payload,dr.trust_state,dr.confirmation_kind,d.intent_type,d.draft_id,d.state,d.owner_profile_id,d.expires_at,d.latest_revision_id
            FROM trainer.previews p JOIN trainer.draft_revisions dr ON dr.draft_revision_id=p.draft_revision_id
            JOIN trainer.drafts d ON d.draft_id=dr.draft_id WHERE p.preview_id=:id FOR UPDATE OF d
        """),
                {"id": preview_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "preview not found", 404)
        if row["intent_type"] == "program":
            error = ContextRepositoryError(
                "confirmation_not_supported", "technical program draft cannot be confirmed", 409
            )
            error.required_next_action = "prepare_program_publication"
            raise error
        if row["latest_revision_id"] != row["draft_revision_id"]:
            raise ContextRepositoryError("stale_preview", "preview is no longer current", 409)
        if payload["confirmation_kind"] != row["confirmation_kind"]:
            raise ContextRepositoryError(
                "ambiguous_confirmation", "confirmation kind differs from preview", 400
            )
        if (
            row["draft_revision_id"] != UUID(str(payload["revision_id"]))
            or row["content_digest"] != payload["preview_digest"]
        ):
            raise ContextRepositoryError(
                "stale_preview", "preview no longer matches requested revision", 409
            )
        if (
            row["shown_message_id"] is None
            or row["trust_state"] != "shown"
            or not row["is_complete"]
        ):
            raise ContextRepositoryError("incomplete_preview", "preview must be shown in full", 400)
        now = datetime.now(UTC)
        same_dialog = payload["confirmation_mode"] == "same_dialog"
        show_event = (
            self.session.execute(
                text("""
                    SELECT conversation_id,shown_message_id,cross_dialog,shown_at
                    FROM trainer.preview_show_events
                    WHERE preview_id=:preview AND conversation_id=:conversation
                    ORDER BY shown_at DESC,show_event_id DESC LIMIT 1
                """),
                {
                    "preview": preview_id,
                    "conversation": payload["context"]["conversation_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        same_dialog_valid = (
            row["conversation_id"] == payload["context"]["conversation_id"]
            and show_event is not None
            and not show_event["cross_dialog"]
            and payload.get("referenced_preview_id") is None
            and payload.get("reshown_message_id") is None
        )
        cross_dialog_valid = (
            not same_dialog
            and show_event is not None
            and show_event["cross_dialog"]
            and str(payload.get("referenced_preview_id")) == str(preview_id)
            and payload.get("reshown_message_id") == show_event["shown_message_id"]
        )
        if (
            (same_dialog and not same_dialog_valid)
            or (not same_dialog and not cross_dialog_valid)
            or show_event is None
            or payload["confirmation_message_id"] != payload["context"]["message_id"]
        ):
            raise ContextRepositoryError(
                "ambiguous_confirmation", "confirmation must follow the shown preview", 400
            )
        if row["state"] != "open" or row["expires_at"] <= datetime.now(UTC):
            raise ContextRepositoryError("already_superseded", "draft is not confirmable", 409)
        typed = dict(row["typed_payload"])
        if typed.get("correction"):
            current_revision = self.session.execute(
                text("""
                SELECT current_revision_id FROM trainer.workout_sessions
                WHERE session_id=:session FOR UPDATE
            """),
                {"session": typed["session_id"]},
            ).scalar_one_or_none()
            if str(current_revision) != typed["correction"]["source_revision_id"]:
                raise ContextRepositoryError(
                    "stale_preview", "correction target is no longer current", 409
                )
            self.session.execute(
                text("""
                SELECT session_revision_id FROM trainer.workout_session_revisions
                WHERE session_revision_id=:source
            """),
                {"source": typed["correction"]["source_revision_id"]},
            ).scalar_one()
            current_effects = [
                str(item)
                for item in self.session.execute(
                    text("""
                SELECT DISTINCT d.derived_value_id FROM trainer.derived_values d
                JOIN trainer.derived_value_inputs i ON i.derived_value_id=d.derived_value_id
                WHERE d.state='current' AND i.source_object_type='workout_session'
                  AND i.source_object_id=:session AND i.source_revision_id=:source
                ORDER BY d.derived_value_id
            """),
                    {
                        "session": typed["session_id"],
                        "source": typed["correction"]["source_revision_id"],
                    },
                ).scalars()
            ]
            shown_effects = sorted(
                item["object_id"] for item in typed.get("downstream_effects", [])
            )
            if sorted(current_effects) != shown_effects:
                raise ContextRepositoryError("stale_preview", "correction effects changed", 409)
            current_candidates = sorted(
                str(item)
                for item in self.session.execute(
                    text("""SELECT candidate_id FROM trainer.record_candidates
                           WHERE session_revision_id=:source AND status='pending'
                           ORDER BY candidate_id"""),
                    {"source": typed["correction"]["source_revision_id"]},
                ).scalars()
            )
            current_source_records = sorted(
                str(item)
                for item in self.session.execute(
                    text(
                        "SELECT record_id FROM trainer.actual_records WHERE session_id=:session ORDER BY record_id"
                    ),
                    {"session": typed["session_id"]},
                ).scalars()
            )
            current_accepted_candidates = sorted(
                str(item)
                for item in self.session.execute(
                    text("""
                    SELECT c.candidate_id FROM trainer.record_candidates c
                    JOIN trainer.actual_records r ON r.candidate_id=c.candidate_id
                    WHERE r.session_id=:session AND c.status='accepted'
                    ORDER BY c.candidate_id
                    """),
                    {"session": typed["session_id"]},
                ).scalars()
            )
            if (
                current_candidates
                != typed.get("record_effects", {}).get("pending_candidates_to_invalidate", [])
                or current_source_records
                != typed.get("record_effects", {}).get("source_records_to_revalidate", [])
                or current_accepted_candidates
                != typed.get("record_effects", {}).get("accepted_candidates_to_reconcile", [])
            ):
                raise ContextRepositoryError(
                    "stale_preview", "record correction effects changed", 409
                )
            from trainer.infrastructure.db.workouts import WorkoutRepository

            correction_plan = typed["record_effects"].get("preview_plan", [])
            record_group_locks: set[str] = set()
            for item in correction_plan:
                if item.get("outcome") == "recompute_current":
                    normalized_key = self.session.execute(
                        text("""
                            SELECT normalized_key FROM trainer.comparison_conditions
                            WHERE condition_id=:id
                        """),
                        {"id": item["condition_id"]},
                    ).scalar_one()
                    lock_identity = (item["exercise_id"], item["metric_kind"], normalized_key)
                elif "condition_key" in item:
                    lock_identity = (
                        item["exercise_id"],
                        item["metric_kind"],
                        item["condition_key"],
                    )
                else:
                    continue
                record_group_locks.add(":".join((str(typed["profile_id"]), *lock_identity)))
            for lock_key in sorted(record_group_locks):
                self.session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": lock_key},
                )
            fresh_record_plan = WorkoutRepository(self)._plan_record_correction(
                session_id=UUID(str(typed["session_id"])),
                old_revision=UUID(str(typed["correction"]["source_revision_id"])),
                before_tree=typed["correction"]["before"],
                after_tree={
                    "local_start_date": typed["report"]["local_start_date"],
                    "blocks": typed["report"]["blocks"],
                },
                report=typed["report"],
                record_effects=typed["record_effects"],
            )
            if fresh_record_plan != typed["record_effects"].get("preview_plan"):
                raise ContextRepositoryError(
                    "stale_dependency", "record consequences changed after preview", 409
                )
        self._check_dependencies(row["draft_revision_id"])
        changes = self._planned_changes(typed)
        confirmation_id, commit_result_id = uuid4(), uuid4()
        expected_invalidated_refs: list[dict[str, str | None]] = [
            self._ref(
                "derived_value",
                UUID(item["object_id"]),
                UUID(item["revision_id"]),
            )
            for item in typed.get("downstream_effects", [])
        ]
        self.session.execute(
            text("""
            INSERT INTO trainer.confirmations(confirmation_id,preview_id,draft_revision_id,
                conversation_id,user_message_id,kind,explicit_expression,confirmed_at,created_at)
            VALUES (:id,:preview,:revision,:conversation,:message,:kind,:expression,:now,:now)
        """),
            {
                "id": confirmation_id,
                "preview": preview_id,
                "revision": row["draft_revision_id"],
                "conversation": payload["context"]["conversation_id"],
                "message": payload["confirmation_message_id"],
                "expression": payload["explicit_intent"],
                "kind": row["confirmation_kind"],
                "now": now,
            },
        )
        response = {
            "commit_result_id": str(commit_result_id),
            "confirmation_id": str(confirmation_id),
            "operation_id": str(operation_id),
            "status": "committed",
            "domain_changes": changes,
            "created_revisions": [
                change for change in changes if change["revision_id"] is not None
            ],
            "invalidated_calculations": expected_invalidated_refs,
            "committed_at": now.isoformat(),
        }
        self.session.execute(
            text("""
            INSERT INTO trainer.commit_results(commit_result_id,confirmation_id,operation_id,
                result_payload,committed_at,created_at)
            VALUES (:id,:confirmation,:operation,CAST(:payload AS jsonb),:now,:now)
        """),
            {
                "id": commit_result_id,
                "confirmation": confirmation_id,
                "operation": operation_id,
                "payload": json.dumps(response),
                "now": now,
            },
        )
        if "plan_lifecycle" in typed:
            self._commit_plan_lifecycle(typed, confirmation_id, commit_result_id, changes, now)
        elif "publication_source" in typed:
            self._commit_program(typed, confirmation_id, commit_result_id, changes, now)
        elif "plan" in typed:
            self._commit_plan(typed, confirmation_id, commit_result_id, changes, now)
        elif "report" in typed:
            from trainer.infrastructure.db.workouts import WorkoutRepository

            invalidated = WorkoutRepository(self).commit(
                typed, confirmation_id, commit_result_id, changes, now
            )
            expected_invalidated = sorted(
                str(item["object_id"]) for item in expected_invalidated_refs
            )
            if sorted(str(item) for item in invalidated) != expected_invalidated:
                raise ContextRepositoryError("stale_preview", "correction effects changed", 409)
        elif typed.get("intent") in ("record_decision", "historical_record"):
            self._commit_record_intent(typed, confirmation_id, commit_result_id, changes, now)
        elif "inventory_change" in typed:
            from trainer.infrastructure.db.inventory import InventoryRepository

            InventoryRepository(self).commit(typed, confirmation_id, commit_result_id, changes, now)
        elif "measurement_change" in typed:
            from trainer.infrastructure.db.measurements import MeasurementRepository

            MeasurementRepository(self).commit(
                typed, confirmation_id, commit_result_id, changes, now
            )
        elif typed.get("intent") == "familiar_batch":
            from trainer.infrastructure.db.familiar_batch import FamiliarBatchRepository

            FamiliarBatchRepository(self).commit(
                typed, confirmation_id, commit_result_id, changes, now
            )
        elif "profile_id" in typed:
            self._commit_onboarding(typed, confirmation_id, commit_result_id, changes, now)
        elif typed.get("intent") in ("create", "correct") and "exercise" in typed:
            self._commit_exercise(typed, confirmation_id, commit_result_id, changes, now)
        else:
            self._commit_goal(
                typed, row["owner_profile_id"], confirmation_id, commit_result_id, changes, now
            )
        if fail_before_commit:
            raise RuntimeError("synthetic fault before transaction commit")
        self.session.execute(
            text("UPDATE trainer.drafts SET state='confirmed' WHERE draft_id=:id"),
            {"id": row["draft_id"]},
        )
        self.session.execute(
            text("""
            UPDATE trainer.idempotent_operations SET state='committed',commit_result_id=:result,
                response_payload=CAST(:payload AS jsonb),updated_at=:now WHERE operation_id=:operation
        """),
            {
                "result": commit_result_id,
                "payload": json.dumps({**response, "_confirmed_preview_id": str(preview_id)}),
                "now": now,
                "operation": operation_id,
            },
        )
        self._audit(
            operation_id,
            "CMD-00",
            correlation_id,
            causation_id,
            key,
            [{"object_type": c["object_type"], "object_id": c["object_id"]} for c in changes],
        )
        return response

    def get_profile_context(self, *, effective_at: datetime) -> dict[str, Any]:
        profile = (
            self.session.execute(text("SELECT * FROM trainer.v_current_profile"))
            .mappings()
            .one_or_none()
        )
        if profile is None:
            return {
                "profile_ref": None,
                "profile": None,
                "goals": [],
                "places": [],
                "equipment": [],
                "familiar_exercises": [],
                "effective_at": effective_at.isoformat(),
                "gaps": [
                    {
                        "path": "profile",
                        "code": "missing",
                        "disposition": "clarify",
                        "message_safe": "profile is not yet confirmed",
                    }
                ],
            }
        profile_id = profile["profile_id"]
        profile_payload = {
            key: profile[key]
            for key in (
                "birth_date",
                "sex",
                "timezone",
                "started_on",
                "desired_sessions_per_week",
                "usual_place_id",
                "comment",
            )
        }
        goals = []
        for item in self.session.execute(
            text(
                "SELECT * FROM trainer.v_current_goals WHERE profile_id=:id ORDER BY priority,goal_id"
            ),
            {"id": profile_id},
        ).mappings():
            goal_payload = {
                key: item[key]
                for key in (
                    "goal_id",
                    "parent_goal_id",
                    "name",
                    "description",
                    "goal_type",
                    "start_value",
                    "target_value",
                    "unit_code",
                    "set_on",
                    "target_date",
                    "priority",
                    "status",
                    "comment",
                )
            }
            goals.append(
                {
                    "ref": self._ref("goal", item["goal_id"], item["goal_revision_id"]),
                    "goal": goal_payload,
                }
            )
        places = []
        for item in self.session.execute(
            text("SELECT * FROM trainer.v_current_places WHERE profile_id=:id ORDER BY place_id"),
            {"id": profile_id},
        ).mappings():
            place_payload = {
                key: item[key] for key in ("place_id", "name", "place_type", "status", "comment")
            }
            places.append(
                {
                    "ref": self._ref("training_place", item["place_id"], item["place_revision_id"]),
                    "place": place_payload,
                }
            )
        from trainer.infrastructure.db.inventory import InventoryRepository

        equipment = InventoryRepository(self).current(profile_id)
        familiar_exercises = []
        for item in self.session.execute(
            text("""
                SELECT f.exercise_id,f.exercise_revision_id,r.canonical_name,
                       (e.current_revision_id=f.exercise_revision_id) AS current
                FROM trainer.profile_familiar_exercises f
                JOIN trainer.exercise_revisions r
                  ON r.exercise_id=f.exercise_id
                 AND r.exercise_revision_id=f.exercise_revision_id
                JOIN trainer.exercises e ON e.exercise_id=f.exercise_id
                WHERE f.profile_revision_id=:revision
                ORDER BY r.canonical_name,f.exercise_id
            """),
            {"revision": profile["profile_revision_id"]},
        ).mappings():
            familiar_exercises.append(
                {
                    "exercise_ref": self._ref(
                        "exercise", item["exercise_id"], item["exercise_revision_id"]
                    ),
                    "name": item["canonical_name"],
                    "current": item["current"],
                }
            )
        return {
            "profile_ref": self._ref("profile", profile_id, profile["profile_revision_id"]),
            "profile": profile_payload,
            "goals": goals,
            "places": places,
            "equipment": equipment,
            "familiar_exercises": familiar_exercises,
            "effective_at": effective_at.isoformat(),
            "gaps": [],
        }

    def get_program(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        program_id = UUID(str(payload["program_id"]))
        program = (
            self.session.execute(
                text(
                    "SELECT * FROM trainer.programs WHERE program_id=:id AND archived_at IS NULL AND logically_deleted_at IS NULL"
                ),
                {"id": program_id},
            )
            .mappings()
            .one_or_none()
        )
        if program is None:
            raise ContextRepositoryError("not_found", "program not found", 404)
        version_id = (
            program["current_version_id"]
            if payload["mode"] == "current"
            else UUID(str(payload["program_version_id"]))
        )
        version = (
            self.session.execute(
                text(
                    "SELECT * FROM trainer.program_versions WHERE program_id=:program AND program_version_id=:version"
                ),
                {"program": program_id, "version": version_id},
            )
            .mappings()
            .one_or_none()
        )
        if version is None:
            raise ContextRepositoryError("not_found", "program not found", 404)
        templates = []
        for day in self.session.execute(
            text(
                "SELECT * FROM trainer.day_templates WHERE program_version_id=:id ORDER BY position"
            ),
            {"id": version_id},
        ).mappings():
            blocks = []
            for block in self.session.execute(
                text(
                    "SELECT * FROM trainer.template_blocks WHERE day_template_id=:id ORDER BY position"
                ),
                {"id": day["day_template_id"]},
            ).mappings():
                exercises = []
                for ex in self.session.execute(
                    text(
                        "SELECT * FROM trainer.template_exercises WHERE template_block_id=:id ORDER BY position"
                    ),
                    {"id": block["template_block_id"]},
                ).mappings():
                    sets = []
                    for item in self.session.execute(
                        text(
                            "SELECT * FROM trainer.template_sets WHERE template_exercise_id=:id ORDER BY position"
                        ),
                        {"id": ex["template_exercise_id"]},
                    ).mappings():
                        metrics = [
                            {
                                **self._fields(
                                    r,
                                    "metric_kind",
                                    "exact_value",
                                    "min_value",
                                    "max_value",
                                    "unit_code",
                                ),
                                "metric_target_id": str(r["planned_metric_value_id"]),
                            }
                            for r in self.session.execute(
                                text(
                                    "SELECT * FROM trainer.planned_metric_values WHERE template_set_id=:id ORDER BY metric_kind"
                                ),
                                {"id": item["template_set_id"]},
                            ).mappings()
                        ]
                        sets.append(
                            {
                                **self._fields(
                                    item,
                                    "template_set_id",
                                    "position",
                                    "round_no",
                                    "logical_set_no",
                                    "side",
                                    "tempo",
                                    "pause_seconds",
                                    "rest_after_seconds",
                                ),
                                "set_type_code": item["set_type"],
                                "metrics": metrics,
                            }
                        )
                    exercises.append(
                        {
                            **self._fields(
                                ex,
                                "template_exercise_id",
                                "position",
                                "circuit_position",
                                "comment",
                                "equipment_snapshot",
                            ),
                            "exercise_ref": self._ref(
                                "exercise", ex["exercise_id"], ex["exercise_revision_id"]
                            ),
                            "sets": sets,
                        }
                    )
                blocks.append(
                    {
                        **self._fields(
                            block,
                            "template_block_id",
                            "position",
                            "name",
                            "declared_rounds",
                            "rest_seconds",
                            "comment",
                        ),
                        "block_type_code": block["block_type"],
                        "exercises": exercises,
                    }
                )
            templates.append(
                {
                    **self._fields(
                        day, "day_template_id", "position", "purpose", "equipment_needs"
                    ),
                    "day_type_code": day["day_type"],
                    "blocks": blocks,
                }
            )
        recommendations = []
        for rec in self.session.execute(
            text(
                "SELECT * FROM trainer.recommendations WHERE program_version_id=:id ORDER BY recommendation_id"
            ),
            {"id": version_id},
        ).mappings():
            explanations = {
                r["detail_level"]: r["content"]
                for r in self.session.execute(
                    text(
                        "SELECT detail_level,content FROM trainer.recommendation_explanations WHERE recommendation_id=:id"
                    ),
                    {"id": rec["recommendation_id"]},
                ).mappings()
            }
            recommendations.append(
                {
                    **self._fields(
                        rec,
                        "recommendation_id",
                        "recommendation_kind",
                        "decision_text",
                        "confidence",
                        "assumptions",
                        "alternatives",
                        "revisit_condition",
                    ),
                    "fact_links": [
                        {
                            **self._fields(r, "role", "reason", "snapshot_digest"),
                            "ref": self._ref(r["object_type"], r["object_id"], r["revision_id"]),
                        }
                        for r in self.session.execute(
                            text(
                                "SELECT * FROM trainer.recommendation_fact_links WHERE recommendation_id=:id ORDER BY object_type,object_id,revision_id,role"
                            ),
                            {"id": rec["recommendation_id"]},
                        ).mappings()
                    ],
                    "calculation_links": [
                        self._fields(r, "derived_value_id", "role")
                        for r in self.session.execute(
                            text(
                                "SELECT * FROM trainer.recommendation_calculation_links WHERE recommendation_id=:id ORDER BY derived_value_id,role"
                            ),
                            {"id": rec["recommendation_id"]},
                        ).mappings()
                    ],
                    "conclusion_links": [
                        {
                            **self._fields(
                                r,
                                "conclusion_version_id",
                                "role",
                                "applicability_assessment",
                                "status_at_decision",
                            ),
                            "status_revision_ref": self._ref(
                                "knowledge_status_revision",
                                r["status_revision_id"],
                                r["status_revision_id"],
                            ),
                        }
                        for r in self.session.execute(
                            text(
                                "SELECT * FROM trainer.recommendation_conclusion_links WHERE recommendation_id=:id ORDER BY conclusion_version_id,role"
                            ),
                            {"id": rec["recommendation_id"]},
                        ).mappings()
                    ],
                    "short_explanation": explanations.get("short"),
                    "detailed_explanation": explanations.get("detailed"),
                }
            )
        version_view = {
            **self._fields(
                version,
                "version_no",
                "version_label",
                "period_start",
                "period_end",
                "rationale",
                "publication_state",
                "published_at",
                "supersedes_version_id",
            ),
            "version_ref": self._ref("program_version", version_id, version_id),
            "goal_revision_ref": self._ref("goal", version["goal_id"], version["goal_revision_id"]),
            "templates": templates,
            "recommendations": recommendations,
        }
        version_view["content_digest"] = digest_payload(
            {
                k: v
                for k, v in version_view.items()
                if k not in {"publication_state", "published_at"}
            }
        )
        result = {
            "program": {
                "program_ref": self._ref("program", program_id, program["current_version_id"]),
                "profile_ref": self._ref(
                    "profile",
                    program["profile_id"],
                    self.session.execute(
                        text(
                            "SELECT current_revision_id FROM trainer.profiles WHERE profile_id=:id"
                        ),
                        {"id": program["profile_id"]},
                    ).scalar_one(),
                ),
                **self._fields(
                    program,
                    "goal_id",
                    "name",
                    "description",
                    "starts_on",
                    "ends_on",
                    "comment",
                    "current_version_id",
                ),
                "status_code": program["status"],
            },
            "version": version_view,
            "current": version_id == program["current_version_id"],
        }
        try:
            if (
                not templates
                or any(
                    not d["blocks"]
                    or any(
                        not b["exercises"]
                        or any(
                            not e["sets"] or any(not s["metrics"] for s in e["sets"])
                            for e in b["exercises"]
                        )
                        for b in d["blocks"]
                    )
                    for d in templates
                )
                or any(not r["short_explanation"] for r in recommendations)
            ):
                raise DomainValidationError("incomplete program tree")
            validate_program_draft(
                {
                    "intent": "create",
                    "program": {k: result["program"][k] for k in ("name", "starts_on", "ends_on")},
                    "version": version_view,
                }
            )
            for view_day in templates:
                expected_needs = []
                for view_block in view_day["blocks"]:
                    for exercise in view_block["exercises"]:
                        ref = exercise["exercise_ref"]
                        exact = self._load_exercise_aggregate(
                            UUID(ref["object_id"]), UUID(ref["revision_id"])
                        )
                        if exact is None:
                            raise DomainValidationError("missing exact exercise")
                        requirements = sorted(
                            exact["equipment_requirements"],
                            key=lambda r: (
                                r["equipment_type"],
                                r["optionality"],
                                r.get("alternative_group") or "",
                            ),
                        )
                        if exercise["equipment_snapshot"] != {"requirements": requirements}:
                            raise DomainValidationError(
                                "equipment snapshot differs from exact exercise"
                            )
                        expected_needs.extend(requirements)
                        specs = {m["metric_kind"]: m for m in exact["measurements"]}
                        for item in exercise["sets"]:
                            metric_kinds = {m["metric_kind"] for m in item["metrics"]}
                            if any(
                                m["requiredness"] == "required" and k not in metric_kinds
                                for k, m in specs.items()
                            ):
                                raise DomainValidationError("missing required metric")
                            for metric in item["metrics"]:
                                family = self.session.execute(
                                    text(
                                        "SELECT family_code FROM trainer.unit_family_mapping WHERE unit_code=:unit"
                                    ),
                                    {"unit": metric["unit_code"]},
                                ).scalar_one_or_none()
                                if (
                                    metric["metric_kind"] not in specs
                                    or family != specs[metric["metric_kind"]]["unit_family"]
                                ):
                                    raise DomainValidationError("incompatible metric")
                if view_day["equipment_needs"] != expected_needs:
                    raise DomainValidationError("day equipment snapshot is inconsistent")
            return result
        except DomainValidationError as error:
            domain_error = ContextRepositoryError(
                "inconsistent_domain_state", "program tree is incomplete", 500
            )
            domain_error.required_next_action = "run_integrity_check"
            raise domain_error from error

    def _commit_program(
        self,
        typed: dict[str, Any],
        confirmation: UUID,
        result: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        source = typed["publication_source"]
        source_row = (
            self.session.execute(
                text(
                    "SELECT state,latest_revision_id FROM trainer.drafts WHERE draft_id=:id FOR UPDATE"
                ),
                {"id": typed["source_draft_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if (
            source_row is None
            or source_row["state"] != "open"
            or str(source_row["latest_revision_id"]) != typed["source_revision_id"]
        ):
            raise ContextRepositoryError("stale_preview", "source program draft changed", 409)
        program_id = UUID(source["program_id"])
        version_id = UUID(source["program_version_id"])
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:id,7))"), {"id": str(program_id)}
        )
        if source["intent"] == "create":
            card = source["program"]
            self.session.execute(
                text(
                    "INSERT INTO trainer.programs(program_id,profile_id,goal_id,name,description,starts_on,ends_on,status,comment,created_at) VALUES (:id,:profile,:goal,:name,:description,:starts_on,:ends_on,:status,:comment,:now)"
                ),
                {
                    "id": program_id,
                    "profile": source["profile_id"],
                    "goal": source["goal_id"],
                    "name": card["name"],
                    "description": card.get("description"),
                    "starts_on": card["starts_on"],
                    "ends_on": card["ends_on"],
                    "status": card["status_code"],
                    "comment": card.get("comment"),
                    "now": now,
                },
            )
            before = None
        else:
            current = self.session.execute(
                text(
                    "SELECT current_version_id FROM trainer.programs WHERE program_id=:id FOR UPDATE"
                ),
                {"id": program_id},
            ).scalar_one()
            if str(current) != source["expected_current_version_id"]:
                raise ContextRepositoryError("stale_preview", "current program changed", 409)
            before = self.get_program(payload={"mode": "current", "program_id": str(program_id)})
            self.session.execute(
                text(
                    "UPDATE trainer.program_versions SET publication_state='retired' WHERE program_version_id=:id AND publication_state='published'"
                ),
                {"id": current},
            )
        version = source["version"]
        self.session.execute(
            text(
                "INSERT INTO trainer.program_versions(program_version_id,program_id,goal_id,goal_revision_id,version_no,version_label,period_start,period_end,rationale,publication_state,published_at,supersedes_version_id,confirmation_id,created_at) VALUES (:id,:program,:goal,:goal_revision,:no,:label,:start,:end,:rationale,'published',:now,:supersedes,:confirmation,:now)"
            ),
            {
                "id": version_id,
                "program": program_id,
                "goal": source["goal_id"],
                "goal_revision": source["goal_revision_id"],
                "no": source["version_no"],
                "label": version["version_label"],
                "start": version["period_start"],
                "end": version["period_end"],
                "rationale": version["rationale"],
                "now": now,
                "supersedes": source["supersedes_version_id"],
                "confirmation": confirmation,
            },
        )
        for day in version["templates"]:
            self.session.execute(
                text(
                    "INSERT INTO trainer.day_templates(day_template_id,program_version_id,position,day_type,purpose,equipment_needs,created_at) VALUES (:id,:version,:position,:kind,:purpose,CAST(:needs AS jsonb),:now)"
                ),
                {
                    "id": day["day_template_id"],
                    "version": version_id,
                    "position": day["position"],
                    "kind": day["day_type_code"],
                    "purpose": day["purpose"],
                    "needs": json.dumps(day["equipment_needs"]),
                    "now": now,
                },
            )
            for block in day["blocks"]:
                self.session.execute(
                    text(
                        "INSERT INTO trainer.template_blocks(template_block_id,day_template_id,position,block_type,name,declared_rounds,rest_seconds,comment,created_at) VALUES (:id,:parent,:position,:kind,:name,:rounds,:rest,:comment,:now)"
                    ),
                    {
                        "id": block["template_block_id"],
                        "parent": day["day_template_id"],
                        "position": block["position"],
                        "kind": block["block_type_code"],
                        "name": block.get("name"),
                        "rounds": block.get("declared_rounds"),
                        "rest": block.get("rest_seconds"),
                        "comment": block.get("comment"),
                        "now": now,
                    },
                )
                for ex in block["exercises"]:
                    self.session.execute(
                        text(
                            "INSERT INTO trainer.template_exercises(template_exercise_id,template_block_id,exercise_id,exercise_revision_id,position,circuit_position,equipment_snapshot,comment,created_at) VALUES (:id,:parent,:exercise,:revision,:position,:circuit,CAST(:equipment AS jsonb),:comment,:now)"
                        ),
                        {
                            "id": ex["template_exercise_id"],
                            "parent": block["template_block_id"],
                            "exercise": ex["exercise_ref"]["object_id"],
                            "revision": ex["exercise_ref"]["revision_id"],
                            "position": ex["position"],
                            "circuit": ex.get("circuit_position"),
                            "equipment": json.dumps(ex["equipment_snapshot"]),
                            "comment": ex.get("comment"),
                            "now": now,
                        },
                    )
                    for item in ex["sets"]:
                        self.session.execute(
                            text(
                                "INSERT INTO trainer.template_sets(template_set_id,template_exercise_id,position,round_no,logical_set_no,side,set_type,tempo,pause_seconds,rest_after_seconds,created_at) VALUES (:id,:parent,:position,:round,:logical,:side,:kind,:tempo,:pause,:rest,:now)"
                            ),
                            {
                                "id": item["template_set_id"],
                                "parent": ex["template_exercise_id"],
                                "position": item["position"],
                                "round": item.get("round_no"),
                                "logical": item["logical_set_no"],
                                "side": item.get("side"),
                                "kind": item["set_type_code"],
                                "tempo": item.get("tempo"),
                                "pause": item.get("pause_seconds"),
                                "rest": item.get("rest_after_seconds"),
                                "now": now,
                            },
                        )
                        for metric in item["metrics"]:
                            self.session.execute(
                                text(
                                    "INSERT INTO trainer.planned_metric_values(planned_metric_value_id,template_set_id,metric_kind,exact_value,min_value,max_value,unit_code,created_at) VALUES (:id,:parent,:kind,:exact,:min,:max,:unit,:now)"
                                ),
                                {
                                    "id": metric["planned_metric_value_id"],
                                    "parent": item["template_set_id"],
                                    "kind": metric["metric_kind"],
                                    "exact": metric.get("exact_value"),
                                    "min": metric.get("min_value"),
                                    "max": metric.get("max_value"),
                                    "unit": metric["unit_code"],
                                    "now": now,
                                },
                            )
        self._commit_program_recommendations(source, confirmation, now)
        self.session.execute(
            text("UPDATE trainer.programs SET current_version_id=:version WHERE program_id=:id"),
            {"version": version_id, "id": program_id},
        )
        self.session.execute(
            text("UPDATE trainer.drafts SET state='confirmed' WHERE draft_id=:id"),
            {"id": typed["source_draft_id"]},
        )
        after = self.get_program(payload={"mode": "current", "program_id": str(program_id)})
        for change in changes:
            self._insert_change(
                change,
                result,
                "retire"
                if source["supersedes_version_id"]
                and change["object_id"] == source["supersedes_version_id"]
                else "publish",
                before,
                after,
                typed.get("publication_reason"),
                now,
            )

    def _commit_program_recommendations(
        self, source: dict[str, Any], confirmation: UUID, now: datetime
    ) -> None:
        for rec in source["version"]["recommendations"]:
            rec_id = UUID(rec["recommendation_id"])
            self.session.execute(
                text(
                    "INSERT INTO trainer.recommendations(recommendation_id,owner_type,program_version_id,recommendation_kind,decision_text,confidence,assumptions,alternatives,revisit_condition,confirmation_id,created_at) VALUES (:id,'program_version',:version,:kind,:decision,:confidence,CAST(:assumptions AS jsonb),CAST(:alternatives AS jsonb),:revisit,:confirmation,:now)"
                ),
                {
                    "id": rec_id,
                    "version": source["program_version_id"],
                    "kind": rec["recommendation_kind"],
                    "decision": rec["decision_text"],
                    "confidence": rec["confidence"],
                    "assumptions": json.dumps(rec["assumptions"]),
                    "alternatives": json.dumps(rec["alternatives"]),
                    "revisit": rec["revisit_condition"],
                    "confirmation": confirmation,
                    "now": now,
                },
            )
            for link in rec["fact_links"]:
                ref = link["ref"]
                self.session.execute(
                    text(
                        "INSERT INTO trainer.recommendation_fact_links(recommendation_id,object_type,object_id,revision_id,role,reason,snapshot_digest,created_at) VALUES (:id,:type,:object,:revision,:role,:reason,:digest,:now)"
                    ),
                    {
                        "id": rec_id,
                        "type": ref["object_type"],
                        "object": ref["object_id"],
                        "revision": ref["revision_id"],
                        "role": link["role"],
                        "reason": link["reason"],
                        "digest": link["snapshot_digest"],
                        "now": now,
                    },
                )
            for link in rec["calculation_links"]:
                self.session.execute(
                    text(
                        "INSERT INTO trainer.recommendation_calculation_links(recommendation_id,derived_value_id,role,created_at) VALUES (:id,:derived,:role,:now)"
                    ),
                    {
                        "id": rec_id,
                        "derived": link["derived_value_id"],
                        "role": link["role"],
                        "now": now,
                    },
                )
            for link in rec["conclusion_links"]:
                self.session.execute(
                    text(
                        "INSERT INTO trainer.recommendation_conclusion_links(recommendation_id,conclusion_version_id,status_revision_id,role,applicability_assessment,status_at_decision,created_at) VALUES (:id,:version,:status_revision,:role,CAST(:assessment AS jsonb),:status,:now)"
                    ),
                    {
                        "id": rec_id,
                        "version": link["conclusion_version_id"],
                        "status_revision": link["status_revision_ref"]["object_id"],
                        "role": link["role"],
                        "assessment": json.dumps(link["applicability_assessment"]),
                        "status": link["status_at_decision"],
                        "now": now,
                    },
                )
            for level, content in (
                ("short", rec["short_explanation"]),
                ("detailed", rec.get("detailed_explanation")),
            ):
                if content is not None:
                    self.session.execute(
                        text(
                            "INSERT INTO trainer.recommendation_explanations(explanation_id,recommendation_id,detail_level,content,generated_at,is_reasoning,created_at) VALUES (:id,:rec,:level,:content,:now,false,:now)"
                        ),
                        {
                            "id": uuid4(),
                            "rec": rec_id,
                            "level": level,
                            "content": content,
                            "now": now,
                        },
                    )

    def _materialize_program_evidence(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        gaps = []
        dependencies = []
        effective = datetime.fromisoformat(body["context"]["effective_at"])
        profile = self.session.execute(
            text(
                "SELECT to_jsonb(r) FROM trainer.profile_revisions r JOIN trainer.profiles p ON p.current_revision_id=r.profile_revision_id WHERE p.profile_id=:id"
            ),
            {"id": body["profile_id"]},
        ).scalar_one()
        goal = self.session.execute(
            text("SELECT to_jsonb(r) FROM trainer.goal_revisions r WHERE goal_revision_id=:id"),
            {"id": body["goal_revision_id"]},
        ).scalar_one()
        facts = {
            **profile,
            "goal_type": goal["goal_type"],
            "profile_id": body["profile_id"],
            "goal_id": body["goal_id"],
        }
        if not body["version"]["recommendations"]:
            gaps.append(
                {
                    "path": "version.recommendations",
                    "code": "missing",
                    "disposition": "block",
                    "message_safe": "program design requires an explicit recommendation and applicable scientific support",
                }
            )
        for index, rec in enumerate(body["version"]["recommendations"]):
            rec["recommendation_id"] = str(uuid4())
            if not rec["fact_links"]:
                gaps.append(
                    {
                        "path": f"version.recommendations[{index}].fact_links",
                        "code": "missing",
                        "disposition": "block",
                        "message_safe": "program recommendation must link personal facts",
                    }
                )
            if rec["recommendation_kind"] != "program_design":
                raise ContextRepositoryError(
                    "validation_error", "unsupported recommendation kind", 400
                )
            for link in rec["fact_links"]:
                ref = link["ref"]
                if (
                    ref["object_type"] not in ("profile", "goal", "exercise")
                    or ref.get("revision_id") is None
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unsupported or inexact fact reference", 400
                    )
                actual = self._revision_digest(ref["object_type"], UUID(ref["revision_id"]))
                if actual != link["snapshot_digest"]:
                    raise ContextRepositoryError(
                        "validation_error", "fact snapshot digest mismatch", 400
                    )
                table, column = {
                    "profile": ("profile_revisions", "profile_revision_id"),
                    "goal": ("goal_revisions", "goal_revision_id"),
                    "exercise": ("exercise_revisions", "exercise_revision_id"),
                }[ref["object_type"]]
                owner_col = {"profile": "profile_id", "goal": "goal_id", "exercise": "exercise_id"}[
                    ref["object_type"]
                ]
                if not self.session.execute(
                    text(
                        f"SELECT 1 FROM trainer.{table} WHERE {column}=:revision AND {owner_col}=:object"
                    ),
                    {"revision": ref["revision_id"], "object": ref["object_id"]},
                ).first():
                    raise ContextRepositoryError("not_found", "fact reference not found", 404)
                dependencies.append(
                    {
                        "dependency_type": ref["object_type"],
                        "object_id": ref["object_id"],
                        "revision_id": ref["revision_id"],
                        "digest": actual,
                        "reason": "recommendation fact",
                    }
                )
            for link in rec["calculation_links"]:
                row = self.session.execute(
                    text(
                        "SELECT to_jsonb(v) FROM trainer.derived_values v WHERE derived_value_id=:id"
                    ),
                    {"id": link["derived_value_id"]},
                ).scalar_one_or_none()
                if row is None:
                    raise ContextRepositoryError("not_found", "calculation not found", 404)
                if row["state"] != "current" or row["profile_id"] != body["profile_id"]:
                    raise ContextRepositoryError(
                        "validation_error", "calculation is not current for this profile", 400
                    )
                dependencies.append(
                    {
                        "dependency_type": "derived_value",
                        "object_id": link["derived_value_id"],
                        "revision_id": link["derived_value_id"],
                        "digest": digest_payload(row),
                        "reason": "recommendation calculation",
                    }
                )
            supported = False
            for link in rec["conclusion_links"]:
                snapshot = self._knowledge_snapshot(UUID(link["conclusion_version_id"]), effective)
                if snapshot is None:
                    raise ContextRepositoryError("not_found", "knowledge status not found", 404)
                status = snapshot["status"]
                assessment = assess_applicability(snapshot["scopes"], facts)
                eligible = (
                    status["status"] == "verified"
                    and snapshot["is_current"]
                    and snapshot["version"]["review_due_on"] >= effective.date().isoformat()
                    and assessment["outcome"] == "applicable"
                )
                assessment["eligible"] = eligible
                link["status_at_decision"] = status["status"]
                link["applicability_assessment"] = assessment
                link["status_revision_ref"] = {
                    "object_type": "knowledge_status_revision",
                    "object_id": status["status_revision_id"],
                    "revision_id": status["status_revision_id"],
                }
                dependencies.append(
                    {
                        "dependency_type": "conclusion_version",
                        "object_id": link["conclusion_version_id"],
                        "revision_id": status["status_revision_id"],
                        "digest": digest_payload(snapshot),
                        "reason": "exact knowledge eligibility/status/scope",
                    }
                )
                supported = supported or (link["role"] == "supports" and eligible)
                if link["role"] == "supports" and not eligible:
                    gaps.append(
                        {
                            "path": f"version.recommendations[{index}].conclusion_links",
                            "code": "conflict",
                            "disposition": "block",
                            "message_safe": "noneligible knowledge cannot be used as verified support",
                        }
                    )
            if not supported:
                gaps.append(
                    {
                        "path": f"version.recommendations[{index}].conclusion_links",
                        "code": "missing",
                        "disposition": "block",
                        "message_safe": "significant program design lacks applicable verified support",
                    }
                )
        body["evidence_dependencies"] = dependencies
        return gaps

    def _knowledge_snapshot(self, version_id: UUID, effective: datetime) -> dict[str, Any] | None:
        version = self.session.execute(
            text(
                "SELECT to_jsonb(v) FROM trainer.conclusion_versions v WHERE conclusion_version_id=:id"
            ),
            {"id": version_id},
        ).scalar_one_or_none()
        if version is None:
            return None
        identity = (
            self.session.execute(
                text(
                    "SELECT current_version_id,archived_at FROM trainer.conclusions WHERE conclusion_id=:id"
                ),
                {"id": version["conclusion_id"]},
            )
            .mappings()
            .one()
        )
        status = self.session.execute(
            text(
                "SELECT to_jsonb(s) FROM trainer.knowledge_status_revisions s WHERE conclusion_version_id=:id AND effective_at<=:at ORDER BY effective_at DESC,revision_no DESC LIMIT 1"
            ),
            {"id": version_id, "at": effective},
        ).scalar_one_or_none()
        if status is None:
            return None
        scopes = [
            dict(r)
            for r in self.session.execute(
                text(
                    "SELECT * FROM trainer.applicability_scopes WHERE conclusion_version_id=:id ORDER BY scope_id"
                ),
                {"id": version_id},
            ).mappings()
        ]
        return {
            "version": version,
            "status": status,
            "is_current": str(identity["current_version_id"]) == str(version_id)
            and identity["archived_at"] is None,
            "scopes": json.loads(json.dumps(scopes, default=str)),
        }

    def _insert_evidence_dependency(
        self, revision: UUID, dep: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        self.session.execute(
            text(
                "INSERT INTO trainer.draft_dependencies(draft_dependency_id,draft_revision_id,dependency_type,dependency_object_id,dependency_revision_id,dependency_digest,reason,created_at) VALUES (:id,:revision,:type,:object,:dep_revision,:digest,:reason,:now) ON CONFLICT DO NOTHING"
            ),
            {
                "id": uuid4(),
                "revision": revision,
                "type": dep["dependency_type"],
                "object": dep["object_id"],
                "dep_revision": dep["revision_id"],
                "digest": dep["digest"],
                "reason": dep["reason"],
                "now": now,
            },
        )
        return dict(dep)

    @staticmethod
    def _json_row(row: Any) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(json.dumps(dict(row), default=str)))

    @classmethod
    def _fields(cls, row: Any, *keys: str) -> dict[str, Any]:
        return cls._json_row({key: row[key] for key in keys})

    def _materialize_program_tree(self, body: dict[str, Any]) -> None:
        seen: set[tuple[str, str]] = set()
        for day in body["version"]["templates"]:
            day["day_template_id"] = str(uuid4())
            needs = []
            for block in day["blocks"]:
                block["template_block_id"] = str(uuid4())
                for ex in block["exercises"]:
                    ref = ex["exercise_ref"]
                    if ref.get("revision_id") is None or ref["object_type"] != "exercise":
                        raise ContextRepositoryError(
                            "validation_error", "exact exercise revision is required", 400
                        )
                    row = (
                        self.session.execute(
                            text(
                                "SELECT r.unilateral,r.status_code FROM trainer.exercise_revisions r JOIN trainer.exercises e ON e.exercise_id=r.exercise_id WHERE r.exercise_id=:exercise AND r.exercise_revision_id=:revision AND e.current_revision_id=r.exercise_revision_id"
                            ),
                            {"exercise": ref["object_id"], "revision": ref["revision_id"]},
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if row is None or row["status_code"] != "active":
                        raise ContextRepositoryError(
                            "validation_error", "exercise revision is not current active", 400
                        )
                    requirements = [
                        self._json_row(r)
                        for r in self.session.execute(
                            text(
                                "SELECT equipment_type,optionality,alternative_group FROM trainer.exercise_equipment_requirements WHERE exercise_revision_id=:id ORDER BY equipment_type,optionality,coalesce(alternative_group,'')"
                            ),
                            {"id": ref["revision_id"]},
                        ).mappings()
                    ]
                    measurements = {
                        r["metric_kind"]: dict(r)
                        for r in self.session.execute(
                            text(
                                "SELECT metric_kind,requiredness,unit_family FROM trainer.exercise_measurements WHERE exercise_revision_id=:id"
                            ),
                            {"id": ref["revision_id"]},
                        ).mappings()
                    }
                    ex["template_exercise_id"] = str(uuid4())
                    ex["equipment_snapshot"] = {"requirements": requirements}
                    needs.extend(requirements)
                    for item in ex["sets"]:
                        item["template_set_id"] = str(uuid4())
                        for metric in item["metrics"]:
                            spec = measurements.get(metric["metric_kind"])
                            if spec is None:
                                raise ContextRepositoryError(
                                    "validation_error", "metric is not supported by exercise", 400
                                )
                            family = self.session.execute(
                                text(
                                    "SELECT family_code FROM trainer.unit_family_mapping WHERE unit_code=:unit"
                                ),
                                {"unit": metric["unit_code"]},
                            ).scalar_one_or_none()
                            if family != spec["unit_family"]:
                                raise ContextRepositoryError(
                                    "validation_error", "metric unit family is incompatible", 400
                                )
                            metric["planned_metric_value_id"] = str(uuid4())
                        missing = [
                            k
                            for k, v in measurements.items()
                            if v["requiredness"] == "required"
                            and k not in {m["metric_kind"] for m in item["metrics"]}
                        ]
                        if missing:
                            raise ContextRepositoryError(
                                "validation_error", "required exercise metric is missing", 400
                            )
                        if row["unilateral"] and item.get("side") is None:
                            raise ContextRepositoryError(
                                "validation_error",
                                "unilateral exercise requires an explicit side",
                                400,
                            )
                        if not row["unilateral"] and item.get("side") not in (
                            "both",
                            "not_applicable",
                        ):
                            raise ContextRepositoryError(
                                "validation_error",
                                "bilateral exercise requires both or not_applicable",
                                400,
                            )
                    seen.add((str(ref["object_id"]), str(ref["revision_id"])))
            day["equipment_needs"] = needs

    def _program_availability_snapshot(self, body: dict[str, Any]) -> dict[str, Any]:
        """Capture only the D-G02 availability relevant to this program draft.

        This is deliberately not inventory planning: it is a materialized,
        deterministic dependency on the usual place and types required by the
        selected exercise revisions at the draft's effective time.
        """
        effective = datetime.fromisoformat(body["context"]["effective_at"])
        place_id = self.session.execute(
            text(
                "SELECT usual_place_id FROM trainer.profile_revisions "
                "WHERE profile_id=:profile ORDER BY revision_no DESC LIMIT 1"
            ),
            {"profile": body["profile_id"]},
        ).scalar_one_or_none()
        types = sorted(
            {
                item["equipment_type"]
                for day in body["version"]["templates"]
                for item in day["equipment_needs"]
            }
        )
        if place_id is None or not types:
            return {
                "usual_place_id": str(place_id) if place_id else None,
                "effective_at": effective.isoformat(),
                "items": [],
            }
        rows = self.session.execute(
            text(
                """
                SELECT a.availability_id,a.place_id,a.equipment_id,a.valid_from,a.valid_to,
                       a.context,r.equipment_type
                FROM trainer.place_equipment_availability a
                JOIN trainer.equipment_items e ON e.equipment_id=a.equipment_id
                JOIN trainer.equipment_item_revisions r ON r.equipment_revision_id=e.current_revision_id
                WHERE a.place_id=:place AND a.valid_from<=:effective
                  AND (a.valid_to IS NULL OR a.valid_to>:effective)
                  AND r.equipment_type = ANY(CAST(:types AS text[]))
                ORDER BY r.equipment_type,a.availability_id
                """
            ),
            {"place": place_id, "effective": effective, "types": types},
        ).mappings()
        return {
            "usual_place_id": str(place_id),
            "effective_at": effective.isoformat(),
            "items": [self._json_row(row) for row in rows],
        }

    def _insert_availability_dependency(
        self, revision_id: UUID, item: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        return self._insert_evidence_dependency(
            revision_id,
            {
                "dependency_type": "equipment_availability",
                "object_id": item["availability_id"],
                "revision_id": item["availability_id"],
                "digest": digest_payload(item),
                "reason": "program effective equipment availability",
            },
            now,
        )

    def _add_program_dependency(
        self, draft_revision: UUID, program_id: UUID, version_id: UUID, now: datetime
    ) -> dict[str, Any]:
        row = self.session.execute(
            text(
                "SELECT to_jsonb(v) FROM trainer.program_versions v WHERE program_id=:program AND program_version_id=:version"
            ),
            {"program": program_id, "version": version_id},
        ).scalar_one_or_none()
        if row is None:
            raise ContextRepositoryError("stale_preview", "program version unavailable", 409)
        digest = digest_payload(row)
        self.session.execute(
            text(
                "INSERT INTO trainer.draft_dependencies(draft_dependency_id,draft_revision_id,dependency_type,dependency_object_id,dependency_revision_id,dependency_digest,reason,created_at) VALUES (:id,:draft,'program_version',:program,:version,:digest,'current program version',:now)"
            ),
            {
                "id": uuid4(),
                "draft": draft_revision,
                "program": program_id,
                "version": version_id,
                "digest": digest,
                "now": now,
            },
        )
        return {
            "dependency_type": "program_version",
            "object_id": str(program_id),
            "revision_id": str(version_id),
            "digest": digest,
            "reason": "current program version",
        }

    def query_exercises(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["mode"] == "exact":
            exercise_id = UUID(str(payload["exercise_id"]))
            current = self.session.execute(
                text("SELECT current_revision_id FROM trainer.exercises WHERE exercise_id=:id"),
                {"id": exercise_id},
            ).scalar_one_or_none()
            if current is None:
                raise ContextRepositoryError("not_found", "exercise not found", 404)
            revision_id = UUID(str(payload.get("revision_id") or current))
            aggregate = self._load_exercise_aggregate(exercise_id, revision_id)
            if aggregate is None:
                raise ContextRepositoryError("not_found", "exercise revision not found", 404)
            return {"aggregate": aggregate, "current": revision_id == current}
        if payload.get("cursor") is not None:
            raise ContextRepositoryError("validation_error", "cursor is not supported yet", 400)
        term = normalize_name(payload["text"])
        rows = self.session.execute(
            text("""
                SELECT e.exercise_id,r.exercise_revision_id,r.canonical_name,r.category_code,
                       array_remove(array_agg(DISTINCT a.alias),NULL) aliases,
                       array_remove(array_agg(DISTINCT m.metric_kind),NULL) metric_kinds,
                       array_remove(array_agg(DISTINCT q.equipment_type),NULL) equipment_types,
                       CASE WHEN r.normalized_name=:term THEN 1.0
                            WHEN r.normalized_name LIKE :prefix THEN 0.9 ELSE 0.7 END rank
                FROM trainer.exercises e
                JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id
                LEFT JOIN trainer.exercise_aliases a ON a.exercise_revision_id=r.exercise_revision_id
                LEFT JOIN trainer.exercise_measurements m ON m.exercise_revision_id=r.exercise_revision_id
                LEFT JOIN trainer.exercise_equipment_requirements q ON q.exercise_revision_id=r.exercise_revision_id
                WHERE e.archived_at IS NULL AND e.logically_deleted_at IS NULL
                  AND (r.normalized_name LIKE :pattern OR EXISTS (
                      SELECT 1 FROM trainer.exercise_aliases ax
                      WHERE ax.exercise_revision_id=r.exercise_revision_id
                        AND ax.normalized_alias LIKE :pattern))
                  AND (CAST(:metrics AS text[]) IS NULL OR NOT EXISTS (
                      SELECT 1 FROM unnest(CAST(:metrics AS text[])) wanted
                      WHERE NOT EXISTS (SELECT 1 FROM trainer.exercise_measurements mx
                          WHERE mx.exercise_revision_id=r.exercise_revision_id
                            AND mx.metric_kind=wanted)))
                  AND (CAST(:equipment AS text[]) IS NULL OR EXISTS (
                      SELECT 1 FROM trainer.exercise_equipment_requirements qx
                      WHERE qx.exercise_revision_id=r.exercise_revision_id
                        AND qx.equipment_type=ANY(CAST(:equipment AS text[]))))
                GROUP BY e.exercise_id,r.exercise_revision_id,r.canonical_name,r.category_code,
                         r.normalized_name
                ORDER BY rank DESC,r.normalized_name,e.exercise_id LIMIT :limit
            """),
            {
                "term": term,
                "prefix": term + "%",
                "pattern": "%" + term + "%",
                "metrics": payload.get("metric_kinds"),
                "equipment": payload.get("equipment_type"),
                "limit": payload["limit"],
            },
        ).mappings()
        return {
            "items": [
                {
                    "exercise_ref": self._ref(
                        "exercise", row["exercise_id"], row["exercise_revision_id"]
                    ),
                    "name": row["canonical_name"],
                    "aliases": row["aliases"],
                    "category": row["category_code"],
                    "metric_kinds": row["metric_kinds"],
                    "equipment_types": row["equipment_types"],
                    "rank": row["rank"],
                }
                for row in rows
            ],
            "next_cursor": None,
        }

    def _load_exercise_aggregate(
        self, exercise_id: UUID, revision_id: UUID
    ) -> dict[str, Any] | None:
        row = (
            self.session.execute(
                text("""
                SELECT r.* FROM trainer.exercise_revisions r
                WHERE r.exercise_id=:exercise AND r.exercise_revision_id=:revision
            """),
                {"exercise": exercise_id, "revision": revision_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        aliases = [
            dict(item)
            for item in self.session.execute(
                text(
                    "SELECT alias,locale,context FROM trainer.exercise_aliases WHERE exercise_revision_id=:id ORDER BY normalized_alias,exercise_alias_id"
                ),
                {"id": revision_id},
            ).mappings()
        ]
        roles = [
            dict(item)
            for item in self.session.execute(
                text("""SELECT g.muscle_group_id,g.name,g.normalized_name,g.status_code,m.role
                    FROM trainer.exercise_muscle_roles m JOIN trainer.muscle_groups g
                      ON g.muscle_group_id=m.muscle_group_id
                    WHERE m.exercise_revision_id=:id ORDER BY m.role,g.normalized_name,g.muscle_group_id"""),
                {"id": revision_id},
            ).mappings()
        ]
        measurements = [
            dict(item)
            for item in self.session.execute(
                text(
                    "SELECT metric_kind,requiredness,unit_family FROM trainer.exercise_measurements WHERE exercise_revision_id=:id ORDER BY metric_kind"
                ),
                {"id": revision_id},
            ).mappings()
        ]
        requirements = [
            dict(item)
            for item in self.session.execute(
                text(
                    "SELECT equipment_type,optionality,alternative_group FROM trainer.exercise_equipment_requirements WHERE exercise_revision_id=:id ORDER BY optionality,coalesce(alternative_group,''),equipment_type"
                ),
                {"id": revision_id},
            ).mappings()
        ]
        aggregate = {
            "exercise_ref": self._ref("exercise", exercise_id, revision_id),
            "canonical_name": row["canonical_name"],
            "description": row["description"],
            "category_code": row["category_code"],
            "unilateral": row["unilateral"],
            "status_code": row["status_code"],
            "comment": row["comment"],
            "aliases": aliases,
            "muscle_roles": [
                {
                    "muscle_group": {
                        key: str(item[key]) if key == "muscle_group_id" else item[key]
                        for key in ("muscle_group_id", "name", "normalized_name", "status_code")
                    },
                    "role": item["role"],
                }
                for item in roles
            ],
            "measurements": measurements,
            "equipment_requirements": requirements,
        }
        aggregate["content_digest"] = digest_payload(aggregate)
        return aggregate

    def _muscle_group(self, group_id: UUID) -> dict[str, Any] | None:
        row = (
            self.session.execute(
                text(
                    "SELECT muscle_group_id,name,normalized_name,normalization_version,status_code FROM trainer.muscle_groups WHERE muscle_group_id=:id AND archived_at IS NULL"
                ),
                {"id": group_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        result = dict(row)
        result["muscle_group_id"] = str(result["muscle_group_id"])
        return result

    def _muscle_group_by_name(self, normalized: str) -> dict[str, Any] | None:
        row = (
            self.session.execute(
                text(
                    "SELECT muscle_group_id,name,normalized_name,normalization_version,status_code FROM trainer.muscle_groups WHERE normalized_name=:name AND archived_at IS NULL"
                ),
                {"name": normalized},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        result = dict(row)
        result["muscle_group_id"] = str(result["muscle_group_id"])
        return result

    def _assert_catalog_codes(self, exercise: dict[str, Any]) -> None:
        checks = [
            ("exercise_category_catalog", [exercise["category_code"]]),
            ("exercise_status_catalog", [exercise["status_code"]]),
            ("metric_kind_catalog", [item["metric_kind"] for item in exercise["measurements"]]),
            ("unit_family_catalog", [item["unit_family"] for item in exercise["measurements"]]),
            (
                "equipment_type_catalog",
                [item["equipment_type"] for item in exercise["equipment_requirements"]],
            ),
        ]
        for table, codes in checks:
            for code in set(codes):
                if (
                    self.session.execute(
                        text(
                            f"SELECT 1 FROM trainer.{table} WHERE code=:code AND archived_at IS NULL"
                        ),
                        {"code": code},
                    ).first()
                    is None
                ):
                    raise ContextRepositoryError("validation_error", f"unknown {table} code", 422)
        for measurement in exercise["measurements"]:
            if (
                self.session.execute(
                    text("""
                        SELECT 1 FROM trainer.exercise_measurement_compatibility
                        WHERE metric_kind=:metric_kind
                          AND requiredness=:requiredness
                          AND unit_family=:unit_family
                    """),
                    measurement,
                ).first()
                is None
            ):
                raise ContextRepositoryError(
                    "validation_error", "incompatible exercise measurement combination", 422
                )

    def _catalog_rules_digest(self) -> str:
        payload: dict[str, Any] = {"normalization_version": NORMALIZATION_VERSION}
        for table in (
            "exercise_category_catalog",
            "exercise_status_catalog",
            "muscle_group_status_catalog",
            "metric_kind_catalog",
            "unit_family_catalog",
            "exercise_measurement_compatibility",
            "equipment_type_catalog",
        ):
            order_by = (
                "metric_kind,requiredness,unit_family"
                if table == "exercise_measurement_compatibility"
                else "code"
            )
            payload[table] = [
                dict(row)
                for row in self.session.execute(
                    text(f"SELECT * FROM trainer.{table} ORDER BY {order_by}")
                ).mappings()
            ]
        return digest_payload(json.loads(json.dumps(payload, default=str)))

    def _active_name_revision(self, normalized_name: str) -> UUID:
        current = self.session.execute(
            text(
                "SELECT current_revision_id FROM trainer.exercises WHERE active_normalized_name=:name"
            ),
            {"name": normalized_name},
        ).scalar_one_or_none()
        return UUID(str(current)) if current else uuid5(NAMESPACE_URL, "trainer:no-active-exercise")

    def _add_dependency(
        self,
        draft_revision: UUID,
        dep_type: str,
        object_id: UUID,
        dep_revision: UUID,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        if dep_type == "exercise_catalog_rules":
            digest = self._catalog_rules_digest()
        elif dep_type == "active_exercise_name":
            digest = digest_payload({"revision_id": str(dep_revision)})
        else:
            digest = self._revision_digest(dep_type, dep_revision)
        self.session.execute(
            text("""
            INSERT INTO trainer.draft_dependencies(draft_dependency_id,draft_revision_id,
                dependency_type,dependency_object_id,dependency_revision_id,dependency_digest,reason,created_at)
            VALUES (:id,:draft,:type,:object,:revision,:digest,:reason,:now)
            ON CONFLICT DO NOTHING
        """),
            {
                "id": uuid4(),
                "draft": draft_revision,
                "type": dep_type,
                "object": object_id,
                "revision": dep_revision,
                "digest": digest,
                "reason": reason,
                "now": now,
            },
        )
        return {
            "dependency_type": dep_type,
            "object_id": str(object_id),
            "revision_id": str(dep_revision),
            "digest": digest,
            "reason": reason,
        }

    def _revision_digest(self, dep_type: str, revision_id: UUID) -> str:
        table, column = {
            "profile": ("profile_revisions", "profile_revision_id"),
            "goal": ("goal_revisions", "goal_revision_id"),
            "exercise": ("exercise_revisions", "exercise_revision_id"),
            "exercise_revision": ("exercise_revisions", "exercise_revision_id"),
            "program_version": ("program_versions", "program_version_id"),
            "workout_plan": ("workout_plan_revisions", "plan_revision_id"),
            "derived_value": ("derived_values", "derived_value_id"),
            "training_place": ("training_place_revisions", "place_revision_id"),
            "equipment_item": ("equipment_item_revisions", "equipment_revision_id"),
            "muscle_group": ("muscle_groups", "muscle_group_id"),
        }[dep_type]
        row = self.session.execute(
            text(f"SELECT to_jsonb(r) AS payload FROM trainer.{table} r WHERE {column}=:id"),
            {"id": revision_id},
        ).scalar_one_or_none()
        if row is None:
            raise ContextRepositoryError("stale_preview", "dependency revision is unavailable", 409)
        if dep_type == "equipment_item":
            try:
                # Current dependencies use the same closed facts and integer quantity
                # as the inventory snapshot. Saved legacy payloads/digests stay untouched.
                row = {
                    "ref": self._ref("equipment_item", UUID(str(row["equipment_id"])), revision_id),
                    "equipment": project_card(row),
                }
            except DomainValidationError as error:
                raise ContextRepositoryError(
                    "inconsistent_domain_state", "equipment dependency is inconsistent", 500
                ) from error
        return digest_payload(row)

    def _check_dependencies(self, revision_id: UUID) -> None:
        rows = list(
            self.session.execute(
                text("SELECT * FROM trainer.draft_dependencies WHERE draft_revision_id=:id"),
                {"id": revision_id},
            ).mappings()
        )
        typed = dict(
            self.session.execute(
                text(
                    "SELECT typed_payload FROM trainer.draft_revisions WHERE draft_revision_id=:id"
                ),
                {"id": revision_id},
            ).scalar_one()
        )
        from trainer.infrastructure.db.inventory import InventoryRepository

        inventory = InventoryRepository(self)
        inventory_owner = typed.get("publication_source", typed)
        if "inventory_change" in typed:
            inventory.lock_profile(typed["profile_id"])
        if "measurement_change" in typed:
            from trainer.infrastructure.db.measurements import MeasurementRepository

            MeasurementRepository(self).check_fresh(typed)
        if "availability_context" in typed:
            raise ContextRepositoryError(
                "stale_preview", "legacy inventory draft must be rebuilt", 409
            )
        candidate_snapshot = typed.get("context_snapshot", typed)
        if (
            "plan" in typed
            and "context_snapshot" in typed
            and self._saved_inventory_format(candidate_snapshot) != "global_inventory_v2"
        ):
            raise ContextRepositoryError("stale_preview", "legacy plan draft must be rebuilt", 409)
        if candidate_snapshot.get("inventory_snapshot_version") == "global_inventory_v2":
            profile_id = inventory_owner.get("profile_id")
            if profile_id is None:
                profile_id = candidate_snapshot["profile_ref"]["object_id"]
            inventory.lock_profile(profile_id)
            fresh, fresh_digest, _ = inventory_snapshot(inventory.current(profile_id))
            if (
                fresh != candidate_snapshot["equipment_inventory_snapshot"]
                or fresh_digest != candidate_snapshot["equipment_inventory_digest"]
            ):
                raise ContextRepositoryError(
                    "stale_preview", "current global inventory membership or revisions changed", 409
                )
        for dep in sorted(
            rows, key=lambda item: (item["dependency_type"], str(item["dependency_object_id"]))
        ):
            if dep["dependency_type"] == "conclusion_version":
                snapshot = self._knowledge_snapshot(dep["dependency_object_id"], datetime.now(UTC))
                if (
                    snapshot is None
                    or digest_payload(snapshot) != dep["dependency_digest"]
                    or snapshot["version"]["review_due_on"] < datetime.now(UTC).date().isoformat()
                ):
                    raise ContextRepositoryError(
                        "stale_preview", "knowledge eligibility changed", 409
                    )
                continue
            if dep["dependency_type"] == "derived_value":
                snapshot = self.session.execute(
                    text(
                        "SELECT to_jsonb(v) FROM trainer.derived_values v WHERE derived_value_id=:id"
                    ),
                    {"id": dep["dependency_object_id"]},
                ).scalar_one_or_none()
                if snapshot is None or digest_payload(snapshot) != dep["dependency_digest"]:
                    raise ContextRepositoryError(
                        "stale_preview", "calculation dependency changed", 409
                    )
                continue
            if dep["dependency_type"] == "exercise_revision":
                exact = (
                    self.session.execute(
                        text("""
                    SELECT r.exercise_id,to_jsonb(r) AS snapshot,e.current_revision_id,
                           e.archived_at,e.logically_deleted_at,s.planning_eligible
                    FROM trainer.exercise_revisions r
                    JOIN trainer.exercises e ON e.exercise_id=r.exercise_id
                    JOIN trainer.exercise_status_catalog s ON s.code=r.status_code
                    WHERE r.exercise_revision_id=:revision
                    FOR UPDATE OF e
                """),
                        {"revision": dep["dependency_revision_id"]},
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    exact is None
                    or exact["exercise_id"] != dep["dependency_object_id"]
                    or digest_payload(exact["snapshot"]) != dep["dependency_digest"]
                ):
                    raise ContextRepositoryError(
                        "stale_preview", "exact exercise revision is unavailable", 409
                    )
                selected_familiarity = any(
                    str(item.get("exercise_ref", {}).get("object_id"))
                    == str(dep["dependency_object_id"])
                    and str(item.get("exercise_ref", {}).get("revision_id"))
                    == str(dep["dependency_revision_id"])
                    for item in typed.get("familiar_exercises", [])
                )
                if selected_familiarity and (
                    exact["current_revision_id"] != dep["dependency_revision_id"]
                    or exact["archived_at"] is not None
                    or exact["logically_deleted_at"] is not None
                    or not exact["planning_eligible"]
                ):
                    raise ContextRepositoryError(
                        "stale_preview",
                        "selected familiar exercise is no longer current and planning-eligible",
                        409,
                    )
                continue
            if dep["dependency_type"] == "program_version":
                current = self.session.execute(
                    text(
                        "SELECT current_version_id FROM trainer.programs WHERE program_id=:id FOR UPDATE"
                    ),
                    {"id": dep["dependency_object_id"]},
                ).scalar_one_or_none()
                snapshot = self.session.execute(
                    text(
                        "SELECT to_jsonb(v) FROM trainer.program_versions v WHERE program_version_id=:id"
                    ),
                    {"id": dep["dependency_revision_id"]},
                ).scalar_one_or_none()
                if (
                    current != dep["dependency_revision_id"]
                    or snapshot is None
                    or digest_payload(snapshot) != dep["dependency_digest"]
                ):
                    raise ContextRepositoryError("stale_preview", "program version changed", 409)
                continue
            if dep["dependency_type"] == "equipment_availability":
                if dep["reason"] == "exact place equipment availability at plan effective time":
                    current = (
                        self.session.execute(
                            text("""
                            SELECT a.availability_id,a.place_id,a.equipment_id,a.valid_from,a.valid_to,a.context,
                                   i.profile_id,i.current_revision_id AS equipment_revision_id,
                                   r.equipment_type,r.availability,r.condition,r.name,r.characteristics
                            FROM trainer.place_equipment_availability a
                            JOIN trainer.equipment_items i ON i.equipment_id=a.equipment_id
                            JOIN trainer.equipment_item_revisions r ON r.equipment_revision_id=i.current_revision_id
                            WHERE a.availability_id=:id
                        """),
                            {"id": dep["dependency_object_id"]},
                        )
                        .mappings()
                        .one_or_none()
                    )
                else:
                    current = (
                        self.session.execute(
                            text("""
                            SELECT a.availability_id,a.place_id,a.equipment_id,a.valid_from,a.valid_to,
                                   a.context,r.equipment_type
                            FROM trainer.place_equipment_availability a
                            JOIN trainer.equipment_items e ON e.equipment_id=a.equipment_id
                            JOIN trainer.equipment_item_revisions r ON r.equipment_revision_id=e.current_revision_id
                            WHERE a.availability_id=:id
                        """),
                            {"id": dep["dependency_object_id"]},
                        )
                        .mappings()
                        .one_or_none()
                    )
                if (
                    current is None
                    or digest_payload(self._json_row(current)) != dep["dependency_digest"]
                ):
                    raise ContextRepositoryError(
                        "stale_preview", "equipment availability dependency changed", 409
                    )
                continue
            if dep["dependency_type"] == "exercise_catalog_rules":
                if dep["dependency_digest"] != self._catalog_rules_digest():
                    raise ContextRepositoryError("stale_preview", "catalog rules changed", 409)
                continue
            if dep["dependency_type"] == "active_exercise_name":
                if typed.get("intent") == "familiar_batch":
                    normalized_names = [
                        entry["exercise_draft"]["exercise"]["normalized_name"]
                        for entry in typed["entries"] if entry["action"] == "create"
                    ]
                    names = [name for name in normalized_names
                             if uuid5(NAMESPACE_URL, f"trainer:exercise-name:{name}") == dep["dependency_object_id"]]
                    if len(names) != 1:
                        raise ContextRepositoryError("stale_preview", "exercise name dependency is ambiguous", 409)
                    normalized_name = names[0]
                else:
                    normalized_name = typed["exercise"]["normalized_name"]
                active_name_revision = self._active_name_revision(normalized_name)
                if (
                    active_name_revision != dep["dependency_revision_id"]
                    or digest_payload({"revision_id": str(active_name_revision)})
                    != dep["dependency_digest"]
                ):
                    raise ContextRepositoryError(
                        "stale_preview", "active exercise name changed", 409
                    )
                continue
            identity, current = {
                "profile": ("profiles", "profile_id"),
                "goal": ("goals", "goal_id"),
                "exercise": ("exercises", "exercise_id"),
                "training_place": ("training_places", "place_id"),
                "equipment_item": ("equipment_items", "equipment_id"),
                "workout_plan": ("workout_plans", "plan_id"),
                "muscle_group": ("muscle_groups", "muscle_group_id"),
            }[dep["dependency_type"]]
            lock_clause = "" if dep["dependency_type"] == "muscle_group" else " FOR UPDATE"
            current_revision = self.session.execute(
                text(
                    f"SELECT {'muscle_group_id' if dep['dependency_type'] == 'muscle_group' else 'current_revision_id'} "
                    f"FROM trainer.{identity} WHERE {current}=:id{lock_clause}"
                ),
                {"id": dep["dependency_object_id"]},
            ).scalar_one_or_none()
            if (
                current_revision != dep["dependency_revision_id"]
                or self._revision_digest(dep["dependency_type"], dep["dependency_revision_id"])
                != dep["dependency_digest"]
            ):
                raise ContextRepositoryError("stale_preview", "draft dependency changed", 409)
        if (
            not rows
            and "goals" in typed
            and self.session.execute(
                text("SELECT 1 FROM trainer.profiles WHERE archived_at IS NULL")
            ).first()
        ):
            raise ContextRepositoryError(
                "stale_preview", "onboarding profile dependency changed", 409
            )

    def _assert_acyclic_parent(self, goal_id: UUID, parent_goal_id: UUID) -> None:
        if goal_id == parent_goal_id:
            raise ContextRepositoryError("validation_error", "goal cannot be its own parent")
        cycle = self.session.execute(
            text("""
                WITH RECURSIVE ancestors(goal_id,parent_goal_id) AS (
                    SELECT goal_id,parent_goal_id FROM trainer.goals WHERE goal_id=:parent
                    UNION ALL
                    SELECT g.goal_id,g.parent_goal_id FROM trainer.goals g
                    JOIN ancestors a ON g.goal_id=a.parent_goal_id
                )
                SELECT 1 FROM ancestors WHERE goal_id=:goal LIMIT 1
            """),
            {"goal": goal_id, "parent": parent_goal_id},
        ).first()
        if cycle is not None:
            raise ContextRepositoryError(
                "validation_error", "goal parent hierarchy must be acyclic"
            )

    def _planned_changes(self, typed: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(item) for item in typed["change_plan"]]

    def _copy_plan_revision_tree(
        self,
        source_revision: UUID,
        target_revision: UUID,
        confirmation_id: UUID,
        recommendation_map: dict[UUID, UUID],
        now: datetime,
    ) -> list[str]:
        """Copy an immutable planned tree and recommendation graph to a terminal revision."""
        for block in self.session.execute(
            text(
                "SELECT * FROM trainer.planned_blocks WHERE plan_revision_id=:id ORDER BY position"
            ),
            {"id": source_revision},
        ).mappings():
            block_id = uuid4()
            self.session.execute(
                text("""
                INSERT INTO trainer.planned_blocks(planned_block_id,plan_revision_id,position,block_type,
                    name,declared_rounds,rest_seconds,comment,created_at)
                VALUES (:id,:revision,:position,:type,:name,:rounds,:rest,:comment,:now)
            """),
                {
                    "id": block_id,
                    "revision": target_revision,
                    "position": block["position"],
                    "type": block["block_type"],
                    "name": block["name"],
                    "rounds": block["declared_rounds"],
                    "rest": block["rest_seconds"],
                    "comment": block["comment"],
                    "now": now,
                },
            )
            for exercise in self.session.execute(
                text(
                    "SELECT * FROM trainer.planned_exercises WHERE planned_block_id=:id ORDER BY position"
                ),
                {"id": block["planned_block_id"]},
            ).mappings():
                exercise_id = uuid4()
                self.session.execute(
                    text("""
                    INSERT INTO trainer.planned_exercises(planned_exercise_id,planned_block_id,exercise_id,
                        position,circuit_position,equipment_snapshot,comment,created_at)
                    VALUES (:id,:block,:exercise,:position,:circuit,CAST(:snapshot AS jsonb),:comment,:now)
                """),
                    {
                        "id": exercise_id,
                        "block": block_id,
                        "exercise": exercise["exercise_id"],
                        "position": exercise["position"],
                        "circuit": exercise["circuit_position"],
                        "snapshot": json.dumps(exercise["equipment_snapshot"]),
                        "comment": exercise["comment"],
                        "now": now,
                    },
                )
                for planned_set in self.session.execute(
                    text(
                        "SELECT * FROM trainer.planned_sets WHERE planned_exercise_id=:id ORDER BY position"
                    ),
                    {"id": exercise["planned_exercise_id"]},
                ).mappings():
                    set_id = uuid4()
                    self.session.execute(
                        text("""
                        INSERT INTO trainer.planned_sets(planned_set_id,planned_exercise_id,position,round_no,
                            logical_set_no,side,set_type,target_rpe_min,target_rpe_max,target_rir_min,target_rir_max,
                            tempo,pause_seconds,rest_after_seconds,created_at)
                        VALUES (:id,:exercise,:position,:round,:logical,:side,:type,:rpe_min,:rpe_max,:rir_min,
                            :rir_max,:tempo,:pause,:rest,:now)
                    """),
                        {
                            "id": set_id,
                            "exercise": exercise_id,
                            "position": planned_set["position"],
                            "round": planned_set["round_no"],
                            "logical": planned_set["logical_set_no"],
                            "side": planned_set["side"],
                            "type": planned_set["set_type"],
                            "rpe_min": planned_set["target_rpe_min"],
                            "rpe_max": planned_set["target_rpe_max"],
                            "rir_min": planned_set["target_rir_min"],
                            "rir_max": planned_set["target_rir_max"],
                            "tempo": planned_set["tempo"],
                            "pause": planned_set["pause_seconds"],
                            "rest": planned_set["rest_after_seconds"],
                            "now": now,
                        },
                    )
                    for metric in self.session.execute(
                        text(
                            "SELECT * FROM trainer.planned_metric_values WHERE planned_set_id=:id"
                        ),
                        {"id": planned_set["planned_set_id"]},
                    ).mappings():
                        self.session.execute(
                            text("""
                            INSERT INTO trainer.planned_metric_values(planned_metric_value_id,planned_set_id,
                                metric_kind,exact_value,min_value,max_value,unit_code,custom_definition,created_at)
                            VALUES (:id,:set,:kind,:exact,:minimum,:maximum,:unit,CAST(:custom AS jsonb),:now)
                        """),
                            {
                                "id": uuid4(),
                                "set": set_id,
                                "kind": metric["metric_kind"],
                                "exact": metric["exact_value"],
                                "minimum": metric["min_value"],
                                "maximum": metric["max_value"],
                                "unit": metric["unit_code"],
                                "custom": json.dumps(metric["custom_definition"])
                                if metric["custom_definition"] is not None
                                else None,
                                "now": now,
                            },
                        )
                    for load in self.session.execute(
                        text(
                            "SELECT * FROM trainer.planned_load_components WHERE planned_set_id=:id ORDER BY position"
                        ),
                        {"id": planned_set["planned_set_id"]},
                    ).mappings():
                        self.session.execute(
                            text("""
                            INSERT INTO trainer.planned_load_components(planned_load_component_id,planned_set_id,
                                position,load_kind,equipment_id,equipment_snapshot,item_count,mass,unit_code,
                                mass_meaning,direction,raw_ambiguous_text,created_at)
                            VALUES (:id,:set,:position,:kind,:equipment,CAST(:snapshot AS jsonb),:count,:mass,
                                :unit,:meaning,:direction,:ambiguous,:now)
                        """),
                            {
                                "id": uuid4(),
                                "set": set_id,
                                "position": load["position"],
                                "kind": load["load_kind"],
                                "equipment": load["equipment_id"],
                                "snapshot": json.dumps(load["equipment_snapshot"]),
                                "count": load["item_count"],
                                "mass": load["mass"],
                                "unit": load["unit_code"],
                                "meaning": load["mass_meaning"],
                                "direction": load["direction"],
                                "ambiguous": load["raw_ambiguous_text"],
                                "now": now,
                            },
                        )

        source_recommendations = list(
            self.session.execute(
                text(
                    "SELECT * FROM trainer.recommendations WHERE plan_revision_id=:id ORDER BY recommendation_id"
                ),
                {"id": source_revision},
            ).mappings()
        )
        for source in source_recommendations:
            new_id = recommendation_map[source["recommendation_id"]]
            self.session.execute(
                text("""
                INSERT INTO trainer.recommendations(recommendation_id,owner_type,program_version_id,
                    plan_revision_id,recommendation_kind,decision_text,confidence,assumptions,alternatives,
                    revisit_condition,confirmation_id,created_at)
                VALUES (:id,'plan_revision',NULL,:revision,:kind,:decision,:confidence,CAST(:assumptions AS jsonb),
                    CAST(:alternatives AS jsonb),:revisit,:confirmation,:now)
            """),
                {
                    "id": new_id,
                    "revision": target_revision,
                    "kind": source["recommendation_kind"],
                    "decision": source["decision_text"],
                    "confidence": source["confidence"],
                    "assumptions": json.dumps(source["assumptions"]),
                    "alternatives": json.dumps(source["alternatives"]),
                    "revisit": source["revisit_condition"],
                    "confirmation": confirmation_id,
                    "now": now,
                },
            )
            old_id = source["recommendation_id"]
            for row in self.session.execute(
                text("SELECT * FROM trainer.recommendation_fact_links WHERE recommendation_id=:id"),
                {"id": old_id},
            ).mappings():
                self.session.execute(
                    text("""
                    INSERT INTO trainer.recommendation_fact_links(recommendation_id,object_type,object_id,
                        revision_id,role,reason,snapshot_digest,created_at)
                    VALUES (:id,:type,:object,:revision,:role,:reason,:digest,:now)
                """),
                    {
                        "id": new_id,
                        "type": row["object_type"],
                        "object": row["object_id"],
                        "revision": row["revision_id"],
                        "role": row["role"],
                        "reason": row["reason"],
                        "digest": row["snapshot_digest"],
                        "now": now,
                    },
                )
            for row in self.session.execute(
                text(
                    "SELECT * FROM trainer.recommendation_calculation_links WHERE recommendation_id=:id"
                ),
                {"id": old_id},
            ).mappings():
                self.session.execute(
                    text("""
                    INSERT INTO trainer.recommendation_calculation_links(recommendation_id,derived_value_id,role,created_at)
                    VALUES (:id,:derived,:role,:now)
                """),
                    {
                        "id": new_id,
                        "derived": row["derived_value_id"],
                        "role": row["role"],
                        "now": now,
                    },
                )
            for row in self.session.execute(
                text(
                    "SELECT * FROM trainer.recommendation_conclusion_links WHERE recommendation_id=:id"
                ),
                {"id": old_id},
            ).mappings():
                self.session.execute(
                    text("""
                    INSERT INTO trainer.recommendation_conclusion_links(recommendation_id,conclusion_version_id,
                        role,applicability_assessment,status_at_decision,status_revision_id,created_at)
                    VALUES (:id,:version,:role,CAST(:assessment AS jsonb),:status,:status_revision,:now)
                """),
                    {
                        "id": new_id,
                        "version": row["conclusion_version_id"],
                        "role": row["role"],
                        "assessment": json.dumps(row["applicability_assessment"]),
                        "status": row["status_at_decision"],
                        "status_revision": row["status_revision_id"],
                        "now": now,
                    },
                )
            for row in self.session.execute(
                text(
                    "SELECT * FROM trainer.recommendation_explanations WHERE recommendation_id=:id"
                ),
                {"id": old_id},
            ).mappings():
                self.session.execute(
                    text("""
                    INSERT INTO trainer.recommendation_explanations(explanation_id,recommendation_id,detail_level,
                        content,generated_at,model_provider,model_name,is_reasoning,shown_in_preview_id,created_at)
                    VALUES (:id,:recommendation,:level,:content,:generated,:provider,:model,:reasoning,:preview,:now)
                """),
                    {
                        "id": uuid4(),
                        "recommendation": new_id,
                        "level": row["detail_level"],
                        "content": row["content"],
                        "generated": row["generated_at"],
                        "provider": row["model_provider"],
                        "model": row["model_name"],
                        "reasoning": row["is_reasoning"],
                        "preview": row["shown_in_preview_id"],
                        "now": now,
                    },
                )
        return sorted(str(value) for value in recommendation_map.values())

    def _commit_plan(
        self,
        typed: dict[str, Any],
        confirmation_id: UUID,
        result_id: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        plan = typed["plan"]
        plan_id, revision_id = UUID(typed["plan_id"]), UUID(typed["plan_revision_id"])
        plan_type = plan["plan_type"]
        if plan.get("program_version_id") is not None:
            version = (
                self.session.execute(
                    text(
                        "SELECT p.profile_id,p.current_version_id FROM trainer.program_versions v JOIN trainer.programs p ON p.program_id=v.program_id WHERE v.program_version_id=:id"
                    ),
                    {"id": plan["program_version_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if (
                version is None
                or str(version["profile_id"]) != typed["profile_id"]
                or str(version["current_version_id"]) != str(plan["program_version_id"])
            ):
                raise ContextRepositoryError(
                    "stale_preview", "program version is not current for this profile", 409
                )
        self.session.execute(
            text(
                "INSERT INTO trainer.workout_plans(plan_id,profile_id,current_revision_id,created_at) VALUES (:id,:profile,:revision,:now)"
            ),
            {"id": plan_id, "profile": typed["profile_id"], "revision": revision_id, "now": now},
        )
        self.session.execute(
            text(
                "INSERT INTO trainer.workout_plan_revisions(plan_revision_id,plan_id,revision_no,program_id,program_version_id,day_template_id,planned_date,planned_time,plan_type,name,purpose,lifecycle,context_snapshot,context_snapshot_digest,content_digest,comment,supersedes_revision_id,confirmation_id,created_at) VALUES (:revision,:plan,1,:program,:version,:template,:date,:time,:type,:name,:purpose,'planned',CAST(:snapshot AS jsonb),:snapshot_digest,:content_digest,:comment,NULL,:confirmation,:now)"
            ),
            {
                "revision": revision_id,
                "plan": plan_id,
                "program": plan.get("program_id"),
                "version": plan.get("program_version_id"),
                "template": plan.get("day_template_id"),
                "date": plan["planned_date"],
                "time": plan.get("planned_time"),
                "type": plan_type,
                "name": plan["name"],
                "purpose": plan.get("purpose"),
                "snapshot": json.dumps(typed["context_snapshot"]),
                "snapshot_digest": digest_payload(typed["context_snapshot"]),
                "content_digest": typed["content_digest"],
                "comment": plan.get("comment"),
                "confirmation": confirmation_id,
                "now": now,
            },
        )
        for block in plan["blocks"]:
            block_id = uuid4()
            self.session.execute(
                text(
                    "INSERT INTO trainer.planned_blocks(planned_block_id,plan_revision_id,position,block_type,name,declared_rounds,rest_seconds,comment,created_at) VALUES (:id,:revision,:position,:type,:name,:rounds,:rest,:comment,:now)"
                ),
                {
                    "id": block_id,
                    "revision": revision_id,
                    "position": block["position"],
                    "type": block["block_type_code"],
                    "name": block.get("name"),
                    "rounds": block.get("declared_rounds"),
                    "rest": block.get("rest_seconds"),
                    "comment": block.get("comment"),
                    "now": now,
                },
            )
            for exercise in block["exercises"]:
                exercise_ref = exercise["exercise_ref"]
                current = self.session.execute(
                    text(
                        "SELECT current_revision_id FROM trainer.exercises WHERE exercise_id=:id FOR UPDATE"
                    ),
                    {"id": exercise_ref["object_id"]},
                ).scalar_one_or_none()
                if current is None or str(current) != str(exercise_ref["revision_id"]):
                    raise ContextRepositoryError("stale_preview", "planned exercise changed", 409)
                planned_exercise_id = uuid4()
                self.session.execute(
                    text(
                        "INSERT INTO trainer.planned_exercises(planned_exercise_id,planned_block_id,exercise_id,position,circuit_position,equipment_snapshot,comment,created_at) VALUES (:id,:block,:exercise,:position,:circuit,CAST(:snapshot AS jsonb),:comment,:now)"
                    ),
                    {
                        "id": planned_exercise_id,
                        "block": block_id,
                        "exercise": exercise_ref["object_id"],
                        "position": exercise["position"],
                        "circuit": exercise.get("circuit_position"),
                        "snapshot": json.dumps(exercise["equipment_snapshot"]),
                        "comment": exercise.get("comment"),
                        "now": now,
                    },
                )
                for planned_set in exercise["sets"]:
                    set_id = uuid4()
                    self.session.execute(
                        text(
                            "INSERT INTO trainer.planned_sets(planned_set_id,planned_exercise_id,position,round_no,logical_set_no,side,set_type,target_rpe_min,target_rpe_max,target_rir_min,target_rir_max,tempo,pause_seconds,rest_after_seconds,created_at) VALUES (:id,:exercise,:position,:round,:logical,:side,:type,:rpe_min,:rpe_max,:rir_min,:rir_max,:tempo,:pause,:rest,:now)"
                        ),
                        {
                            "id": set_id,
                            "exercise": planned_exercise_id,
                            "position": planned_set["position"],
                            "round": planned_set.get("round_no"),
                            "logical": planned_set["logical_set_no"],
                            "side": planned_set.get("side"),
                            "type": planned_set["set_type_code"],
                            "rpe_min": planned_set.get("target_rpe_min"),
                            "rpe_max": planned_set.get("target_rpe_max"),
                            "rir_min": planned_set.get("target_rir_min"),
                            "rir_max": planned_set.get("target_rir_max"),
                            "tempo": planned_set.get("tempo"),
                            "pause": planned_set.get("pause_seconds"),
                            "rest": planned_set.get("rest_after_seconds"),
                            "now": now,
                        },
                    )
                    for metric in planned_set["metrics"]:
                        self.session.execute(
                            text(
                                "INSERT INTO trainer.planned_metric_values(planned_metric_value_id,planned_set_id,metric_kind,exact_value,min_value,max_value,unit_code,custom_definition,created_at) VALUES (:id,:set,:kind,:exact,:minimum,:maximum,:unit,CAST(:custom AS jsonb),:now)"
                            ),
                            {
                                "id": uuid4(),
                                "set": set_id,
                                "kind": metric["metric_kind"],
                                "exact": metric.get("exact_value"),
                                "minimum": metric.get("min_value"),
                                "maximum": metric.get("max_value"),
                                "unit": metric["unit_code"],
                                "custom": json.dumps(metric["custom_definition"])
                                if metric.get("custom_definition") is not None
                                else None,
                                "now": now,
                            },
                        )
                    for load in planned_set["loads"]:
                        equip_ref = load.get("equipment_ref")
                        self.session.execute(
                            text(
                                "INSERT INTO trainer.planned_load_components(planned_load_component_id,planned_set_id,position,load_kind,equipment_id,equipment_snapshot,item_count,mass,unit_code,mass_meaning,direction,raw_ambiguous_text,created_at) VALUES (:id,:set,:position,:kind,:equipment,CAST(:snapshot AS jsonb),:count,:mass,:unit,:meaning,:direction,:ambiguous,:now)"
                            ),
                            {
                                "id": uuid4(),
                                "set": set_id,
                                "position": load["position"],
                                "kind": load["load_kind"],
                                "equipment": equip_ref["object_id"] if equip_ref else None,
                                "snapshot": json.dumps(load["equipment_snapshot"]),
                                "count": load.get("item_count"),
                                "mass": load.get("mass"),
                                "unit": load.get("unit_code"),
                                "meaning": load.get("mass_meaning"),
                                "direction": load.get("direction"),
                                "ambiguous": load.get("raw_ambiguous_text"),
                                "now": now,
                            },
                        )
        for recommendation in typed["recommendations"]:
            recommendation_id = UUID(recommendation["recommendation_id"])
            self.session.execute(
                text(
                    "INSERT INTO trainer.recommendations(recommendation_id,owner_type,program_version_id,plan_revision_id,recommendation_kind,decision_text,confidence,assumptions,alternatives,revisit_condition,confirmation_id,created_at) VALUES (:id,'plan_revision',NULL,:revision,:kind,:decision,:confidence,CAST(:assumptions AS jsonb),CAST(:alternatives AS jsonb),:revisit,:confirmation,:now)"
                ),
                {
                    "id": recommendation_id,
                    "revision": revision_id,
                    "kind": recommendation["recommendation_kind"],
                    "decision": recommendation["decision_text"],
                    "confidence": recommendation["confidence"],
                    "assumptions": json.dumps(recommendation["assumptions"]),
                    "alternatives": json.dumps(recommendation["alternatives"]),
                    "revisit": recommendation["revisit_condition"],
                    "confirmation": confirmation_id,
                    "now": now,
                },
            )
            for fact in recommendation["fact_links"]:
                ref = fact["ref"]
                self.session.execute(
                    text(
                        "INSERT INTO trainer.recommendation_fact_links(recommendation_id,object_type,object_id,revision_id,role,reason,snapshot_digest,created_at) VALUES (:id,:type,:object,:revision,:role,:reason,:digest,:now)"
                    ),
                    {
                        "id": recommendation_id,
                        "type": ref["object_type"],
                        "object": ref["object_id"],
                        "revision": ref["revision_id"],
                        "role": fact["role"],
                        "reason": fact["reason"],
                        "digest": fact["snapshot_digest"],
                        "now": now,
                    },
                )
            for calculation in recommendation["calculation_links"]:
                self.session.execute(
                    text(
                        "INSERT INTO trainer.recommendation_calculation_links(recommendation_id,derived_value_id,role,created_at) VALUES (:id,:derived,:role,:now)"
                    ),
                    {
                        "id": recommendation_id,
                        "derived": calculation["derived_value_id"],
                        "role": calculation["role"],
                        "now": now,
                    },
                )
            for conclusion in recommendation["conclusion_links"]:
                status_ref = conclusion["status_revision_ref"]
                self.session.execute(
                    text("""
                    INSERT INTO trainer.recommendation_conclusion_links(recommendation_id,conclusion_version_id,
                        status_revision_id,role,applicability_assessment,status_at_decision,created_at)
                    VALUES (:id,:version,:status_revision,:role,CAST(:assessment AS jsonb),:status,:now)
                """),
                    {
                        "id": recommendation_id,
                        "version": conclusion["conclusion_version_id"],
                        "status_revision": status_ref["revision_id"],
                        "role": conclusion["role"],
                        "assessment": json.dumps(conclusion["applicability_assessment"]),
                        "status": conclusion["status_at_decision"],
                        "now": now,
                    },
                )
            for level, content in (
                ("short", recommendation["short_explanation"]),
                ("detailed", recommendation.get("detailed_explanation")),
            ):
                if content is not None:
                    self.session.execute(
                        text("""
                            INSERT INTO trainer.recommendation_explanations(
                                explanation_id,recommendation_id,detail_level,content,generated_at,
                                is_reasoning,created_at)
                            VALUES (:id,:recommendation,:level,:content,:now,false,:now)
                        """),
                        {
                            "id": uuid4(),
                            "recommendation": recommendation_id,
                            "level": level,
                            "content": content,
                            "now": now,
                        },
                    )
        if changes:
            self._insert_change(
                changes[0],
                result_id,
                "create",
                None,
                {"plan_id": str(plan_id), "plan_revision_id": str(revision_id)},
                None,
                now,
            )

    def _commit_plan_lifecycle(
        self,
        typed: dict[str, Any],
        confirmation_id: UUID,
        result_id: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        lifecycle = typed["plan_lifecycle"]
        intent = lifecycle["intent"]
        state = {"cancel": "cancelled", "miss": "missed", "reschedule": "rescheduled"}[intent]
        source_id = UUID(lifecycle["source_plan_id"])
        old_revision_id = UUID(lifecycle["source_revision_id"])
        new_revision_id = UUID(lifecycle["source_next_revision_id"])
        source = (
            self.session.execute(
                text("""
            SELECT p.profile_id,p.current_revision_id,r.*
            FROM trainer.workout_plans p JOIN trainer.workout_plan_revisions r
              ON r.plan_id=p.plan_id AND r.plan_revision_id=p.current_revision_id
            WHERE p.plan_id=:id FOR UPDATE OF p
        """),
                {"id": source_id},
            )
            .mappings()
            .one_or_none()
        )
        if source is None or source["current_revision_id"] != old_revision_id:
            raise ContextRepositoryError("stale_preview", "source plan changed after preview", 409)
        if source["lifecycle"] != "planned":
            raise ContextRepositoryError(
                "already_superseded", "source plan is no longer planned", 409
            )
        if int(source["revision_no"]) != int(lifecycle["source_revision_no"]):
            raise ContextRepositoryError(
                "stale_preview", "source plan revision number changed", 409
            )

        previous = {
            "plan_id": str(source_id),
            "revision_id": str(old_revision_id),
            "revision_no": source["revision_no"],
            "lifecycle": source["lifecycle"],
            "content_digest": source["content_digest"],
        }
        source_tree = lifecycle["source_tree"]
        old_recommendations = [
            UUID(str(item["recommendation_id"]))
            for item in self.session.execute(
                text(
                    "SELECT recommendation_id FROM trainer.recommendations WHERE plan_revision_id=:id ORDER BY recommendation_id"
                ),
                {"id": old_revision_id},
            ).mappings()
        ]
        recommendation_map = {old_id: uuid4() for old_id in old_recommendations}
        replacement_ids = sorted(str(value) for value in recommendation_map.values())
        context_snapshot = dict(source["context_snapshot"])
        source_content_digest = self._plan_content_digest(
            source_tree, replacement_ids, context_snapshot
        )
        if digest_payload(context_snapshot) != source["context_snapshot_digest"]:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "source plan snapshot changed", 500
            )
        self.session.execute(
            text("""
            INSERT INTO trainer.workout_plan_revisions(plan_revision_id,plan_id,revision_no,program_id,
                program_version_id,day_template_id,planned_date,planned_time,plan_type,name,purpose,lifecycle,
                context_snapshot,context_snapshot_digest,content_digest,comment,supersedes_revision_id,
                confirmation_id,created_at)
            VALUES (:revision,:plan,:revision_no,:program,:version,:template,:date,:time,:type,:name,:purpose,
                :lifecycle,CAST(:snapshot AS jsonb),:snapshot_digest,:content_digest,:comment,:supersedes,
                :confirmation,:now)
        """),
            {
                "revision": new_revision_id,
                "plan": source_id,
                "revision_no": int(source["revision_no"]) + 1,
                "program": source["program_id"],
                "version": source["program_version_id"],
                "template": source["day_template_id"],
                "date": source["planned_date"],
                "time": source["planned_time"],
                "type": source["plan_type"],
                "name": source["name"],
                "purpose": source["purpose"],
                "lifecycle": state,
                "snapshot": json.dumps(context_snapshot),
                "snapshot_digest": source["context_snapshot_digest"],
                "content_digest": source_content_digest,
                "comment": source["comment"],
                "supersedes": old_revision_id,
                "confirmation": confirmation_id,
                "now": now,
            },
        )
        self._copy_plan_revision_tree(
            old_revision_id, new_revision_id, confirmation_id, recommendation_map, now
        )
        self.session.execute(
            text(
                "UPDATE trainer.workout_plans SET current_revision_id=:revision WHERE plan_id=:plan"
            ),
            {"revision": new_revision_id, "plan": source_id},
        )
        source_change = next(item for item in changes if item["object_id"] == str(source_id))
        after = {
            "plan_id": str(source_id),
            "revision_id": str(new_revision_id),
            "revision_no": int(source["revision_no"]) + 1,
            "lifecycle": state,
            "content_digest": source_content_digest,
        }
        self._insert_change(
            source_change, result_id, intent, previous, after, lifecycle.get("reason"), now
        )

        if intent == "reschedule":
            replacement_change = next(
                item for item in changes if item["object_id"] == lifecycle["replacement_plan_id"]
            )
            self._commit_plan(typed, confirmation_id, result_id, [], now)
            self._insert_change(
                replacement_change,
                result_id,
                "create",
                None,
                {
                    "plan_id": lifecycle["replacement_plan_id"],
                    "plan_revision_id": lifecycle["replacement_revision_id"],
                },
                lifecycle.get("reason"),
                now,
            )
            self.session.execute(
                text("""
                INSERT INTO trainer.plan_reschedule_links(source_plan_id,replacement_plan_id,reason,
                    confirmation_id,created_at)
                VALUES (:source,:replacement,:reason,:confirmation,:now)
            """),
                {
                    "source": source_id,
                    "replacement": UUID(lifecycle["replacement_plan_id"]),
                    "reason": lifecycle.get("reason"),
                    "confirmation": confirmation_id,
                    "now": now,
                },
            )

    @staticmethod
    def _new_onboarding_change_plan(typed: dict[str, Any]) -> list[dict[str, Any]]:
        result = [
            {
                "object_type": "profile",
                "object_id": typed["profile_id"],
                "revision_id": str(uuid4()),
            }
        ]
        result += [
            {"object_type": "goal", "object_id": item["goal_id"], "revision_id": str(uuid4())}
            for item in typed["goals"]
        ]
        result += [
            {
                "object_type": "training_place",
                "object_id": item["place_id"],
                "revision_id": str(uuid4()),
            }
            for item in typed["places"]
        ]
        result += [
            {
                "object_type": "equipment_item",
                "object_id": item["equipment_id"],
                "revision_id": str(uuid4()),
            }
            for item in typed["equipment"]
        ]
        return result

    def _insert_change(
        self,
        change: dict[str, Any],
        result_id: UUID,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        reason: str | None,
        now: datetime,
    ) -> UUID:
        change_id = uuid4()
        if after is not None and after.get("current_exit_reason") is not None:
            after["current_exit_change_id"] = str(change_id)
        self.session.execute(
            text("""
            INSERT INTO trainer.domain_changes(domain_change_id,commit_result_id,object_type,object_id,
                new_revision_id,operation,before_snapshot,after_snapshot,initiator,reason,occurred_at,created_at)
            VALUES (:id,:result,:type,:object,:revision,:operation,CAST(:before AS jsonb),
                CAST(:after AS jsonb),'ai_agent',:reason,:now,:now)
        """),
            {
                "id": change_id,
                "result": result_id,
                "type": change["object_type"],
                "object": change["object_id"],
                "revision": change["revision_id"],
                "operation": operation,
                "before": json.dumps(before, default=str) if before is not None else None,
                "after": json.dumps(after, default=str) if after is not None else None,
                "reason": reason,
                "now": now,
            },
        )
        return change_id

    def _commit_onboarding(
        self,
        typed: dict[str, Any],
        confirmation: UUID,
        result: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        profile_id = UUID(typed["profile_id"])
        profile_revision = UUID(changes[0]["revision_id"])
        self.session.execute(
            text(
                "INSERT INTO trainer.profiles(profile_id,current_revision_id,created_at) VALUES (:id,:revision,:now)"
            ),
            {"id": profile_id, "revision": profile_revision, "now": now},
        )
        index = 1
        goal_changes = changes[index : index + len(typed["goals"])]
        index += len(typed["goals"])
        place_changes = changes[index : index + len(typed["places"])]
        index += len(typed["places"])
        equipment_changes = changes[index : index + len(typed["equipment"])]
        goal_pairs = list(zip(typed["goals"], goal_changes, strict=True))
        by_id = {str(goal["goal_id"]): (goal, change) for goal, change in goal_pairs}
        ordered_goal_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        pending = dict(by_id)
        while pending:
            ready = [
                goal_id
                for goal_id, (goal, _) in pending.items()
                if goal.get("parent_goal_id") is None or str(goal["parent_goal_id"]) not in pending
            ]
            if not ready:
                raise ContextRepositoryError(
                    "validation_error", "goal parent hierarchy must be acyclic"
                )
            for goal_id in sorted(ready):
                ordered_goal_pairs.append(pending.pop(goal_id))
        for goal, change in ordered_goal_pairs:
            self.session.execute(
                text(
                    "INSERT INTO trainer.goals(goal_id,profile_id,parent_goal_id,current_revision_id,created_at) VALUES (:id,:profile,:parent,:revision,:now)"
                ),
                {
                    "id": goal["goal_id"],
                    "profile": profile_id,
                    "parent": goal.get("parent_goal_id"),
                    "revision": change["revision_id"],
                    "now": now,
                },
            )
            self._insert_goal_revision(
                goal, UUID(change["revision_id"]), 1, None, confirmation, now
            )
            self._insert_change(change, result, "create", None, goal, None, now)
        for place, change in zip(typed["places"], place_changes, strict=True):
            self.session.execute(
                text(
                    "INSERT INTO trainer.training_places(place_id,profile_id,current_revision_id,created_at) VALUES (:id,:profile,:revision,:now)"
                ),
                {
                    "id": place["place_id"],
                    "profile": profile_id,
                    "revision": change["revision_id"],
                    "now": now,
                },
            )
            self.session.execute(
                text(
                    """INSERT INTO trainer.training_place_revisions(place_revision_id,place_id,revision_no,name,place_type,status,comment,confirmation_id,confirmed_at,created_at) VALUES (:revision,:id,1,:name,:type,:status,:comment,:confirmation,:now,:now)"""
                ),
                {
                    "revision": change["revision_id"],
                    "id": place["place_id"],
                    "name": place["name"],
                    "type": place["place_type"],
                    "status": place["status"],
                    "comment": place.get("comment"),
                    "confirmation": confirmation,
                    "now": now,
                },
            )
            self._insert_change(change, result, "create", None, place, None, now)
        for equipment, change in zip(typed["equipment"], equipment_changes, strict=True):
            self.session.execute(
                text(
                    "INSERT INTO trainer.equipment_items(equipment_id,profile_id,current_revision_id,created_at) VALUES (:id,:profile,:revision,:now)"
                ),
                {
                    "id": equipment["equipment_id"],
                    "profile": profile_id,
                    "revision": change["revision_id"],
                    "now": now,
                },
            )
            from trainer.infrastructure.db.inventory import InventoryRepository

            InventoryRepository(self).insert_revision(
                {key: value for key, value in equipment.items() if key != "equipment_id"},
                change,
                confirmation,
                now,
            )
            self._insert_change(change, result, "create", None, equipment, None, now)
        profile = typed["profile"]
        self.session.execute(
            text(
                """INSERT INTO trainer.profile_revisions(profile_revision_id,profile_id,revision_no,birth_date,sex,timezone,started_on,desired_sessions_per_week,usual_place_id,comment,source_kind,confirmation_id,confirmed_at,created_at) VALUES (:revision,:id,1,:birth,:sex,:timezone,:started,:sessions,:place,:comment,'user',:confirmation,:now,:now)"""
            ),
            {
                "revision": profile_revision,
                "id": profile_id,
                "birth": profile.get("birth_date"),
                "sex": profile.get("sex"),
                "timezone": profile["timezone"],
                "started": profile.get("started_on"),
                "sessions": profile.get("desired_sessions_per_week"),
                "place": profile.get("usual_place_id"),
                "comment": profile.get("comment"),
                "confirmation": confirmation,
                "now": now,
            },
        )
        for selection in typed.get("familiar_exercises", []):
            exercise_ref = selection["exercise_ref"]
            self.session.execute(
                text("""
                    INSERT INTO trainer.profile_familiar_exercises(
                        profile_revision_id,exercise_id,exercise_revision_id,created_at
                    ) VALUES (:profile_revision,:exercise,:exercise_revision,:now)
                """),
                {
                    "profile_revision": profile_revision,
                    "exercise": UUID(str(exercise_ref["object_id"])),
                    "exercise_revision": UUID(str(exercise_ref["revision_id"])),
                    "now": now,
                },
            )
        profile_snapshot = {
            **profile,
            "familiar_exercises": [
                {
                    "exercise_ref": selection["exercise_ref"],
                    "name": selection["name"],
                }
                for selection in typed.get("familiar_exercises", [])
            ],
        }
        self._insert_change(changes[0], result, "create", None, profile_snapshot, None, now)

    def _commit_goal(
        self,
        typed: dict[str, Any],
        profile_id: UUID,
        confirmation: UUID,
        result: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        goal, intent, change = typed["goal"], typed["intent"], changes[0]
        if goal.get("parent_goal_id") is not None:
            self._assert_acyclic_parent(
                UUID(str(goal["goal_id"])), UUID(str(goal["parent_goal_id"]))
            )
        before = None
        if intent == "create":
            revision_no, supersedes = 1, None
            self.session.execute(
                text(
                    "INSERT INTO trainer.goals(goal_id,profile_id,parent_goal_id,current_revision_id,created_at) VALUES (:id,:profile,:parent,:revision,:now)"
                ),
                {
                    "id": goal["goal_id"],
                    "profile": profile_id,
                    "parent": goal.get("parent_goal_id"),
                    "revision": change["revision_id"],
                    "now": now,
                },
            )
        else:
            current = (
                self.session.execute(
                    text("""
                        SELECT r.*,g.parent_goal_id FROM trainer.goals g
                        JOIN trainer.goal_revisions r ON r.goal_revision_id=g.current_revision_id
                        WHERE g.goal_id=:id FOR UPDATE OF g
                    """),
                    {"id": goal["goal_id"]},
                )
                .mappings()
                .one()
            )
            revision_no, supersedes, before = (
                current["revision_no"] + 1,
                current["goal_revision_id"],
                dict(current),
            )
        self._insert_goal_revision(
            goal, UUID(change["revision_id"]), revision_no, supersedes, confirmation, now
        )
        if intent != "create":
            self.session.execute(
                text(
                    "UPDATE trainer.goals SET current_revision_id=:revision,parent_goal_id=:parent "
                    "WHERE goal_id=:id"
                ),
                {
                    "revision": change["revision_id"],
                    "parent": goal.get("parent_goal_id"),
                    "id": goal["goal_id"],
                },
            )
        self._insert_change(change, result, intent, before, goal, typed.get("reason"), now)

    def _commit_exercise(
        self,
        typed: dict[str, Any],
        confirmation: UUID,
        result: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        exercise, intent, change = typed["exercise"], typed["intent"], changes[0]
        exercise_id = UUID(str(exercise["exercise_id"]))
        revision_id = UUID(str(change["revision_id"]))
        normalized = exercise["normalized_name"]
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name,0))"), {"name": normalized}
        )
        conflicting = self.session.execute(
            text(
                "SELECT exercise_id FROM trainer.exercises WHERE active_normalized_name=:name FOR UPDATE"
            ),
            {"name": normalized},
        ).scalar_one_or_none()
        if conflicting is not None and UUID(str(conflicting)) != exercise_id:
            raise ContextRepositoryError("revision_conflict", "active exercise name exists", 409)

        for group, group_change in zip(typed["created_muscle_groups"], changes[1:], strict=True):
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:name,1))"),
                {"name": group["normalized_name"]},
            )
            existing = self._muscle_group_by_name(group["normalized_name"])
            if (
                existing is not None
                and str(existing["muscle_group_id"]) != group["muscle_group_id"]
            ):
                raise ContextRepositoryError(
                    "stale_preview", "muscle group name was concurrently created", 409
                )
            self.session.execute(
                text("""
                INSERT INTO trainer.muscle_groups(muscle_group_id,name,normalized_name,status_code,
                    normalization_version,confirmation_id,created_at)
                VALUES (:id,:name,:normalized,:status,:version,:confirmation,:now)
            """),
                {
                    "id": group["muscle_group_id"],
                    "name": group["name"],
                    "normalized": group["normalized_name"],
                    "status": group["status_code"],
                    "version": group["normalization_version"],
                    "confirmation": confirmation,
                    "now": now,
                },
            )
            self._insert_change(
                group_change, result, "create", None, group, typed.get("reason"), now
            )

        if intent == "create":
            self.session.execute(
                text("""
                INSERT INTO trainer.exercises(exercise_id,current_revision_id,active_normalized_name,created_at)
                VALUES (:id,:revision,:name,:now)
            """),
                {"id": exercise_id, "revision": revision_id, "name": normalized, "now": now},
            )
            supersedes = None
        else:
            current = self.session.execute(
                text(
                    "SELECT current_revision_id FROM trainer.exercises WHERE exercise_id=:id FOR UPDATE"
                ),
                {"id": exercise_id},
            ).scalar_one()
            if str(current) != str(typed["expected_revision_id"]):
                raise ContextRepositoryError(
                    "stale_preview", "exercise changed before confirmation", 409
                )
            supersedes = current
        self.session.execute(
            text("""
            INSERT INTO trainer.exercise_revisions(exercise_revision_id,exercise_id,revision_no,
                canonical_name,normalized_name,description,category_code,unilateral,status_code,
                comment,normalization_version,supersedes_revision_id,confirmation_id,confirmed_at,created_at)
            VALUES (:revision,:exercise,:no,:name,:normalized,:description,:category,:unilateral,
                :status,:comment,:version,:supersedes,:confirmation,:now,:now)
        """),
            {
                "revision": revision_id,
                "exercise": exercise_id,
                "no": typed["revision_no"],
                "name": exercise["canonical_name"],
                "normalized": normalized,
                "description": exercise.get("description"),
                "category": exercise["category_code"],
                "unilateral": exercise["unilateral"],
                "status": exercise["status_code"],
                "comment": exercise.get("comment"),
                "version": exercise["normalization_version"],
                "supersedes": supersedes,
                "confirmation": confirmation,
                "now": now,
            },
        )
        for alias in exercise["aliases"]:
            self.session.execute(
                text("""
                INSERT INTO trainer.exercise_aliases(exercise_alias_id,exercise_revision_id,alias,
                    normalized_alias,locale,context,created_at)
                VALUES (:id,:revision,:alias,:normalized,:locale,:context,:now)
            """),
                {
                    "id": uuid4(),
                    "revision": revision_id,
                    "alias": alias["alias"],
                    "normalized": normalize_name(alias["alias"]),
                    "locale": alias.get("locale"),
                    "context": alias.get("context"),
                    "now": now,
                },
            )
        for role in typed["resolved_muscle_roles"]:
            self.session.execute(
                text("""
                INSERT INTO trainer.exercise_muscle_roles(exercise_revision_id,muscle_group_id,role,created_at)
                VALUES (:revision,:group,:role,:now)
            """),
                {
                    "revision": revision_id,
                    "group": role["muscle_group"]["muscle_group_id"],
                    "role": role["role"],
                    "now": now,
                },
            )
        for item in exercise["measurements"]:
            self.session.execute(
                text("""
                INSERT INTO trainer.exercise_measurements(exercise_measurement_id,
                    exercise_revision_id,metric_kind,requiredness,unit_family,created_at)
                VALUES (:id,:revision,:kind,:requiredness,:family,:now)
            """),
                {
                    "id": uuid4(),
                    "revision": revision_id,
                    "kind": item["metric_kind"],
                    "requiredness": item["requiredness"],
                    "family": item["unit_family"],
                    "now": now,
                },
            )
        for item in exercise["equipment_requirements"]:
            self.session.execute(
                text("""
                INSERT INTO trainer.exercise_equipment_requirements(requirement_id,
                    exercise_revision_id,equipment_type,optionality,alternative_group,created_at)
                VALUES (:id,:revision,:type,:optionality,:group,:now)
            """),
                {
                    "id": uuid4(),
                    "revision": revision_id,
                    "type": item["equipment_type"],
                    "optionality": item["optionality"],
                    "group": item.get("alternative_group"),
                    "now": now,
                },
            )
        if intent == "correct":
            self.session.execute(
                text("""
                UPDATE trainer.exercises SET current_revision_id=:revision,
                    active_normalized_name=:name WHERE exercise_id=:exercise
            """),
                {"revision": revision_id, "name": normalized, "exercise": exercise_id},
            )
        after = self._load_exercise_aggregate(exercise_id, revision_id)
        self._insert_change(
            change, result, intent, typed.get("before"), after, typed.get("reason"), now
        )

    def _insert_goal_revision(
        self,
        goal: dict[str, Any],
        revision: UUID,
        revision_no: int,
        supersedes: UUID | None,
        confirmation: UUID,
        now: datetime,
    ) -> None:
        self.session.execute(
            text("""
            INSERT INTO trainer.goal_revisions(goal_revision_id,goal_id,revision_no,supersedes_revision_id,
                name,description,goal_type,start_value,target_value,unit_code,set_on,target_date,priority,
                status,comment,source_kind,confirmation_id,confirmed_at,created_at)
            VALUES (:revision,:id,:no,:supersedes,:name,:description,:type,:start,:target,:unit,:set_on,
                :target_date,:priority,:status,:comment,'user',:confirmation,:now,:now)
        """),
            {
                "revision": revision,
                "id": goal["goal_id"],
                "no": revision_no,
                "supersedes": supersedes,
                "name": goal["name"],
                "description": goal.get("description"),
                "type": goal["goal_type"],
                "start": goal.get("start_value"),
                "target": goal.get("target_value"),
                "unit": goal.get("unit_code"),
                "set_on": goal["set_on"],
                "target_date": goal["target_date"],
                "priority": goal["priority"],
                "status": goal["status"],
                "comment": goal.get("comment"),
                "confirmation": confirmation,
                "now": now,
            },
        )

    @staticmethod
    def _ref(object_type: str, object_id: UUID, revision_id: UUID | None) -> dict[str, str | None]:
        return {
            "object_type": object_type,
            "object_id": str(object_id),
            "revision_id": str(revision_id) if revision_id is not None else None,
        }

    @staticmethod
    def _preview_field_label(key: str) -> str:
        labels = {
            "after": "Новое состояние",
            "activity_type": "Вид активности",
            "actual_sets": "Фактические подходы",
            "alias": "Дополнительное название",
            "aliases": "Другие названия",
            "applicability": "Применимость",
            "assumptions": "Предположения",
            "alternatives": "Альтернативы",
            "alternative_group": "Группа взаимозаменяемого оборудования",
            "before": "Текущее состояние",
            "birth_date": "Дата рождения",
            "blocks": "Блоки тренировки",
            "brand": "Бренд",
            "canonical_name": "Основное название",
            "category_code": "Категория",
            "caveats": "Ограничения и оговорки",
            "comment": "Комментарий",
            "condition": "Состояние",
            "context": "Контекст",
            "condition_readable": "Условия сравнения",
            "calculation_effects": "Последствия для расчётов",
            "confidence": "Уверенность",
            "conclusion_links": "Связанные выводы",
            "conclusion_version_ref": "Версия научного вывода",
            "created_muscle_groups": "Новые группы мышц",
            "current_snapshot": "Текущий результат до изменения",
            "downstream_effects": "Зависимые расчёты",
            "date": "Дата",
            "local_start_date": "Локальная дата начала тренировки",
            "planned_date": "Дата плана",
            "normalized_inputs": "Нормализованные исходные значения",
            "source_ref": "Источник данных",
            "decision": "Решение",
            "description": "Описание",
            "distance": "Дистанция",
            "duration": "Длительность",
            "equipment": "Оборудование",
            "equipment_needs": "Требования к оборудованию",
            "equipment_requirements": "Требования к оборудованию",
            "equipment_type": "Тип оборудования",
            "full_reps": "Полные повторения",
            "exercise": "Упражнение",
            "exercise_name": "Упражнение",
            "exercise_ref": "Упражнение",
            "exercises": "Упражнения",
            "explanation": "Обоснование",
            "fallback": "Результаты, которые станут актуальными",
            "fatigue": "Оценка усталости",
            "goal": "Цель",
            "goal_type": "Тип цели",
            "gaps": "Неизвестные значения и их решения",
            "goals": "Цели",
            "familiar_exercises": "Знакомые упражнения",
            "historical": "Историческая запись",
            "kind": "Способ выбора",
            "intent_mode": "Тип изменения",
            "inventory_change": "Изменение инвентаря",
            "load": "Нагрузка",
            "loads": "Компоненты нагрузки",
            "locale": "Язык",
            "mass": "Масса",
            "message_safe": "Описание пробела",
            "measurement_change": "Изменение замеров тела",
            "measurement_kind": "Вид замера",
            "metric_kind": "Показатель",
            "measurements": "Измерения",
            "method": "Метод измерения",
            "muscle_roles": "Роли групп мышц",
            "name": "Название",
            "notes": "Примечание",
            "optional": "Необязательное оборудование",
            "optionality": "Обязательность оборудования",
            "partial_reps": "Частичные повторения",
            "place": "Место занятий",
            "place_type": "Тип места",
            "places": "Обычные места занятий",
            "path": "Поле",
            "position": "Позиция",
            "disposition": "Решение по пробелу",
            "priority": "Приоритет",
            "profile": "Профиль",
            "program": "Программа",
            "program_name": "Программа",
            "publication_state": "Состояние публикации",
            "quantity": "Количество",
            "reason": "Причина изменения",
            "requiredness": "Обязательность показателя",
            "role": "Роль",
            "outcome": "Последствие",
            "result": "Результат",
            "method_id": "Метод расчёта",
            "method_version": "Версия метода",
            "completeness": "Полнота исходных данных",
            "included_count": "Учтено значений",
            "excluded_count": "Исключено значений",
            "record": "Рекорд",
            "record_effects": "Затронутые рекорды",
            "preview_plan": "План последствий для рекордов",
            "pending_candidates_to_invalidate": "Кандидаты рекорда к признанию недействительными",
            "accepted_candidates_to_reconcile": "Принятые кандидаты к повторной сверке",
            "source_records_to_revalidate": "Исходные записи рекорда к повторной проверке",
            "source_kind": "Источник значения",
            "record_type": "Вид результата",
            "recommendations": "Рекомендации",
            "report": "Полный отчёт о тренировке",
            "reps": "Повторения",
            "rest_seconds": "Отдых между подходами, секунд",
            "sets": "Подходы",
            "sex": "Пол",
            "side": "Сторона тела",
            "set_on": "Дата постановки цели",
            "started_on": "Дата начала тренировок",
            "start_value": "Начальное значение",
            "timezone": "Часовой пояс",
            "desired_sessions_per_week": "Желаемое количество тренировок в неделю",
            "sources": "Источники",
            "statement": "Формулировка научного вывода",
            "status_code": "Статус упражнения",
            "object_type": "Тип объекта",
            "dependency_type": "Тип зависимости",
            "status": "Статус",
            "state": "Состояние",
            "target": "Объект изменения",
            "target_date": "Целевая дата",
            "target_value": "Целевое значение",
            "technique_self_confirmation": "Подтверждение техники пользователем",
            "time": "Время",
            "training_place": "Место занятий",
            "unit_code": "Единица измерения",
            "unit_family": "Тип единицы измерения",
            "unilateral": "Одностороннее упражнение",
            "usual_place_id": "Обычное место занятий",
            "values": "Значения",
            "weight": "Вес",
            "workout": "Тренировка",
            "workout_date": "Дата тренировки",
            "workout_name": "Тренировка",
        }
        if key in labels:
            return labels[key]
        return key.replace("_", " ").capitalize()

    @staticmethod
    def _preview_gap_path(path: Any, known_labels: dict[str, str], root: Any) -> str:
        if not isinstance(path, str):
            raise ContextRepositoryError("incomplete_preview", "gap path is not readable", 400)
        if path in {"profile", "/report/blocks"}:
            return "Профиль" if path == "profile" else "Блоки фактической тренировки"
        match = re.fullmatch(
            r"version\.recommendations(?:\[(\d+)\]\.(fact_links|conclusion_links))?", path
        )
        if match:
            index, field = match.groups()
            if index is None:
                return "Рекомендации версии программы"
            kind = "связанные факты" if field == "fact_links" else "связанные научные выводы"
            return f"Рекомендация №{int(index) + 1}, {kind}"
        match = re.fullmatch(r"report\.blocks\[(\d+)\]", path)
        if match:
            return f"Блок фактической тренировки №{int(match.group(1)) + 1}"
        match = re.fullmatch(
            r"/report/blocks/(\d+)/exercises/(\d+)(?:/sets(?:/(\d+)/metrics/([a-z_]+))?)?",
            path,
        )
        if match:
            block, exercise, actual_set, metric = match.groups()
            description = f"Блок №{block}, упражнение №{exercise}"
            if "/sets" in path:
                description += (
                    ", фактические подходы" if actual_set is None else f", подход №{actual_set}"
                )
            if metric is not None:
                metric_labels = {
                    "repetitions": "повторения",
                    "duration": "длительность",
                    "distance": "дистанция",
                    "time": "время",
                }
                if metric not in metric_labels:
                    raise ContextRepositoryError("incomplete_preview", "unknown gap metric", 400)
                description += f", показатель «{metric_labels[metric]}»"
            return description
        match = re.fullmatch(
            r"/report/(planned_exercises|planned_sets|planned_load_components)/([0-9a-fA-F-]{36})",
            path,
        )
        if match:
            kind, identity = match.groups()
            if str(UUID(identity)) != identity.lower():
                raise ContextRepositoryError("incomplete_preview", "invalid planned gap identity", 400)
            gaps = root.get("gaps", []) if isinstance(root, dict) else []
            matching = [gap for gap in gaps if isinstance(gap, dict) and gap.get("path") == path]
            if len(matching) == 1:
                detail = matching[0].get("message_safe")
                shapes = {
                    "planned_exercises": r"(Блок №[1-9]\d*, плановое упражнение №[1-9]\d*): Плановое упражнение не отмечено как выполненное, пропущенное, заменённое или неизвестное\.",
                    "planned_sets": r"(Блок №[1-9]\d*, плановое упражнение №[1-9]\d*, плановый подход №[1-9]\d*): Плановый подход не отмечен как выполненный, пропущенный или неизвестный\.",
                    "planned_load_components": r"(Блок №[1-9]\d*, плановое упражнение №[1-9]\d*, плановый подход №[1-9]\d*, компонент нагрузки №[1-9]\d*): Плановый компонент нагрузки не сопоставлен с фактическим или явно неизвестным\.",
                }
                if isinstance(detail, str):
                    readable = re.fullmatch(shapes[kind], detail)
                    if readable:
                        return readable.group(1)
            raise ContextRepositoryError(
                "incomplete_preview", "planned gap has no readable identity", 400
            )
        raise ContextRepositoryError("incomplete_preview", "unknown gap path", 400)

    @staticmethod
    def _preview_text(
        value: Any,
        *,
        key: str | None = None,
        depth: int = 0,
        root: Any = None,
        known_labels: dict[str, str] | None = None,
    ) -> list[str]:
        """Render canonical domain data as readable text without transport identifiers."""
        if root is None:
            root = value
        if known_labels is None:
            known_labels = ContextRepository._preview_known_labels(root)
        technical = {
            "context",
            "change_plan",
            "content_digest",
            "correlation_id",
            "causation_id",
            "draft_id",
            "idempotency_key",
            "intent",
            "operation_id",
            "preview_id",
            "request_digest",
            "revision_id",
            "revision_no",
            "sequence",
            "shown_message_id",
            "owner_profile_id",
        }
        if key == "change_plan":
            return ContextRepository._preview_link_lines(
                value,
                label="Изменяемые объекты",
                payload=root,
                known_labels=known_labels,
                depth=depth,
            )
        if key == "dependencies":
            return ContextRepository._preview_dependency_lines(
                value, payload=root, known_labels=known_labels, depth=depth
            )
        if key == "source_refs":
            return ContextRepository._preview_link_lines(
                value,
                label="Исходные данные расчёта",
                payload=root,
                known_labels=known_labels,
                depth=depth,
            )
        if key == "path":
            return [
                f"{'  ' * depth}Место пробела: "
                f"{ContextRepository._preview_gap_path(value, known_labels, root)}"
            ]
        if key == "record_effects" and isinstance(value, dict):
            return ContextRepository._preview_record_effect_lines(
                value, payload=root, known_labels=known_labels, depth=depth
            )
        if key == "usual_place_id":
            if value is None:
                return [f"{'  ' * depth}Обычное место занятий: не указано"]
            places = root.get("places", []) if isinstance(root, dict) else []
            if isinstance(places, list):
                for place in places:
                    if (
                        isinstance(place, dict)
                        and str(place.get("place_id")) == str(value)
                        and isinstance(place.get("name"), str)
                    ):
                        return [
                            f"{'  ' * depth}Обычное место занятий: {place['name']}"
                        ]
            return [f"{'  ' * depth}Обычное место занятий: не найдено в этом пакете"]
        if (key in technical and not (key == "context" and depth > 1)) or (
            key is not None
            and (
                key.endswith("_id") or key.endswith("_digest") or key in {"digest", "condition_key"}
            )
        ):
            return []
        if value is None:
            rendered = "неизвестно"
            return (
                [f"{'  ' * depth}{ContextRepository._preview_field_label(key)}: {rendered}"]
                if key
                else []
            )
        if isinstance(value, dict):
            if {"object_type", "object_id"}.issubset(value):
                object_type = ContextRepository._preview_object_type_label(value["object_type"])
                target = known_labels.get(str(value["object_id"]), object_type)
                return [
                    f"{'  ' * depth}{ContextRepository._preview_field_label(key or 'reference')}: {target}; точная редакция учтена, служебные ID скрыты"
                ]
            lines: list[str] = []
            for child_key, child_value in sorted(value.items()):
                if (child_key in technical and child_key != "change_plan"
                    and not (child_key == "context" and depth > 0)) or child_key.endswith(
                    "_id"
                ) and child_key != "usual_place_id":
                    continue
                child_lines = ContextRepository._preview_text(
                    child_value,
                    key=child_key,
                    depth=depth + 1,
                    root=root,
                    known_labels=known_labels,
                )
                if child_lines:
                    needs_heading = isinstance(child_value, dict) and not {
                        "object_type",
                        "object_id",
                    }.issubset(child_value)
                    if child_key == "record_effects":
                        needs_heading = True
                    if needs_heading:
                        label = ContextRepository._preview_field_label(child_key)
                        if (
                            depth == 0
                            and child_key == "report"
                            and value.get("intent") == "workout_report"
                            and value.get("correction")
                        ):
                            label = "Новое состояние"
                        lines.append(f"{'  ' * depth}{label}:")
                    lines.extend(child_lines)
            return lines
        if isinstance(value, list):
            if not value:
                return [
                    f"{'  ' * depth}{ContextRepository._preview_field_label(key or 'references')}: нет"
                ]
            if value and all(ContextRepository._is_uuid(item) for item in value):
                label = ContextRepository._preview_field_label(key or "references")
                return [
                    f"{'  ' * depth}{label}: {len(value)} точных ссылок; служебные ID скрыты, подробности объектов приведены в соответствующих разделах выше"
                ]
            lines = [f"{'  ' * depth}{ContextRepository._preview_field_label(key)}:"] if key else []
            for index, item in enumerate(value, start=1):
                item_lines = ContextRepository._preview_text(
                    item, depth=depth + 1, root=root, known_labels=known_labels
                )
                if item_lines:
                    lines.append(f"{'  ' * (depth + 1)}Пункт {index}:")
                    lines.extend(item_lines)
            return lines
        if isinstance(value, bool):
            rendered = "да" if value else "нет"
        elif isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return ContextRepository._preview_text(
                    parsed, key=key, depth=depth, root=root, known_labels=known_labels
                )
            try:
                UUID(value)
            except (ValueError, TypeError, AttributeError):
                pass
            else:
                return []
            rendered = ContextRepository._preview_value_label(key, value)
        else:
            rendered = str(value)
        if key is None:
            return [f"{'  ' * depth}{rendered}"]
        return [f"{'  ' * depth}{ContextRepository._preview_field_label(key)}: {rendered}"]

    @staticmethod
    def _is_uuid(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError):
            return False
        return True

    @staticmethod
    def _preview_record_effect_lines(
        value: dict[str, Any],
        *,
        payload: Any,
        known_labels: dict[str, str],
        depth: int,
    ) -> list[str]:
        categories = (
            ("pending_candidates_to_invalidate", "candidate", "будет признан недействительным"),
            (
                "accepted_candidates_to_reconcile",
                "candidate",
                "будет повторно сверён с исправленной тренировкой",
            ),
            ("source_records_to_revalidate", "record", "будет проверен заново"),
        )
        identities: dict[tuple[str, str], str] = {}
        for key, kind, _ in categories:
            for identity in value.get(key, []):
                if isinstance(identity, str) and ContextRepository._is_uuid(identity):
                    identities[(kind, identity)] = identity
        for item in value.get("preview_plan", []):
            if isinstance(item, dict):
                identity = item.get("candidate_id")
                if isinstance(identity, str) and ContextRepository._is_uuid(identity):
                    identities[("candidate", identity)] = identity

        labels: dict[tuple[str, str], str] = {}
        for kind in ("candidate", "record"):
            type_label = "кандидат рекорда" if kind == "candidate" else "исходная запись рекорда"
            ordered_ids = sorted(
                identity for item_kind, identity in identities if item_kind == kind
            )
            for index, identity in enumerate(ordered_ids, start=1):
                known_label = known_labels.get(identity)
                label = known_label or f"{type_label} №{index}"
                if known_label is None:
                    label = label[:1].upper() + label[1:]
                labels[(kind, identity)] = label

        lines: list[str] = []
        for key, kind, effect in categories:
            ids = value.get(key, [])
            if not isinstance(ids, list) or not ids:
                continue
            lines.append(f"{'  ' * depth}{ContextRepository._preview_field_label(key)}:")
            for identity in sorted(str(item) for item in ids):
                label = labels.get((kind, identity), "связанная запись")
                lines.append(f"{'  ' * (depth + 1)}{label}: {effect}")

        outcomes = {
            "invalidated_pending_source_revision": "будет помечен недействительным из-за замены исходной редакции тренировки",
            "reconcile_accepted_candidate": "будет повторно сверён с исправленной тренировкой",
            "retain": "останется без изменения",
        }
        preview_plan = value.get("preview_plan", [])
        if isinstance(preview_plan, list) and preview_plan:
            lines.append(f"{'  ' * depth}План последствий для рекордов:")
            ordered = sorted(
                (item for item in preview_plan if isinstance(item, dict)),
                key=lambda item: (str(item.get("candidate_id", "")), str(item.get("outcome", ""))),
            )
            for item in ordered:
                identity = str(item.get("candidate_id", ""))
                label = labels.get(("candidate", identity), "кандидат рекорда")
                outcome = str(item.get("outcome", ""))
                description = outcomes.get(outcome, "требует повторной проверки")
                lines.append(f"{'  ' * (depth + 1)}{label}: {description}")
        return lines

    @staticmethod
    def _preview_object_type_label(value: Any) -> str:
        labels = {
            "actual_record": "фактический рекорд",
            "actual_set": "фактический подход",
            "body_measurement_session": "сеанс замеров тела",
            "calculated_best": "расчётный лучший результат",
            "conclusion": "научный вывод",
            "conclusion_version": "версия научного вывода",
            "day_template": "шаблон дня",
            "derived_progress_point": "точка расчётного прогресса",
            "derived_value": "сохранённый расчёт",
            "equipment_item": "предмет инвентаря",
            "equipment_availability": "доступность оборудования",
            "exercise": "упражнение",
            "exercise_catalog_rules": "правила каталога упражнений",
            "active_exercise_name": "название упражнения в каталоге",
            "goal": "цель",
            "knowledge_status_revision": "статус научного вывода",
            "muscle_group": "группа мышц",
            "program": "программа",
            "program_version": "версия программы",
            "profile": "профиль спортсмена",
            "record_candidate": "кандидат рекорда",
            "session": "тренировка",
            "training_place": "место занятий",
            "workout_plan": "план тренировки",
            "workout_session": "тренировка",
            "workout_session_revision": "редакция тренировки",
        }
        return labels.get(str(value), "объект")

    @staticmethod
    def _preview_known_labels(payload: Any) -> dict[str, str]:
        labels: dict[str, str] = {}
        if isinstance(payload, dict):
            id_fields = {
                "actual_record_id": "actual_record",
                "record_id": "actual_record",
                "candidate_id": "record_candidate",
                "conclusion_version_id": "conclusion_version",
                "derived_value_id": "derived_value",
                "equipment_id": "equipment_item",
                "exercise_id": "exercise",
                "goal_id": "goal",
                "plan_id": "workout_plan",
                "program_id": "program",
                "program_version_id": "program_version",
                "session_id": "workout_session",
            }
            for id_field, object_type in id_fields.items():
                identity = payload.get(id_field)
                if not isinstance(identity, str):
                    continue
                name = next(
                    (
                        payload[key]
                        for key in (
                            "canonical_name",
                            "exercise_name",
                            "program_name",
                            "workout_name",
                            "name",
                            "statement",
                            "title",
                            "version_label",
                        )
                        if isinstance(payload.get(key), str) and payload[key].strip()
                    ),
                    None,
                )
                details = [str(name)] if name else []
                date_value = next(
                    (
                        payload[key]
                        for key in (
                            "local_start_date",
                            "planned_date",
                            "achieved_on",
                            "target_date",
                            "period_start",
                            "date",
                        )
                        if payload.get(key) is not None
                    ),
                    None,
                )
                if date_value is not None:
                    details.append(f"дата {date_value}")
                metric = payload.get("metric_kind")
                result = payload.get("value", payload.get("target_value"))
                if metric is not None and result is not None:
                    details.append(f"{metric}: {result}")
                if details:
                    labels[identity] = (
                        f"{ContextRepository._preview_object_type_label(object_type)} «{' — '.join(details)}»"
                    )
            exercise_ref = payload.get("exercise_ref")
            if (
                isinstance(exercise_ref, dict)
                and isinstance(exercise_ref.get("object_id"), str)
                and isinstance(payload.get("name"), str)
                and payload["name"].strip()
            ):
                labels[str(exercise_ref["object_id"])] = f"Упражнение «{payload['name'].strip()}»"
            source_ref = payload.get("source_ref")
            if isinstance(source_ref, dict) and isinstance(source_ref.get("object_id"), str):
                details = []
                date_value = payload.get("date", payload.get("achieved_on"))
                if date_value is not None:
                    details.append(f"дата {date_value}")
                for field in ("metric_kind", "reps", "weight", "value", "unit_code"):
                    if payload.get(field) is not None:
                        details.append(f"{field.replace('_', ' ')} {payload[field]}")
                if details:
                    source_object_type = source_ref.get("object_type")
                    labels[str(source_ref["object_id"])] = (
                        f"{ContextRepository._preview_object_type_label(source_object_type)} «{' — '.join(details)}»"
                    )
            for nested in (payload[key] for key in sorted(payload)):
                labels.update(ContextRepository._preview_known_labels(nested))
        elif isinstance(payload, list):
            for nested in payload:
                labels.update(ContextRepository._preview_known_labels(nested))
        return labels

    @staticmethod
    def _preview_link_lines(
        value: Any,
        *,
        label: str,
        payload: Any,
        known_labels: dict[str, str],
        depth: int = 0,
    ) -> list[str]:
        if not isinstance(value, list):
            return []
        lines = [f"{'  ' * depth}{label}:"]
        rendered_count = 0
        for item in value:
            if not isinstance(item, dict):
                continue
            object_type = item.get("object_type", item.get("dependency_type"))
            identity = item.get("object_id")
            target = known_labels.get(str(identity)) if identity is not None else None
            if target is None and object_type == "workout_session" and isinstance(payload, dict):
                report = payload.get("report", {})
                if isinstance(report, dict):
                    date_value = report.get("local_start_date", report.get("date"))
                    name = report.get("name", report.get("workout_name"))
                    details = [str(name)] if name else []
                    if date_value is not None:
                        details.append(f"дата {date_value}")
                    if details:
                        target = f"тренировка «{' — '.join(details)}»"
            if target is None and object_type == "workout_plan" and isinstance(payload, dict):
                report = payload.get("report", {})
                snapshot = report.get("plan_snapshot", {}) if isinstance(report, dict) else {}
                if isinstance(snapshot, dict):
                    name = snapshot.get("name")
                    planned_date = snapshot.get("planned_date")
                    details = [str(name)] if name else []
                    if planned_date is not None:
                        details.append(f"дата {planned_date}")
                    if details:
                        target = f"план тренировки «{' — '.join(details)}»"
            target = target or ContextRepository._preview_object_type_label(object_type)
            revision = item.get("revision_id")
            suffix = "; редакция зафиксирована" if revision else ""
            lines.append(f"{'  ' * (depth + 1)}• {target}{suffix}; служебные ID скрыты")
            rendered_count += 1
        if rendered_count == 0:
            return []
        return lines

    @staticmethod
    def _preview_dependency_lines(
        value: Any,
        *,
        payload: Any = None,
        known_labels: dict[str, str],
        depth: int = 0,
    ) -> list[str]:
        if not isinstance(value, list):
            return []
        lines = [f"{'  ' * depth}Основания и зависимости:"]
        count = 0
        reason_labels = {
            "current exercise": "текущая редакция упражнения",
            "existing muscle group": "существующая группа мышц",
            "catalog and normalization rules": "правила каталога и сопоставления названий упражнений",
            "active normalized exercise name": "проверка уникальности названия среди активных упражнений",
            "exact workout plan revision for corrected workout": "точная редакция плана исправляемой тренировки",
            "profile owner of corrected workout": "профиль-владелец исправляемой тренировки",
            "current recommendation calculation": "текущий расчёт, использованный рекомендацией",
            "exact recommendation fact": "точный факт, использованный рекомендацией",
        }
        for item in value:
            if not isinstance(item, dict):
                continue
            object_type = item.get("dependency_type")
            target = known_labels.get(str(item.get("object_id")))
            if target is None and object_type == "workout_plan" and isinstance(payload, dict):
                report = payload.get("report", {})
                snapshot = report.get("plan_snapshot", {}) if isinstance(report, dict) else {}
                if isinstance(snapshot, dict):
                    name = snapshot.get("name")
                    planned_date = snapshot.get("planned_date")
                    details = [str(name)] if name else []
                    if planned_date is not None:
                        details.append(f"дата {planned_date}")
                    if details:
                        target = f"план тренировки «{' — '.join(details)}»"
            target = target or ContextRepository._preview_object_type_label(object_type)
            reason = str(item.get("reason", ""))
            readable_reason = reason_labels.get(reason, reason.replace("_", " "))
            qualifier = f" — {readable_reason}" if readable_reason else ""
            lines.append(
                f"{'  ' * (depth + 1)}• {target}{qualifier}; точная редакция учтена, служебные ID и digest скрыты"
            )
            count += 1
        return lines if count else []

    @staticmethod
    def _preview_value_label(key: str | None, value: str) -> str:
        common = {
            "active": "активно",
            "archived": "в архиве",
            "draft": "черновик",
            "published": "опубликовано",
            "retired": "заменено новой редакцией",
            "verified": "проверено владельцем",
            "outdated": "устарело",
            "rejected": "отклонено",
            "primary": "основная",
            "secondary": "дополнительная",
            "stabilizer": "стабилизирующая",
            "left": "левая",
            "right": "правая",
            "both": "обе стороны",
            "not_applicable": "не применяется",
            "create": "создание",
            "correct": "исправление",
            "cancel": "отмена",
            "accept": "принять",
            "reject": "отклонить",
            "high": "высокая",
            "moderate": "умеренная",
            "low": "низкая",
            "insufficient": "недостаточно данных",
            "external": "внешнее отягощение",
            "bodyweight": "собственный вес",
            "band": "резиновая лента",
            "assistance": "помощь",
            "resistance": "сопротивление",
            "performed": "выполнено",
            "partial": "выполнено частично",
            "skipped": "пропущено",
            "added": "добавлено вне плана",
            "interrupted": "прервано",
            "completed": "завершено",
            "in_progress": "в процессе",
            "invalidated_pending_source_revision": "кандидат устареет после исправления источника",
            "invalidated_source": "результат исключён из-за исправления источника",
            "retained_valid": "результат остаётся действительным",
            "required": "обязательное",
            "optional": "необязательное",
            "alternative": "альтернативное",
            "warmup": "разминочный",
            "working": "рабочий",
            "straight": "обычный порядок",
            "superset": "суперсет",
            "circuit": "круговая последовательность",
            "user": "со слов пользователя",
            "calculation": "расчёт",
            "system": "система",
            "curator": "составитель",
            "external_metadata": "внешние метаданные",
            "block": "блокирует подтверждение",
            "clarify": "уточнить у пользователя",
            "assume_with_disclosure": "допущение с явным пояснением",
            "leave_unknown": "оставить неизвестным",
        }
        by_field = {
            "category_code": {"strength": "силовое упражнение"},
            "alternative_group": {"external_load": "внешнее отягощение"},
            "kind": {
                "existing": "существующая группа мышц",
                "create_by_name": "создать группу мышц по названию",
                "saved": "сохранённое место",
                "ad_hoc": "временное место",
            },
            "locale": {"ru": "русский", "en": "английский"},
            "unit_family": {"count": "количество", "time": "время", "length": "длина"},
            "equipment_type": {
                "barbell": "штанга",
                "weight_plate": "диск для штанги",
                "bench": "скамья",
                "pull_up_bar": "турник",
                "parallel_bars": "брусья",
                "resistance_band": "резиновая лента",
                "hyperextension_bench": "скамья для гиперэкстензии",
                "push_up_handles": "упоры для отжиманий",
                "dumbbell": "гантель",
                "exercise_mat": "коврик",
                "treadmill": "беговая дорожка",
                "jump_rope": "скакалка",
                "ab_wheel": "ролик для пресса",
                "boxing_reflex_target": "тренажёр для реакции",
                "hand_gripper": "эспандер для кисти",
                "sportswear": "спортивная одежда",
                "training_gloves": "тренировочные перчатки",
                "wrist_wraps": "кистевые бинты",
            },
            "measurement_kind": {
                "body_mass": "масса тела",
                "height": "рост",
                "waist_circumference": "обхват талии",
                "chest_circumference": "обхват груди",
                "neck_circumference": "обхват шеи",
                "upper_arm_circumference": "обхват плеча",
                "forearm_circumference": "обхват предплечья",
                "thigh_circumference": "обхват бедра",
                "calf_circumference": "обхват голени",
            },
            "original_unit": {
                "centimeter": "сантиметры",
                "millimeter": "миллиметры",
                "inch": "дюймы",
                "kilogram": "килограммы",
                "pound": "фунты",
            },
            "normalized_unit": {"centimeter": "сантиметры", "kilogram": "килограммы"},
            "unit_code": {
                "repetition": "повторения",
                "kilogram": "килограммы",
                "second": "секунды",
                "kg": "килограммы",
            },
            "goal_type": {
                "strength": "силовая цель",
                "endurance": "цель на выносливость",
                "body_composition": "цель по составу тела",
            },
            "place_type": {
                "home": "дом",
                "gym": "спортивный зал",
                "outdoor": "улица",
            },
            "sex": {
                "male": "мужской",
                "female": "женский",
                "other": "другое",
                "not_stated": "не указано",
            },
            "metric_kind": {
                "repetitions": "повторения",
                "duration": "длительность",
                "distance": "дистанция",
                "e1rm": "расчётный одноповторный максимум",
            },
            "role": {**common},
            "status": {**common, "pending": "ожидает решения", "accepted": "принято"},
            "status_code": {**common},
            "state": {**common, "cancelled": "отменено", "missed": "пропущено"},
            "decision": {**common},
            "intent_mode": {**common},
            "optionality": {**common},
            "requiredness": {**common},
            "load_kind": {**common},
            "direction": {**common},
            "set_type_code": {**common},
            "block_type_code": {**common},
            "confidence": {**common},
            "source_kind": {**common},
            "disposition": {**common},
        }
        return by_field.get(key or "", {}).get(value, common.get(value, value.replace("_", " ")))

    @staticmethod
    def _preview_heading(payload: dict[str, Any]) -> str:
        if payload.get("correction") or payload.get("intent_mode") == "correct":
            return "Полное описание исправления"
        if "plan" in payload or "plan_lifecycle" in payload:
            return "Полное описание изменения плана тренировки"
        if "report" in payload:
            return "Полный отчёт о фактической тренировке"
        if "exercise" in payload:
            return "Полное описание упражнения"
        if "measurement_change" in payload:
            return "Полное описание изменения замеров тела"
        if "inventory_change" in payload:
            return "Полное описание изменения инвентаря"
        if payload.get("intent") == "record_decision" or "candidate_snapshot" in payload:
            return "Полное описание решения по кандидату рекорда"
        if "historical" in payload:
            return "Полное описание исторического рекорда"
        if "evidence_change" in payload:
            return "Полное описание научных сведений"
        if "calculated" in payload:
            return "Полное описание расчёта"
        if "program_version_id" in payload or "publication_source" in payload:
            return "Полное описание программы"
        if "profile_id" in payload or "goals" in payload:
            return "Полное описание первого знакомства"
        if "goal" in payload:
            return "Полное описание цели"
        return "Полное описание изменений"

    @staticmethod
    def _render_preview(payload: dict[str, Any], *, detail_level: str = "full") -> str:
        del detail_level  # Persisted/confirmable previews are always rendered in full.
        if payload.get("intent") == "familiar_batch":
            from trainer.infrastructure.db.familiar_batch import FamiliarBatchRepository

            return FamiliarBatchRepository.render(payload)
        if isinstance(payload.get("exercise"), dict) and "resolved_muscle_roles" in payload:
            return ContextRepository._render_exercise_preview(payload)
        labels = ContextRepository._preview_known_labels(payload)
        lines = ContextRepository._preview_text(payload, root=payload, known_labels=labels)
        if not lines:
            raise ContextRepositoryError(
                "incomplete_preview", "preview has no user-readable domain content", 400
            )
        return ContextRepository._preview_heading(payload) + "\n" + "\n".join(lines)

    @staticmethod
    def _render_exercise_preview(payload: dict[str, Any]) -> str:
        """Describe the entire exercise decision without exposing transport metadata."""
        exercise = payload["exercise"]
        intent = payload.get("intent")
        resolved = payload["resolved_muscle_roles"]
        created = payload.get("created_muscle_groups")
        if intent not in {"create", "correct"} or not isinstance(resolved, list) or not isinstance(created, list):
            raise ContextRepositoryError("incomplete_preview", "exercise preview is incomplete", 400)

        def required_label(key: str, value: Any) -> str:
            if not isinstance(value, str) or not value.strip():
                raise ContextRepositoryError("incomplete_preview", f"exercise {key} is unreadable", 400)
            return ContextRepository._preview_value_label(key, value.strip())

        if any(
            not isinstance(group, dict)
            or not isinstance(group.get("muscle_group_id"), str)
            or not isinstance(group.get("name"), str)
            for group in created
        ):
            raise ContextRepositoryError("incomplete_preview", "new muscle group is incomplete", 400)
        created_ids = {group["muscle_group_id"] for group in created}
        resolved_ids = {
            item.get("muscle_group", {}).get("muscle_group_id")
            for item in resolved if isinstance(item, dict) and isinstance(item.get("muscle_group"), dict)
        }
        if not created_ids <= resolved_ids:
            raise ContextRepositoryError("incomplete_preview", "new muscle group has no exercise role", 400)

        def snapshot(card: dict[str, Any], roles: list[dict[str, Any]], *, new_groups: bool) -> list[str]:
            name = card.get("canonical_name")
            if not isinstance(name, str) or not name.strip():
                raise ContextRepositoryError("incomplete_preview", "exercise name is unreadable", 400)
            if not isinstance(card.get("unilateral"), bool):
                raise ContextRepositoryError("incomplete_preview", "exercise sidedness is missing", 400)
            lines = [
                f"Название: {name.strip()}",
                f"Категория: {required_label('category_code', card.get('category_code'))}",
                f"Статус в каталоге: {required_label('status_code', card.get('status_code'))}",
                f"Одностороннее упражнение: {'да' if card.get('unilateral') is True else 'нет'}",
            ]
            measurements = card.get("measurements")
            requirements = card.get("equipment_requirements")
            aliases = card.get("aliases")
            if not all(isinstance(items, list) for items in (measurements, requirements, aliases)):
                raise ContextRepositoryError("incomplete_preview", "exercise fields are incomplete", 400)
            lines.append("Измерения:")
            for item in measurements:
                lines.append(
                    f"  • {required_label('metric_kind', item.get('metric_kind'))} — "
                    f"{required_label('requiredness', item.get('requiredness'))}; "
                    f"единица: {required_label('unit_family', item.get('unit_family'))}"
                )
            lines.append("Оборудование:" if requirements else "Оборудование: не требуется")
            for item in requirements:
                alternative = item.get("alternative_group")
                group = (
                    f"; группа альтернатив: {required_label('alternative_group', alternative)}"
                    if alternative else ""
                )
                lines.append(
                    f"  • {required_label('equipment_type', item.get('equipment_type'))} — "
                    f"{required_label('optionality', item.get('optionality'))}{group}"
                )
            lines.append("Группы мышц:")
            for item in roles:
                group = item.get("muscle_group") if isinstance(item, dict) else None
                if not isinstance(group, dict):
                    raise ContextRepositoryError("incomplete_preview", "exercise muscle group is missing", 400)
                name = group.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ContextRepositoryError("incomplete_preview", "exercise muscle group has no name", 400)
                creation = "; будет создана новая группа" if new_groups and group.get("muscle_group_id") in created_ids else ""
                lines.append(f"  • {name.strip()} — {required_label('role', item.get('role'))}{creation}")
            lines.append("Другие названия:" if aliases else "Другие названия: нет")
            for alias in aliases:
                details = []
                if alias.get("locale"):
                    details.append(f"язык: {required_label('locale', alias['locale'])}")
                if alias.get("context"):
                    details.append(f"контекст: {alias['context']}")
                suffix = f" ({'; '.join(details)})" if details else ""
                lines.append(f"  • {required_label('alias', alias.get('alias'))}{suffix}")
            missing = []
            for key, label in (("description", "описание"), ("comment", "комментарий")):
                value = card.get(key)
                if value is None or value == "":
                    missing.append(label)
                else:
                    lines.append(f"{label.capitalize()}: {value}")
            if missing:
                lines.append("Не указаны: " + ", ".join(missing) + ".")
            return lines

        name = required_label("canonical_name", exercise.get("canonical_name"))
        if intent == "create":
            lines = [f"Создать упражнение «{name}»?", "После подтверждения карточка будет добавлена в каталог упражнений. Знакомым в профиле оно от этого не станет."]
        else:
            before = payload.get("before")
            if not isinstance(before, dict) or not isinstance(before.get("muscle_roles"), list):
                raise ContextRepositoryError("incomplete_preview", "previous exercise state is missing", 400)
            lines = [f"Исправить упражнение «{name}»?", "После подтверждения карточка будет обновлена.", "Было:"]
            lines.extend("  " + line for line in snapshot(before, before["muscle_roles"], new_groups=False))
            lines.append("Станет:")
        current = snapshot(exercise, resolved, new_groups=True)
        lines.extend(("  " + line for line in current) if intent == "correct" else current)
        reason = payload.get("reason")
        if reason:
            lines.append(f"Причина изменения: {reason}")
        elif intent == "create":
            lines.append("Причина создания не указана.")
        lines.append("Изменения пока не сохранены.")
        return "\n".join(lines)


class ContextDatabaseExecutor:
    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def __call__(self, *, action: str, **kwargs: Any) -> dict[str, Any]:
        with self._session_factory() as session:
            try:
                result = cast(dict[str, Any], getattr(ContextRepository(session), action)(**kwargs))
                session.commit()
                return result
            except Exception as error:
                session.rollback()
                self.record_failed_attempt(action, kwargs, error)
                raise

    def record_failed_attempt(self, action: str, kwargs: dict[str, Any], error: Exception) -> None:
        method_by_action = {
            "create_onboarding_draft": "CMD-01",
            "create_goal_draft": "CMD-02",
            "create_inventory_change_draft": "CMD-09",
            "create_measurement_session_draft": "CMD-11",
            "create_exercise_catalog_draft": "CMD-22",
            "create_familiar_exercise_batch_draft": "CMD-25",
            "create_plan_draft": "CMD-05",
            "create_workout_report_draft": "CMD-06",
            "create_correction_draft": "CMD-07",
            "create_record_decision_draft": "CMD-08",
            "create_historical_record_draft": "CMD-24",
            "create_plan_lifecycle_draft": "CMD-23",
            "create_program_draft": "CMD-03",
            "create_program_publication_draft": "CMD-04",
            "revise_program_draft": "CMD-19",
            "mark_preview_shown": "CMD-20",
            "create_preview": "CMD-20",
            "confirm": "CMD-00",
            "create_calculation_draft": "CMD-17",
        }
        method = method_by_action.get(action)
        correlation_id = kwargs.get("correlation_id")
        causation_id = kwargs.get("causation_id")
        key = kwargs.get("key")
        if method is None or not isinstance(correlation_id, UUID) or not isinstance(key, str):
            return
        code = getattr(error, "code", "internal_error")
        with self._session_factory() as audit_session:
            now = datetime.now(UTC)
            operation_id: UUID | None = uuid4()
            request_payload = kwargs.get("payload")
            request_digest = (
                request_payload.get("request_digest") if isinstance(request_payload, dict) else None
            )
            if (
                not isinstance(request_digest, str)
                or len(request_digest) != 64
                or any(char not in "0123456789abcdef" for char in request_digest)
            ):
                request_digest = digest_payload(request_payload or {})
            inserted_operation = audit_session.execute(
                text("""
                INSERT INTO trainer.idempotent_operations(
                    operation_id,scope,idempotency_key,command_kind,request_digest,state,error_code,
                    received_at,updated_at,created_at
                ) VALUES (:operation,'local-owner:'||:method,:key,:method,:digest,'failed',:error,:now,:now,:now)
                ON CONFLICT (scope,idempotency_key) DO NOTHING
                RETURNING operation_id
            """),
                {
                    "operation": operation_id,
                    "method": method,
                    "key": key,
                    "digest": request_digest,
                    "error": str(code)[:100],
                    "now": now,
                },
            ).scalar_one_or_none()
            if inserted_operation is None:
                operation_id = None
            audit_session.execute(
                text("""
                    INSERT INTO trainer.agent_operation_audit(
                        audit_id,operation_id,method_id,object_refs,attempt_no,result_kind,
                        correlation_id,causation_id,idempotency_key_digest,error_code,started_at,finished_at,
                        latency_ms,safe_metadata,created_at
                    ) VALUES (:id,:operation,:method,'[]'::jsonb,1,'failed',:correlation,:causation,
                        :key_digest,:error,:now,:now,0,'{}'::jsonb,:now)
                """),
                {
                    "id": uuid4(),
                    "operation": operation_id,
                    "method": method,
                    "correlation": correlation_id,
                    "causation": causation_id if isinstance(causation_id, UUID) else None,
                    "key_digest": hashlib.sha256(key.encode()).hexdigest(),
                    "error": str(code)[:100],
                    "now": now,
                },
            )
            audit_session.commit()
