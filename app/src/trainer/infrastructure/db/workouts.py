from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from trainer.domain.context import digest_payload
from trainer.domain.records import (
    RECORD_RULE_VERSION,
    canonical_condition_key,
    decimal_text,
    strictly_improves,
)
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError


class WorkoutRepository:
    """D-G06 report draft and immutable aggregate writer."""

    def __init__(self, repository: ContextRepository) -> None:
        self.repo = repository
        self.session = repository.session

    def create_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None,
        _target: tuple[UUID, UUID, int, datetime, UUID] | None = None,
        _operation_id: UUID | None = None,
    ) -> dict[str, Any]:
        if _operation_id is None:
            operation_id, replay = self.repo._reserve("CMD-06", key, payload["request_digest"])
            if replay is not None:
                self.repo._audit_replay(
                    operation_id, "CMD-06", correlation_id, causation_id, key, replay
                )
                return replay
            command = "CMD-06"
        else:
            operation_id, command = _operation_id, "CMD-19"

        profile = (
            self.session.execute(
                text("""
                SELECT p.profile_id,p.current_revision_id,r.timezone
                FROM trainer.profiles p JOIN trainer.profile_revisions r
                  ON r.profile_revision_id=p.current_revision_id
                WHERE p.archived_at IS NULL
            """)
            )
            .mappings()
            .one_or_none()
        )
        if profile is None:
            raise ContextRepositoryError("not_found", "active profile not found", 404)

        body = {key: value for key, value in payload.items() if key != "request_digest"}
        report = body["report"]
        profile_id = UUID(str(profile["profile_id"]))
        session_id = _target[1] if _target is not None else uuid4()
        session_revision_id = uuid4()
        report["profile_timezone"] = profile["timezone"]
        self._validate_start_date(report, profile["timezone"])
        body.update(
            intent="workout_report",
            profile_id=str(profile_id),
            session_id=str(session_id),
            session_revision_id=str(session_revision_id),
            change_plan=[
                {
                    "object_type": "workout_session",
                    "object_id": str(session_id),
                    "revision_id": str(session_revision_id),
                }
            ],
            gaps=[],
        )
        dependencies: dict[tuple[str, str, str], dict[str, Any]] = {}

        def add_dependency(dep_type: str, object_id: UUID, revision_id: UUID, reason: str) -> None:
            key_tuple = (dep_type, str(object_id), str(revision_id))
            dependencies[key_tuple] = {
                "dependency_type": dep_type,
                "object_id": str(object_id),
                "revision_id": str(revision_id),
                "digest": self.repo._revision_digest(dep_type, revision_id),
                "reason": reason,
            }

        add_dependency(
            "profile",
            profile_id,
            UUID(str(profile["current_revision_id"])),
            "profile timezone for workout local date",
        )
        plan = self._load_exact_plan(report, profile_id)
        if plan is not None:
            add_dependency(
                "workout_plan",
                UUID(str(report["plan_id"])),
                UUID(str(report["plan_revision_id"])),
                "exact workout plan revision",
            )
            report["plan_snapshot"] = plan["snapshot"]

        self._pin_exercises(report, plan, add_dependency)
        self._resolve_replacements(report, plan)
        self._materialize_load_snapshots(report, profile_id, add_dependency)
        self._validate_load_codes(report)
        self._validate_actual_metrics(report)
        self._validate_workout_metrics(report, profile_id, add_dependency)
        body["gaps"] = self._materialize_gaps(report, plan)
        self._validate_session_type(report)
        self._materialize_execution_ids(report)
        body["dependencies"] = [dependencies[key] for key in sorted(dependencies)]
        draft_id = _target[0] if _target is not None else uuid4()
        revision_no = _target[2] if _target is not None else 1
        now = datetime.now(UTC)
        digest = digest_payload(body)
        if _target is None:
            self.session.execute(
                text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,
                latest_revision_id,expires_at,created_at)
            VALUES (:draft,'workout_report',:profile,'open',:revision,:expires,:now)
        """),
                {
                    "draft": draft_id,
                    "profile": profile_id,
                    "revision": session_revision_id,
                    "expires": now + timedelta(hours=24),
                    "now": now,
                },
            )
        else:
            target_draft, _, _, _, _ = _target
        self.session.execute(
            text("""
            INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,
                typed_payload,content_digest,trust_state,confirmation_kind,created_at)
            VALUES (:revision,:draft,:revision_no,CAST(:payload AS jsonb),:digest,
                    'draft','normal',:now)
        """),
            {
                "revision": session_revision_id,
                "draft": draft_id,
                "revision_no": revision_no,
                "payload": json.dumps(body),
                "digest": digest,
                "now": now,
            },
        )
        if _target is not None:
            target_draft, _, _, original_expiry, base_revision_id = _target
            updated = self.session.execute(
                text("""
                UPDATE trainer.drafts SET latest_revision_id=:revision
                WHERE draft_id=:draft AND latest_revision_id=:base AND state='open'
                  AND expires_at=:expires AND expires_at>:now
                RETURNING draft_id
            """),
                {
                    "revision": session_revision_id,
                    "draft": target_draft,
                    "base": base_revision_id,
                    "expires": original_expiry,
                    "now": now,
                },
            )
            if updated.scalar_one_or_none() is None:
                raise ContextRepositoryError(
                    "revision_conflict", "draft base revision changed", 409
                )
        for dep in body["dependencies"]:
            self.repo._add_dependency(
                session_revision_id,
                dep["dependency_type"],
                UUID(dep["object_id"]),
                UUID(dep["revision_id"]),
                dep["reason"],
                now,
            )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(session_revision_id),
            "revision_no": revision_no,
            "content_digest": digest,
            "dependencies": body["dependencies"],
            "gaps": body["gaps"],
            "preview_candidate": {
                "summary": "Отчёт о тренировке готов к предпросмотру",
                "confirmation_kind": "normal",
                "complete": all(gap["disposition"] == "leave_unknown" for gap in body["gaps"]),
            },
        }
        self.repo._finish(operation_id, response)
        self.repo._audit(
            operation_id, command, correlation_id, causation_id, key, body["change_plan"]
        )
        return response

    def revise_draft(
        self,
        *,
        draft_id: UUID,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None,
        operation_id: UUID,
    ) -> dict[str, Any]:
        row = (
            self.session.execute(
                text("""
                SELECT d.owner_profile_id,d.state,d.expires_at,d.latest_revision_id,
                       dr.draft_revision_id,dr.revision_no,dr.typed_payload
                FROM trainer.drafts d
                JOIN trainer.draft_revisions dr ON dr.draft_revision_id=d.latest_revision_id
                WHERE d.draft_id=:draft AND d.intent_type='workout_report'
                  AND d.owner_profile_id=(
                    SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL
                  )
                FOR UPDATE OF d
            """),
                {"draft": draft_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "workout draft not found", 404)
        if row["state"] != "open" or row["expires_at"] <= datetime.now(UTC):
            raise ContextRepositoryError("already_superseded", "draft is not open", 409)
        if str(row["draft_revision_id"]) != str(payload["base_revision_id"]):
            raise ContextRepositoryError("revision_conflict", "draft base revision changed", 409)
        active_profile = self.session.execute(
            text("SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL")
        ).scalar_one_or_none()
        if active_profile is None or row["owner_profile_id"] != active_profile:
            raise ContextRepositoryError("not_found", "workout draft not found", 404)
        original = row["typed_payload"]
        source_input = payload["changes"][0]["value"]
        revised_payload = {
            "context": payload["context"],
            "request_digest": payload["request_digest"],
            "report": source_input,
        }
        return self.create_draft(
            payload=revised_payload,
            key=key,
            correlation_id=correlation_id,
            causation_id=causation_id,
            _target=(
                draft_id,
                UUID(str(original["session_id"])),
                int(row["revision_no"]) + 1,
                row["expires_at"],
                UUID(str(row["draft_revision_id"])),
            ),
            _operation_id=operation_id,
        )

    def create_correction_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None,
    ) -> dict[str, Any]:
        operation_id, replay = self.repo._reserve("CMD-07", key, payload["request_digest"])
        if replay is not None:
            self.repo._audit_replay(
                operation_id, "CMD-07", correlation_id, causation_id, key, replay
            )
            return replay
        target = payload["target"]
        session_id, source_revision = (
            UUID(str(target["object_id"])),
            UUID(str(target["revision_id"])),
        )
        row = (
            self.session.execute(
                text("""
            SELECT s.profile_id,s.current_revision_id,s.plan_id,r.revision_no,r.plan_revision_id
            FROM trainer.workout_sessions s
            JOIN trainer.workout_session_revisions r ON r.session_id=s.session_id
              AND r.session_revision_id=s.current_revision_id
            WHERE s.session_id=:session AND s.current_revision_id=:revision
              AND s.archived_at IS NULL AND s.logically_deleted_at IS NULL
            FOR UPDATE OF s
        """),
                {"session": session_id, "revision": source_revision},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("stale_preview", "correction target is not current", 409)
        profile_revision = self.session.execute(
            text("""
            SELECT current_revision_id FROM trainer.profiles
            WHERE profile_id=:profile AND archived_at IS NULL
        """),
            {"profile": row["profile_id"]},
        ).scalar_one_or_none()
        if profile_revision is None:
            raise ContextRepositoryError("not_found", "workout owner not found", 404)
        before = json.loads(
            json.dumps(self._load_saved_tree(session_id, source_revision), default=str)
        )
        source_timezone = self.session.execute(
            text("""
            SELECT dr.typed_payload->'report'->>'profile_timezone'
            FROM trainer.workout_session_revisions r
            JOIN trainer.confirmations c ON c.confirmation_id=r.confirmation_id
            JOIN trainer.draft_revisions dr ON dr.draft_revision_id=c.draft_revision_id
            WHERE r.session_revision_id=:revision
        """),
            {"revision": source_revision},
        ).scalar_one_or_none()
        if source_timezone is None:
            source_timezone = self.session.execute(
                text("""
                SELECT r.timezone FROM trainer.profiles p
                JOIN trainer.profile_revisions r ON r.profile_revision_id=p.current_revision_id
                WHERE p.profile_id=:profile
            """),
                {"profile": row["profile_id"]},
            ).scalar_one()
        report = payload["patch"][0]["value"]
        report["profile_timezone"] = source_timezone
        self._validate_start_date(report, source_timezone)
        if report.get("plan_id") != (str(row["plan_id"]) if row["plan_id"] else None):
            raise ContextRepositoryError(
                "validation_error", "correction cannot change plan ownership", 422
            )
        if report.get("plan_revision_id") != (
            str(row["plan_revision_id"]) if row["plan_revision_id"] else None
        ):
            raise ContextRepositoryError(
                "validation_error", "correction must preserve exact plan revision", 422
            )
        pinned_revisions = {
            str(ex["exercise_id"]): str(ex["exercise_revision_id"])
            for block in before["blocks"]
            for ex in block["exercises"]
        }
        preserved_loads = {
            (
                int(block["position"]),
                int(ex["position"]),
                int(actual_set["position"]),
                int(load["position"]),
            ): {key: load.get(key) for key in ("equipment_id", "equipment_snapshot")}
            for block in before["blocks"]
            for ex in block["exercises"]
            for actual_set in ex["sets"]
            for load in actual_set["loads"]
        }
        plan = self._load_exact_plan(report, UUID(str(row["profile_id"])), require_current=False)
        dependencies: dict[tuple[str, str, str], dict[str, Any]] = {}

        def add_dependency(dep_type: str, object_id: UUID, revision_id: UUID, reason: str) -> None:
            identity = (dep_type, str(object_id), str(revision_id))
            dependencies[identity] = {
                "dependency_type": dep_type,
                "object_id": str(object_id),
                "revision_id": str(revision_id),
                "digest": self.repo._revision_digest(dep_type, revision_id),
                "reason": reason,
            }

        if plan is None:
            report["plan_snapshot"] = None
        else:
            plan_revision_id = UUID(str(report["plan_revision_id"]))
            add_dependency(
                "workout_plan",
                UUID(str(row["plan_id"])),
                plan_revision_id,
                "exact workout plan revision for corrected workout",
            )
            report["plan_snapshot"] = plan["snapshot"]

        add_dependency(
            "profile",
            UUID(str(row["profile_id"])),
            UUID(str(profile_revision)),
            "profile owner of corrected workout",
        )
        self._pin_exercises(report, plan, add_dependency, pinned_revisions=pinned_revisions)
        self._resolve_replacements(report, plan)
        self._materialize_load_snapshots(
            report,
            UUID(str(row["profile_id"])),
            add_dependency,
            preserved_snapshots=preserved_loads,
        )
        self._validate_load_codes(report)
        self._validate_actual_metrics(report)
        self._validate_workout_metrics(report, UUID(str(row["profile_id"])), add_dependency)
        gaps = self._materialize_gaps(report, plan)
        self._validate_session_type(report)
        self._materialize_execution_ids(report)
        revision_id = uuid4()
        downstream = [
            {"object_type": "derived_value", "object_id": str(item), "revision_id": str(item)}
            for item in self.session.execute(
                text("""
                SELECT DISTINCT d.derived_value_id FROM trainer.derived_values d
                JOIN trainer.derived_value_inputs i ON i.derived_value_id=d.derived_value_id
                WHERE d.state='current' AND i.source_object_type='workout_session'
                  AND i.source_object_id=:session AND i.source_revision_id=:source
                ORDER BY d.derived_value_id
            """),
                {"session": session_id, "source": source_revision},
            ).scalars()
        ]
        calculation_effects = self._plan_calculation_best_effects(session_id, source_revision)
        source_candidate_ids = [
            UUID(str(item))
            for item in self.session.execute(
                text("""
                    SELECT candidate_id FROM trainer.record_candidates
                    WHERE session_revision_id=:revision AND status='pending' ORDER BY candidate_id
                """),
                {"revision": source_revision},
            ).scalars()
        ]
        accepted_candidate_ids = [
            UUID(str(item))
            for item in self.session.execute(
                text("""
                    SELECT c.candidate_id FROM trainer.record_candidates c
                    JOIN trainer.actual_records r ON r.candidate_id=c.candidate_id
                    WHERE r.session_id=:session AND c.status='accepted'
                    ORDER BY c.candidate_id
                """),
                {"session": session_id},
            ).scalars()
        ]
        source_record_ids = [
            UUID(str(item))
            for item in self.session.execute(
                text("""
                    SELECT record_id FROM trainer.actual_records
                    WHERE session_id=:session ORDER BY record_id
                """),
                {"session": session_id},
            ).scalars()
        ]
        affected_record_ids = set(source_record_ids)
        for group in self.session.execute(
            text("""
            SELECT DISTINCT profile_id,exercise_id,metric_kind,condition_id
            FROM trainer.actual_records WHERE session_id=:session
        """),
            {"session": session_id},
        ).mappings():
            affected_record_ids.update(
                UUID(str(item))
                for item in self.session.execute(
                    text("""
                    SELECT record_id FROM trainer.actual_records
                    WHERE profile_id=:profile AND exercise_id=:exercise
                      AND metric_kind=:metric AND condition_id=:condition
                    ORDER BY record_id
                """),
                    {
                        "profile": group["profile_id"],
                        "exercise": group["exercise_id"],
                        "metric": group["metric_kind"],
                        "condition": group["condition_id"],
                    },
                ).scalars()
            )
        record_effects = {
            "pending_candidates_to_invalidate": [str(item) for item in source_candidate_ids],
            "accepted_candidates_to_reconcile": [str(item) for item in accepted_candidate_ids],
            "source_records_to_revalidate": [str(item) for item in source_record_ids],
            "affected_comparison_groups": sorted(str(item) for item in affected_record_ids),
        }
        typed = {
            "intent": "workout_report",
            "profile_id": str(row["profile_id"]),
            "session_id": str(session_id),
            "session_revision_id": str(revision_id),
            "report": report,
            "gaps": gaps,
            "correction": {
                "source_revision_id": str(source_revision),
                "revision_no": int(row["revision_no"]) + 1,
                "before": before,
                "reason": payload["reason"],
            },
            "downstream_effects": downstream,
            "calculation_effects": calculation_effects,
            "record_effects": record_effects,
            "dependencies": list(dependencies.values()),
            "change_plan": [
                {
                    "object_type": "workout_session",
                    "object_id": str(session_id),
                    "revision_id": str(revision_id),
                },
                *[
                    {
                        "object_type": "record_candidate",
                        "object_id": str(item),
                        "revision_id": None,
                    }
                    for item in source_candidate_ids
                ],
                *[
                    {
                        "object_type": "record_candidate",
                        "object_id": str(item),
                        "revision_id": None,
                    }
                    for item in accepted_candidate_ids
                ],
                *[
                    {
                        "object_type": "actual_record",
                        "object_id": str(item),
                        "revision_id": None,
                    }
                    for item in sorted(affected_record_ids, key=str)
                ],
                *[
                    {"object_type": "calculated_best", "object_id": item[key], "revision_id": None}
                    for item in calculation_effects
                    for key in ("retire_best_id", "fallback_best_id")
                    if item.get(key) is not None
                ],
            ],
        }
        typed["record_effects"]["preview_plan"] = self._plan_record_correction(
            session_id=session_id,
            old_revision=source_revision,
            before_tree=before,
            after_tree={
                "local_start_date": report["local_start_date"],
                "blocks": report["blocks"],
            },
            report=report,
            record_effects=record_effects,
        )
        digest = digest_payload(typed)
        now, draft_id = datetime.now(UTC), uuid4()
        self.session.execute(
            text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,
                latest_revision_id,expires_at,created_at)
            VALUES (:draft,'workout_report',:profile,'open',:revision,:expires,:now)
        """),
            {
                "draft": draft_id,
                "profile": row["profile_id"],
                "revision": revision_id,
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
                "revision": revision_id,
                "draft": draft_id,
                "payload": json.dumps(typed),
                "digest": digest,
                "now": now,
            },
        )
        for dep in typed["dependencies"]:
            self.repo._add_dependency(
                revision_id,
                dep["dependency_type"],
                UUID(dep["object_id"]),
                UUID(dep["revision_id"]),
                dep["reason"],
                now,
            )
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": typed["dependencies"],
            "gaps": gaps,
            "preview_candidate": {
                "summary": "Исправление тренировки готово к предпросмотру",
                "confirmation_kind": "normal",
                "complete": all(item["disposition"] == "leave_unknown" for item in gaps),
            },
        }
        self.repo._finish(operation_id, response)
        self.repo._audit(
            operation_id, "CMD-07", correlation_id, causation_id, key, typed["change_plan"]
        )
        return response

    def _validate_start_date(self, report: dict[str, Any], timezone: str) -> None:
        if report.get("started_at") is None:
            return
        started = datetime.fromisoformat(report["started_at"])
        if started.tzinfo is None:
            raise ContextRepositoryError("validation_error", "started_at requires timezone", 422)
        try:
            local_date = started.astimezone(ZoneInfo(timezone)).date()
        except ZoneInfoNotFoundError as error:
            raise ContextRepositoryError(
                "inconsistent_domain_state", "profile timezone is invalid", 500
            ) from error
        if local_date.isoformat() != report["local_start_date"]:
            raise ContextRepositoryError(
                "validation_error", "local_start_date does not match profile timezone", 422
            )

    def _plan_calculation_best_effects(
        self, session_id: UUID, source_revision_id: UUID, *, lock: bool = False
    ) -> list[dict[str, Any]]:
        locking = " FOR UPDATE OF b" if lock else ""
        affected_current_bests = list(
            self.session.execute(
                text(f"""
                    SELECT b.calculated_best_id,b.profile_id,b.exercise_id,b.metric_kind,
                           b.condition_id,b.derived_value_id,b.value,b.achieved_on
                    FROM trainer.calculated_bests b
                    JOIN trainer.derived_values d USING(derived_value_id)
                    WHERE b.is_current AND d.state='current' AND EXISTS (
                        SELECT 1 FROM trainer.derived_value_inputs i
                        WHERE i.derived_value_id=d.derived_value_id
                          AND i.source_object_type='workout_session'
                          AND i.source_object_id=:session AND i.source_revision_id=:revision
                    )
                    ORDER BY b.profile_id,b.exercise_id,b.metric_kind,b.condition_id
                    {locking}
                """),
                {"session": session_id, "revision": source_revision_id},
            )
            .mappings()
            .all()
        )
        effects = []
        for best in affected_current_bests:
            fallback = (
                self.session.execute(
                    text("""
                        SELECT p.derived_value_id,p.exercise_id,p.condition_id,p.session_id,
                               p.session_revision_id,p.source_actual_set_id,d.profile_id,
                               d.result_value AS value,r.local_start_date AS achieved_on,
                               b.calculated_best_id
                        FROM trainer.derived_progress_points p
                        JOIN trainer.derived_values d USING(derived_value_id)
                        JOIN trainer.workout_session_revisions r
                          ON r.session_id=p.session_id
                         AND r.session_revision_id=p.session_revision_id
                        LEFT JOIN trainer.calculated_bests b
                          ON b.derived_value_id=p.derived_value_id
                        WHERE d.profile_id=:profile AND p.exercise_id=:exercise
                          AND p.metric_kind=:metric AND p.condition_id=:condition
                          AND d.state='current'
                          AND NOT (p.session_id=:session AND p.session_revision_id=:revision)
                        ORDER BY d.result_value DESC,r.local_start_date ASC,d.created_at ASC,
                                 p.derived_value_id ASC
                        LIMIT 1
                    """),
                    {
                        "profile": best["profile_id"],
                        "exercise": best["exercise_id"],
                        "metric": best["metric_kind"],
                        "condition": best["condition_id"],
                        "session": session_id,
                        "revision": source_revision_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            fallback_id = (
                UUID(str(fallback["calculated_best_id"]))
                if fallback and fallback["calculated_best_id"] is not None
                else uuid5(NAMESPACE_URL, f"trainer:calculated-best:{fallback['derived_value_id']}")
                if fallback
                else None
            )
            effects.append(
                {
                    "profile_id": str(best["profile_id"]),
                    "exercise_id": str(best["exercise_id"]),
                    "metric_kind": best["metric_kind"],
                    "condition_id": str(best["condition_id"]),
                    "retire_best_id": str(best["calculated_best_id"]),
                    "retire_derived_value_id": str(best["derived_value_id"]),
                    "retire_value": str(best["value"]),
                    "retire_date": best["achieved_on"].isoformat(),
                    "fallback_best_id": str(fallback_id) if fallback_id else None,
                    "fallback_create": bool(fallback and fallback["calculated_best_id"] is None),
                    "fallback_derived_value_id": str(fallback["derived_value_id"])
                    if fallback
                    else None,
                    "fallback_session_id": str(fallback["session_id"]) if fallback else None,
                    "fallback_session_revision_id": str(fallback["session_revision_id"])
                    if fallback
                    else None,
                    "fallback_source_actual_set_id": str(fallback["source_actual_set_id"])
                    if fallback
                    else None,
                    "fallback_value": str(fallback["value"]) if fallback else None,
                    "fallback_date": fallback["achieved_on"].isoformat() if fallback else None,
                }
            )
        return effects

    def _load_exact_plan(
        self, report: dict[str, Any], profile_id: UUID, *, require_current: bool = True
    ) -> dict[str, Any] | None:
        if report.get("plan_id") is None:
            if any(
                block.get("planned_block_id") is not None
                or any(ex.get("planned_exercise_id") is not None for ex in block["exercises"])
                for block in report["blocks"]
            ):
                raise ContextRepositoryError(
                    "validation_error", "planned references require a plan", 422
                )
            return None
        row = (
            self.session.execute(
                text("""
            SELECT p.profile_id,p.current_revision_id,r.context_snapshot,r.lifecycle
            FROM trainer.workout_plans p JOIN trainer.workout_plan_revisions r
              ON r.plan_id=p.plan_id AND r.plan_revision_id=:revision
            WHERE p.plan_id=:plan AND p.profile_id=:profile
              AND p.archived_at IS NULL AND p.logically_deleted_at IS NULL
            FOR UPDATE OF p
        """),
                {
                    "plan": report["plan_id"],
                    "revision": report["plan_revision_id"],
                    "profile": profile_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "exact plan revision not found", 404)
        if require_current and str(row["current_revision_id"]) != report["plan_revision_id"]:
            raise ContextRepositoryError("stale_preview", "plan revision is not current", 409)
        snapshot = dict(row["context_snapshot"])
        plan_exercises = {
            str(item["object_id"]): str(item["revision_id"])
            for item in snapshot.get("dependencies", [])
            if item.get("dependency_type") == "exercise"
        }
        blocks = {
            str(row["planned_block_id"]): row
            for row in self.session.execute(
                text("""
                SELECT planned_block_id,position,block_type
                FROM trainer.planned_blocks WHERE plan_revision_id=:revision
            """),
                {"revision": report["plan_revision_id"]},
            ).mappings()
        }
        exercises = {
            str(row["planned_exercise_id"]): row
            for row in self.session.execute(
                text("""
                SELECT e.planned_exercise_id,e.exercise_id,e.position,e.planned_block_id
                FROM trainer.planned_exercises e
                JOIN trainer.planned_blocks b USING(planned_block_id)
                WHERE b.plan_revision_id=:revision
            """),
                {"revision": report["plan_revision_id"]},
            ).mappings()
        }
        sets = {
            str(row["planned_set_id"]): row
            for row in self.session.execute(
                text("""
                SELECT s.planned_set_id,s.planned_exercise_id,s.position
                FROM trainer.planned_sets s
                JOIN trainer.planned_exercises e USING(planned_exercise_id)
                JOIN trainer.planned_blocks b USING(planned_block_id)
                WHERE b.plan_revision_id=:revision
            """),
                {"revision": report["plan_revision_id"]},
            ).mappings()
        }
        loads = {
            str(row["planned_load_component_id"]): row
            for row in self.session.execute(
                text("""
                SELECT l.planned_load_component_id,l.planned_set_id,l.position,l.load_kind,
                       l.equipment_id,l.equipment_snapshot,l.item_count,l.mass,l.unit_code,
                       l.mass_meaning,l.direction,l.raw_ambiguous_text
                FROM trainer.planned_load_components l
                JOIN trainer.planned_sets s USING(planned_set_id)
                JOIN trainer.planned_exercises e USING(planned_exercise_id)
                JOIN trainer.planned_blocks b USING(planned_block_id)
                WHERE b.plan_revision_id=:revision
            """),
                {"revision": report["plan_revision_id"]},
            ).mappings()
        }
        return {
            "snapshot": snapshot,
            "exercises": plan_exercises,
            "blocks": blocks,
            "planned_exercises": exercises,
            "planned_sets": sets,
            "planned_loads": loads,
        }

    def _validate_session_type(self, report: dict[str, Any]) -> None:
        if (
            self.session.execute(
                text("""
            SELECT 1 FROM trainer.session_type_catalog
            WHERE code=:code AND archived_at IS NULL
        """),
                {"code": report["session_type"]},
            ).first()
            is None
        ):
            raise ContextRepositoryError("validation_error", "unknown session type", 422)

    @staticmethod
    def _materialize_execution_ids(report: dict[str, Any]) -> None:
        positions: set[tuple[int, int]] = set()
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                position = (block["position"], exercise["position"])
                if position in positions:
                    raise ContextRepositoryError(
                        "validation_error", "duplicate exercise position", 422
                    )
                positions.add(position)
                exercise["execution_id"] = str(uuid4())

    def _pin_exercises(
        self,
        report: dict[str, Any],
        plan: dict[str, Any] | None,
        add_dependency: Any,
        *,
        pinned_revisions: dict[str, str] | None = None,
    ) -> None:
        reported_planned_blocks: set[str] = set()
        for block in report["blocks"]:
            planned_block_id = block.get("planned_block_id")
            if planned_block_id is not None:
                planned_block_key = str(planned_block_id)
                if planned_block_key in reported_planned_blocks:
                    raise ContextRepositoryError(
                        "validation_error", "planned block is reported more than once", 422
                    )
                reported_planned_blocks.add(planned_block_key)
                planned_block = (plan or {}).get("blocks", {}).get(str(planned_block_id))
                if planned_block is None or planned_block["block_type"] != block["block_type_code"]:
                    raise ContextRepositoryError(
                        "validation_error",
                        "planned block is outside exact plan or has a different type",
                        422,
                    )
            exercise_positions: set[int] = set()
            for exercise in block["exercises"]:
                if exercise["position"] in exercise_positions:
                    raise ContextRepositoryError(
                        "validation_error", "duplicate exercise position", 422
                    )
                exercise_positions.add(exercise["position"])
                planned_id = exercise.get("planned_exercise_id")
                if planned_id is not None:
                    planned = (plan or {}).get("planned_exercises", {}).get(str(planned_id))
                    if planned is None or str(planned["exercise_id"]) != exercise["exercise_id"]:
                        raise ContextRepositoryError(
                            "validation_error", "planned exercise is outside exact plan", 422
                        )
                    if planned_block_id is not None and str(planned["planned_block_id"]) != str(
                        planned_block_id
                    ):
                        raise ContextRepositoryError(
                            "validation_error",
                            "planned exercise is outside the reported planned block",
                            422,
                        )
                    revision = (plan or {}).get("exercises", {}).get(str(planned["exercise_id"]))
                    if revision is None:
                        raise ContextRepositoryError(
                            "inconsistent_domain_state",
                            "plan exercise revision snapshot is missing",
                            500,
                        )
                    exercise["exercise_revision_id"] = revision
                    continue
                if exercise["result"] != "added" and not (
                    exercise["result"] == "skipped" and exercise.get("reason") == "replacement"
                ):
                    raise ContextRepositoryError(
                        "validation_error",
                        "unplanned execution must be an added exercise or an "
                        "explicit replacement marker",
                        422,
                    )
                row = (
                    self.session.execute(
                        text("""
                    SELECT coalesce(:pinned,e.current_revision_id) AS current_revision_id,
                           r.status_code
                    FROM trainer.exercises e JOIN trainer.exercise_revisions r
                      ON r.exercise_revision_id=coalesce(:pinned,e.current_revision_id)
                    WHERE e.exercise_id=:id
                      AND (:pinned IS NOT NULL OR
                           (e.archived_at IS NULL AND e.logically_deleted_at IS NULL))
                    FOR UPDATE OF e
                """),
                        {
                            "id": exercise["exercise_id"],
                            "pinned": (pinned_revisions or {}).get(str(exercise["exercise_id"])),
                        },
                    )
                    .mappings()
                    .one_or_none()
                )
                pinned_revision = (pinned_revisions or {}).get(str(exercise["exercise_id"]))
                if row is None or (pinned_revision is None and row["status_code"] != "active"):
                    raise ContextRepositoryError(
                        "validation_error", "exercise is unavailable for execution", 422
                    )
                exact_revision = pinned_revision or str(row["current_revision_id"])
                exercise["exercise_revision_id"] = exact_revision
                add_dependency(
                    "exercise_revision" if pinned_revision is not None else "exercise",
                    UUID(exercise["exercise_id"]),
                    UUID(exact_revision),
                    "preserved exact historical execution revision"
                    if pinned_revision is not None
                    else "current added or replacement-marker exercise",
                )
                self._validate_set_references(exercise, planned_id=None, plan=plan)

    def _validate_set_references(
        self, exercise: dict[str, Any], planned_id: str | None, plan: dict[str, Any] | None
    ) -> None:
        for actual_set in exercise["sets"]:
            planned_set_id = actual_set.get("planned_set_id")
            if planned_set_id is None:
                continue
            planned = (plan or {}).get("planned_sets", {}).get(str(planned_set_id))
            if (
                planned is None
                or planned_id is None
                or str(planned["planned_exercise_id"]) != str(planned_id)
            ):
                raise ContextRepositoryError(
                    "validation_error", "planned set is outside exact planned exercise", 422
                )

    def _resolve_replacements(self, report: dict[str, Any], plan: dict[str, Any] | None) -> None:
        replacement_sources: set[tuple[str, str]] = set()
        by_position = {
            (block["position"], exercise["position"]): exercise
            for block in report["blocks"]
            for exercise in block["exercises"]
        }
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                self._validate_set_references(exercise, exercise.get("planned_exercise_id"), plan)
                source = exercise.get("replacement_source")
                if source is None:
                    continue
                if exercise["result"] != "added":
                    raise ContextRepositoryError(
                        "validation_error", "replacement link requires added execution", 422
                    )
                if source["kind"] == "skipped_execution":
                    source_key = (
                        "skipped_execution",
                        f"{source['block_position']}:{source['exercise_position']}",
                    )
                    if source_key in replacement_sources:
                        raise ContextRepositoryError(
                            "validation_error", "replacement source is linked more than once", 422
                        )
                    replacement_sources.add(source_key)
                    marker = by_position.get(
                        (source["block_position"], source["exercise_position"])
                    )
                    if (
                        marker is None
                        or marker["result"] != "skipped"
                        or marker.get("reason") != "replacement"
                        or marker["sets"]
                    ):
                        raise ContextRepositoryError(
                            "validation_error",
                            "replacement source must name an explicit skipped exercise "
                            "in this report",
                            422,
                        )
                    if marker is exercise:
                        raise ContextRepositoryError(
                            "validation_error", "replacement source cannot be itself", 422
                        )
                    if plan is not None and marker.get("planned_exercise_id") is not None:
                        marker_plan_id = str(marker["planned_exercise_id"])
                        if marker_plan_id not in plan["planned_exercises"]:
                            raise ContextRepositoryError(
                                "validation_error", "replacement marker is outside exact plan", 422
                            )
                    exercise["resolved_replacement_source"] = {
                        "kind": "skipped_execution",
                        "block_position": source["block_position"],
                        "exercise_position": source["exercise_position"],
                    }
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                if (
                    exercise.get("reason") == "replacement"
                    and exercise["result"] == "skipped"
                    and not any(
                        added.get("resolved_replacement_source", {}).get("kind")
                        == "skipped_execution"
                        and added["resolved_replacement_source"]["block_position"]
                        == block["position"]
                        and added["resolved_replacement_source"]["exercise_position"]
                        == exercise["position"]
                        for b in report["blocks"]
                        for added in b["exercises"]
                    )
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unlinked replacement marker is not allowed", 422
                    )

    def _materialize_gaps(
        self, report: dict[str, Any], plan: dict[str, Any] | None
    ) -> list[dict[str, str]]:
        gaps: list[dict[str, str]] = []

        def planned_location(kind: str, identity: str) -> str:
            if plan is None:
                raise ContextRepositoryError(
                    "inconsistent_domain_state", "exact plan is missing", 500
                )
            if kind == "exercise":
                exercise = plan["planned_exercises"][identity]
                suffix = ""
            elif kind == "set":
                planned_set = plan["planned_sets"][identity]
                exercise = plan["planned_exercises"][str(planned_set["planned_exercise_id"])]
                suffix = f", плановый подход №{planned_set['position']}"
            else:
                load = plan["planned_loads"][identity]
                planned_set = plan["planned_sets"][str(load["planned_set_id"])]
                exercise = plan["planned_exercises"][str(planned_set["planned_exercise_id"])]
                suffix = (
                    f", плановый подход №{planned_set['position']}, "
                    f"компонент нагрузки №{load['position']}"
                )
            block = plan["blocks"][str(exercise["planned_block_id"])]
            return f"Блок №{block['position']}, плановое упражнение №{exercise['position']}{suffix}"

        def add_gap(path: str, explicitly_unknown: bool, detail: str) -> None:
            gaps.append(
                {
                    "path": path,
                    "code": "missing",
                    "disposition": "leave_unknown" if explicitly_unknown else "clarify",
                    "message_safe": detail,
                }
            )

        actual_exercises = [
            exercise for block in report["blocks"] for exercise in block["exercises"]
        ]
        mapped_exercises: dict[str, dict[str, Any]] = {}
        actual_sets: dict[
            tuple[int, int, int], tuple[int, int, dict[str, Any], dict[str, Any]]
        ] = {}
        actual_planned_set_ids: set[str] = set()
        actual_load_ids: set[str] = set()
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                planned_id = exercise.get("planned_exercise_id")
                if planned_id is not None:
                    planned_key = str(planned_id)
                    if planned_key in mapped_exercises:
                        raise ContextRepositoryError(
                            "validation_error", "planned exercise is reported more than once", 422
                        )
                    mapped_exercises[planned_key] = exercise
                for actual_set in exercise["sets"]:
                    actual_key = (block["position"], exercise["position"], actual_set["position"])
                    if actual_key in actual_sets:
                        raise ContextRepositoryError(
                            "validation_error", "actual set position is duplicated", 422
                        )
                    actual_sets[actual_key] = (
                        block["position"],
                        exercise["position"],
                        exercise,
                        actual_set,
                    )
                    planned_set_id = actual_set.get("planned_set_id")
                    if planned_set_id is not None:
                        planned_set_key = str(planned_set_id)
                        if planned_set_key in actual_planned_set_ids:
                            raise ContextRepositoryError(
                                "validation_error", "planned set is reported more than once", 422
                            )
                        actual_planned_set_ids.add(planned_set_key)
                    for load in actual_set["loads"]:
                        planned_load_id = load.get("planned_load_component_id")
                        if planned_load_id is not None:
                            planned_load_key = str(planned_load_id)
                            if planned_load_key in actual_load_ids:
                                raise ContextRepositoryError(
                                    "validation_error",
                                    "planned load is reported more than once",
                                    422,
                                )
                            actual_load_ids.add(planned_load_key)

        unknown_exercises = {
            str(value) for value in report["confirmed_unknown_planned_exercise_ids"]
        }
        unknown_sets = {str(value) for value in report["confirmed_unknown_planned_set_ids"]}
        unknown_loads = {
            str(value) for value in report["confirmed_unknown_planned_load_component_ids"]
        }
        unknown_metric_keys = {
            (
                item["block_position"],
                item["exercise_position"],
                item["set_position"],
                item["metric_kind"],
            )
            for item in report["confirmed_unknown_metrics"]
        }

        if plan is None:
            if unknown_exercises or unknown_sets or unknown_loads:
                raise ContextRepositoryError(
                    "validation_error", "planned unknown markers require an exact plan", 422
                )
            if not actual_exercises:
                add_gap(
                    "/report/blocks",
                    False,
                    "Укажите фактические упражнения или отметьте точные сведения неизвестными.",
                )
            for block in report["blocks"]:
                for exercise in block["exercises"]:
                    if (
                        exercise["result"] in {"performed", "partial", "added"}
                        and not exercise["sets"]
                    ):
                        add_gap(
                            f"/report/blocks/{block['position']}/exercises/{exercise['position']}/sets",
                            False,
                            "Для отмеченного упражнения не указаны фактические подходы.",
                        )
        else:
            planned_exercise_ids = set(plan["planned_exercises"])
            if not unknown_exercises <= planned_exercise_ids:
                raise ContextRepositoryError(
                    "validation_error", "unknown exercise marker is outside exact plan", 422
                )
            replaced_planned_ids = {
                str(exercise["planned_exercise_id"])
                for block in report["blocks"]
                for exercise in block["exercises"]
                if exercise["result"] == "skipped"
                and exercise.get("reason") == "replacement"
                and exercise.get("planned_exercise_id") is not None
            }
            for planned_id in unknown_exercises:
                if planned_id in mapped_exercises or planned_id in replaced_planned_ids:
                    raise ContextRepositoryError(
                        "validation_error",
                        "unknown exercise marker conflicts with reported exercise or replacement",
                        422,
                    )
            for planned_id in sorted(planned_exercise_ids):
                if planned_id in mapped_exercises or planned_id in replaced_planned_ids:
                    continue
                explicitly_unknown = planned_id in unknown_exercises
                add_gap(
                    f"/report/planned_exercises/{planned_id}",
                    explicitly_unknown,
                    f"{planned_location('exercise', planned_id)}: "
                    "Плановое упражнение не отмечено как выполненное, пропущенное, "
                    "заменённое или неизвестное.",
                )

            planned_sets = plan["planned_sets"]
            if not unknown_sets <= set(planned_sets):
                raise ContextRepositoryError(
                    "validation_error", "unknown set marker is outside exact plan", 422
                )
            for set_id in unknown_sets:
                planned_set = planned_sets[set_id]
                execution = mapped_exercises.get(str(planned_set["planned_exercise_id"]))
                if (
                    execution is None
                    or execution["result"] == "skipped"
                    or set_id in actual_planned_set_ids
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unknown set marker conflicts with report", 422
                    )
            for set_id, planned_set in planned_sets.items():
                execution = mapped_exercises.get(str(planned_set["planned_exercise_id"]))
                if execution is None or execution["result"] == "skipped":
                    continue
                if set_id in actual_planned_set_ids:
                    continue
                explicitly_unknown = set_id in unknown_sets
                add_gap(
                    f"/report/planned_sets/{set_id}",
                    explicitly_unknown,
                    f"{planned_location('set', set_id)}: "
                    "Плановый подход не отмечен как выполненный, пропущенный или неизвестный.",
                )

            planned_loads = plan["planned_loads"]
            if not unknown_loads <= set(planned_loads):
                raise ContextRepositoryError(
                    "validation_error", "unknown load marker is outside exact plan", 422
                )
            for load_id in unknown_loads:
                planned_load = planned_loads[load_id]
                parent_set_id = str(planned_load["planned_set_id"])
                execution = mapped_exercises.get(
                    str(planned_sets[parent_set_id]["planned_exercise_id"])
                )
                actual_set = next(
                    (
                        actual_set
                        for _, _, owner, actual_set in actual_sets.values()
                        if owner is execution
                        and str(actual_set.get("planned_set_id")) == parent_set_id
                    ),
                    None,
                )
                if (
                    execution is None
                    or execution["result"] == "skipped"
                    or actual_set is None
                    or actual_set["status"] == "skipped"
                    or load_id in actual_load_ids
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unknown load marker conflicts with report", 422
                    )
            for set_id in actual_planned_set_ids:
                actual_set = next(
                    aset
                    for _, _, _, aset in actual_sets.values()
                    if str(aset.get("planned_set_id")) == set_id
                )
                if actual_set["status"] == "skipped":
                    continue
                for load_id, planned_load in planned_loads.items():
                    if str(planned_load["planned_set_id"]) != set_id:
                        continue
                    if load_id in actual_load_ids:
                        continue
                    explicitly_unknown = load_id in unknown_loads
                    add_gap(
                        f"/report/planned_load_components/{load_id}",
                        explicitly_unknown,
                        f"{planned_location('load', load_id)}: "
                        "Плановый компонент нагрузки не сопоставлен с фактическим "
                        "или явно неизвестным.",
                    )

        known_metric_paths: set[tuple[int, int, int, str]] = set()
        for (block_pos, exercise_pos, set_pos), (_, _, exercise, actual_set) in actual_sets.items():
            if actual_set["status"] == "skipped":
                continue
            required = {
                row["metric_kind"]
                for row in self.session.execute(
                    text("""
                    SELECT metric_kind,requiredness FROM trainer.exercise_measurements
                    WHERE exercise_revision_id=:revision AND requiredness='required'
                """),
                    {"revision": exercise["exercise_revision_id"]},
                ).mappings()
            }
            supplied = {metric["metric_kind"] for metric in actual_set["metrics"]}
            if (
                actual_set.get("full_reps") is not None
                or actual_set.get("partial_reps") is not None
            ):
                supplied.add("repetitions")
            for metric_kind in required:
                key = (block_pos, exercise_pos, set_pos, metric_kind)
                known_metric_paths.add(key)
                if metric_kind in supplied:
                    if key in unknown_metric_keys:
                        raise ContextRepositoryError(
                            "validation_error", "unknown metric marker conflicts with value", 422
                        )
                    continue
                explicitly_unknown = key in unknown_metric_keys
                add_gap(
                    f"/report/blocks/{block_pos}/exercises/{exercise_pos}/sets/{set_pos}/metrics/{metric_kind}",
                    explicitly_unknown,
                    "Обязательная метрика отсутствует; укажите значение или явно "
                    "оставьте его неизвестным.",
                )
        if unknown_metric_keys - known_metric_paths:
            raise ContextRepositoryError(
                "validation_error", "unknown metric marker is not a missing required metric", 422
            )
        return sorted(gaps, key=lambda item: item["path"])

    def _materialize_load_snapshots(
        self,
        report: dict[str, Any],
        profile_id: UUID,
        add_dependency: Any,
        *,
        preserved_snapshots: dict[tuple[int, int, int, int], dict[str, Any]] | None = None,
    ) -> None:
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                for actual_set in exercise["sets"]:
                    for load in actual_set["loads"]:
                        key = (
                            block["position"],
                            exercise["position"],
                            actual_set["position"],
                            load["position"],
                        )
                        preserved = (preserved_snapshots or {}).get(key)
                        if (
                            preserved is not None
                            and load.get("planned_load_component_id") is None
                            and str(load.get("equipment_id")) == str(preserved["equipment_id"])
                        ):
                            load["equipment_snapshot"] = preserved["equipment_snapshot"]
                            load["assembly_snapshot"] = None
                            continue
                        planned_load_id = load.get("planned_load_component_id")
                        if planned_load_id is not None:
                            if (
                                report.get("plan_snapshot") is None
                                or actual_set.get("planned_set_id") is None
                            ):
                                raise ContextRepositoryError(
                                    "validation_error", "planned load requires exact plan", 422
                                )
                            source = (
                                self.session.execute(
                                    text("""
                                SELECT l.* FROM trainer.planned_load_components l
                                JOIN trainer.planned_sets s USING(planned_set_id)
                                JOIN trainer.planned_exercises e USING(planned_exercise_id)
                                JOIN trainer.planned_blocks b USING(planned_block_id)
                                WHERE l.planned_load_component_id=:load
                                  AND l.planned_set_id=:set_id AND b.plan_revision_id=:revision
                            """),
                                    {
                                        "load": planned_load_id,
                                        "set_id": actual_set["planned_set_id"],
                                        "revision": report["plan_revision_id"],
                                    },
                                )
                                .mappings()
                                .one_or_none()
                            )
                            if source is None:
                                raise ContextRepositoryError(
                                    "validation_error",
                                    "planned load is outside exact planned set",
                                    422,
                                )
                            if load["load_kind"] != source["load_kind"]:
                                raise ContextRepositoryError(
                                    "validation_error", "planned load kind differs", 422
                                )
                        equipment_id = load.get("equipment_id")
                        if equipment_id is None and planned_load_id is not None:
                            if source is None:
                                raise ContextRepositoryError(
                                    "inconsistent_domain_state",
                                    "planned load snapshot disappeared during materialization",
                                    500,
                                )
                            equipment_id = source["equipment_id"]
                            load["equipment_id"] = str(equipment_id) if equipment_id else None
                            snapshot = dict(source["equipment_snapshot"])
                        elif equipment_id is not None:
                            row = (
                                self.session.execute(
                                    text("""
                                SELECT i.profile_id,i.current_revision_id,to_jsonb(r) AS revision
                                FROM trainer.equipment_items i
                                JOIN trainer.equipment_item_revisions r
                                  ON r.equipment_revision_id=i.current_revision_id
                                WHERE i.equipment_id=:id
                                  AND i.archived_at IS NULL AND i.logically_deleted_at IS NULL
                            """),
                                    {"id": equipment_id},
                                )
                                .mappings()
                                .one_or_none()
                            )
                            if row is None or str(row["profile_id"]) != str(profile_id):
                                raise ContextRepositoryError(
                                    "validation_error", "equipment item is unavailable", 422
                                )
                            revision_id = UUID(str(row["current_revision_id"]))
                            add_dependency(
                                "equipment_item",
                                UUID(str(equipment_id)),
                                revision_id,
                                "actual load equipment snapshot",
                            )
                            snapshot = {
                                "equipment_id": str(equipment_id),
                                "revision_id": str(revision_id),
                                "revision": self.repo._json_row(row["revision"]),
                            }
                        else:
                            snapshot = {"equipment_id": None}
                        load["equipment_snapshot"] = snapshot
                        load["assembly_snapshot"] = None

    def _validate_actual_metrics(self, report: dict[str, Any]) -> None:
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                unilateral = self.session.execute(
                    text("""
                    SELECT unilateral FROM trainer.exercise_revisions
                    WHERE exercise_revision_id=:revision
                """),
                    {"revision": exercise["exercise_revision_id"]},
                ).scalar_one()
                allowed = {
                    row["metric_kind"]: row["requiredness"]
                    for row in self.session.execute(
                        text("""
                        SELECT metric_kind,requiredness FROM trainer.exercise_measurements
                        WHERE exercise_revision_id=:revision
                    """),
                        {"revision": exercise["exercise_revision_id"]},
                    ).mappings()
                }
                for actual_set in exercise["sets"]:
                    if unilateral and actual_set.get("side") not in {"left", "right"}:
                        raise ContextRepositoryError(
                            "validation_error",
                            "unilateral sets require separate left or right rows",
                            422,
                        )
                    if (
                        block["block_type_code"] in {"circuit", "superset"}
                        and actual_set.get("round_no") is None
                    ):
                        raise ContextRepositoryError(
                            "validation_error", "circuit and superset sets require round_no", 422
                        )
                    if (
                        block.get("declared_rounds") is not None
                        and actual_set.get("round_no") is not None
                        and actual_set["round_no"] > block["declared_rounds"]
                    ):
                        raise ContextRepositoryError(
                            "validation_error", "set round exceeds declared rounds", 422
                        )
                    for metric in actual_set["metrics"]:
                        requirement = allowed.get(metric["metric_kind"])
                        if requirement is None:
                            raise ContextRepositoryError(
                                "validation_error",
                                "metric is not allowed for exact exercise revision",
                                422,
                            )
                        family = self.session.execute(
                            text("""
                            SELECT mapping.family_code FROM trainer.unit_family_mapping mapping
                            WHERE mapping.unit_code=:unit
                        """),
                            {"unit": metric["unit_code"]},
                        ).scalar_one_or_none()
                        if (
                            family is None
                            or self.session.execute(
                                text("""
                            SELECT 1 FROM trainer.exercise_measurements
                            WHERE exercise_revision_id=:revision
                              AND metric_kind=:kind AND unit_family=:family
                        """),
                                {
                                    "revision": exercise["exercise_revision_id"],
                                    "kind": metric["metric_kind"],
                                    "family": family,
                                },
                            ).first()
                            is None
                        ):
                            raise ContextRepositoryError(
                                "validation_error",
                                "metric unit is incompatible with exact exercise revision",
                                422,
                            )

    def _validate_load_codes(self, report: dict[str, Any]) -> None:
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                for actual_set in exercise["sets"]:
                    for load in actual_set["loads"]:
                        if (load["load_kind"] == "band") != (load.get("direction") is not None):
                            raise ContextRepositoryError(
                                "validation_error",
                                "band requires direction; other load kinds prohibit it",
                                422,
                            )
                        if (
                            self.session.execute(
                                text("""
                            SELECT 1 FROM trainer.load_kind_catalog
                            WHERE code=:code AND archived_at IS NULL
                        """),
                                {"code": load["load_kind"]},
                            ).first()
                            is None
                        ):
                            raise ContextRepositoryError(
                                "validation_error", "unknown load kind", 422
                            )
                        if (
                            load.get("direction") is not None
                            and self.session.execute(
                                text("""
                            SELECT 1 FROM trainer.load_direction_catalog
                            WHERE code=:code AND archived_at IS NULL
                        """),
                                {"code": load["direction"]},
                            ).first()
                            is None
                        ):
                            raise ContextRepositoryError(
                                "validation_error", "unknown load direction", 422
                            )

    def _validate_workout_metrics(
        self, report: dict[str, Any], profile_id: UUID, add_dependency: Any
    ) -> None:
        for metric in report["workout_metrics"]:
            if (
                self.session.execute(
                    text("""
                SELECT 1 FROM trainer.workout_metric_compatibility
                WHERE metric_kind=:kind AND unit_code=:unit
            """),
                    {"kind": metric["metric_kind"], "unit": metric["unit_code"]},
                ).first()
                is None
            ):
                raise ContextRepositoryError(
                    "validation_error", "workout metric and unit are incompatible", 422
                )
            derived_id = metric.get("derived_value_id")
            if metric["source_kind"] == "calculation":
                row = (
                    self.session.execute(
                        text("""
                    SELECT profile_id,state,result_value,unit_code FROM trainer.derived_values
                    WHERE derived_value_id=:id
                """),
                        {"id": derived_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    row is None
                    or str(row["profile_id"]) != str(profile_id)
                    or row["state"] != "current"
                    or Decimal(str(row["result_value"])) != Decimal(str(metric["value"]))
                    or row["unit_code"] != metric["unit_code"]
                ):
                    raise ContextRepositoryError(
                        "validation_error",
                        "calculated metric must match a current exact DerivedValue",
                        422,
                    )
                add_dependency(
                    "derived_value",
                    UUID(derived_id),
                    UUID(derived_id),
                    "calculated workout metric provenance",
                )

    def commit(
        self,
        typed: dict[str, Any],
        confirmation_id: UUID,
        result_id: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> list[UUID]:
        report = typed["report"]
        session_id = UUID(typed["session_id"])
        revision_id = UUID(typed["session_revision_id"])
        profile_id = UUID(typed["profile_id"])
        correction = typed.get("correction")
        if correction is not None:
            current_effects = self._plan_calculation_best_effects(
                session_id, UUID(correction["source_revision_id"]), lock=True
            )
            if current_effects != typed.get("calculation_effects", []):
                raise ContextRepositoryError(
                    "stale_dependency", "calculated-best correction effects changed", 409
                )
        plan_id = UUID(report["plan_id"]) if report.get("plan_id") else None
        plan_revision_id = (
            UUID(report["plan_revision_id"]) if report.get("plan_revision_id") else None
        )
        if correction is None:
            self.session.execute(
                text("""
                INSERT INTO trainer.workout_sessions(
                    session_id,profile_id,plan_id,current_revision_id,created_at
                ) VALUES (:id,:profile,:plan,:revision,:now)
            """),
                {
                    "id": session_id,
                    "profile": profile_id,
                    "plan": plan_id,
                    "revision": revision_id,
                    "now": now,
                },
            )
        else:
            current_row = (
                self.session.execute(
                    text("""
                SELECT current_revision_id,plan_id FROM trainer.workout_sessions
                WHERE session_id=:session AND profile_id=:profile FOR UPDATE
            """),
                    {"session": session_id, "profile": profile_id},
                )
                .mappings()
                .one_or_none()
            )
            current = dict(current_row) if current_row is not None else None
            if (
                current is None
                or str(current["current_revision_id"]) != correction["source_revision_id"]
                or current["plan_id"] != plan_id
            ):
                raise ContextRepositoryError("stale_preview", "correction target changed", 409)
        self.session.execute(
            text("""
            INSERT INTO trainer.workout_session_revisions(
                session_revision_id,session_id,revision_no,
                plan_revision_id,started_at,local_start_date,ended_at,duration_seconds,session_type,
                name,lifecycle,effort,comment,report_gaps,supersedes_revision_id,confirmation_id,created_at)
            VALUES (:revision,:session,:revision_no,:plan_revision,:started,:local_date,
                :ended,:duration,:type,:name,:lifecycle,:effort,:comment,
                CAST(:gaps AS jsonb),:supersedes,:confirmation,:now)
        """),
            {
                "revision": revision_id,
                "session": session_id,
                "revision_no": correction["revision_no"] if correction else 1,
                "plan_revision": plan_revision_id,
                "started": report.get("started_at"),
                "local_date": report["local_start_date"],
                "ended": report.get("ended_at"),
                "duration": report.get("duration_seconds"),
                "type": report["session_type"],
                "name": report.get("name"),
                "lifecycle": report["lifecycle"],
                "effort": report.get("effort"),
                "comment": report.get("comment"),
                "gaps": json.dumps(typed.get("gaps", [])),
                "supersedes": UUID(correction["source_revision_id"]) if correction else None,
                "confirmation": confirmation_id,
                "now": now,
            },
        )
        invalidated: list[UUID] = []
        execution_ids: dict[tuple[int, int], UUID] = {
            (block["position"], exercise["position"]): UUID(exercise["execution_id"])
            for block in report["blocks"]
            for exercise in block["exercises"]
        }
        replacement_rows: list[tuple[UUID, dict[str, Any]]] = []
        for block in report["blocks"]:
            block_id = uuid4()
            self.session.execute(
                text("""
                INSERT INTO trainer.actual_blocks(
                    actual_block_id,session_revision_id,planned_block_id,
                    position,block_type,declared_rounds,rest_seconds,comment,created_at)
                VALUES (:id,:revision,:planned,:position,:type,:rounds,:rest,:comment,:now)
            """),
                {
                    "id": block_id,
                    "revision": revision_id,
                    "planned": block.get("planned_block_id"),
                    "position": block["position"],
                    "type": block["block_type_code"],
                    "rounds": block.get("declared_rounds"),
                    "rest": block.get("rest_seconds"),
                    "comment": block.get("comment"),
                    "now": now,
                },
            )
            for exercise in block["exercises"]:
                execution_id = execution_ids[(block["position"], exercise["position"])]
                self.session.execute(
                    text("""
                    INSERT INTO trainer.exercise_executions(
                        execution_id,actual_block_id,exercise_id,
                        exercise_revision_id,planned_exercise_id,position,result,reason,created_at)
                    VALUES (
                        :id,:block,:exercise,:exercise_revision,:planned,:position,:result,:reason,:now
                    )
                """),
                    {
                        "id": execution_id,
                        "block": block_id,
                        "exercise": exercise["exercise_id"],
                        "exercise_revision": exercise["exercise_revision_id"],
                        "planned": exercise.get("planned_exercise_id"),
                        "position": exercise["position"],
                        "result": exercise["result"],
                        "reason": exercise.get("reason"),
                        "now": now,
                    },
                )
                for actual_set in exercise["sets"]:
                    set_id = uuid4()
                    self.session.execute(
                        text("""
                        INSERT INTO trainer.actual_sets(
                            actual_set_id,execution_id,planned_set_id,position,
                            round_no,logical_set_no,side,set_type,full_reps,partial_reps,failed_attempts,
                            reported_rpe,reported_rir,failure_reached,rom_state,technique_state,tempo,
                            pause_seconds,rest_after_seconds,status,comment,created_at)
                        VALUES (:id,:execution,:planned,:position,:round,:logical,:side,:type,:reps,
                            :partial,:failed,:rpe,:rir,:failure,:rom,:technique,:tempo,:pause,:rest,
                            :status,:comment,:now)
                    """),
                        {
                            "id": set_id,
                            "execution": execution_id,
                            "planned": actual_set.get("planned_set_id"),
                            "position": actual_set["position"],
                            "round": actual_set.get("round_no"),
                            "logical": actual_set["logical_set_no"],
                            "side": actual_set.get("side"),
                            "type": actual_set["set_type_code"],
                            "reps": actual_set.get("full_reps"),
                            "partial": actual_set.get("partial_reps"),
                            "failed": actual_set.get("failed_attempts"),
                            "rpe": actual_set.get("reported_rpe"),
                            "rir": actual_set.get("reported_rir"),
                            "failure": actual_set.get("failure_reached"),
                            "rom": actual_set.get("rom_state"),
                            "technique": actual_set.get("technique_state"),
                            "tempo": actual_set.get("tempo"),
                            "pause": actual_set.get("pause_seconds"),
                            "rest": actual_set.get("rest_after_seconds"),
                            "status": actual_set["status"],
                            "comment": actual_set.get("comment"),
                            "now": now,
                        },
                    )
                    for metric in actual_set["metrics"]:
                        self.session.execute(
                            text("""
                            INSERT INTO trainer.actual_metric_values(
                                actual_metric_value_id,actual_set_id,
                                metric_kind,value,unit_code,custom_definition,source_kind,created_at)
                            VALUES (:id,:set,:kind,:value,:unit,CAST(:custom AS jsonb),:source,:now)
                        """),
                            {
                                "id": uuid4(),
                                "set": set_id,
                                "kind": metric["metric_kind"],
                                "value": metric["value"],
                                "unit": metric["unit_code"],
                                "custom": json.dumps(metric["custom_definition"])
                                if metric.get("custom_definition") is not None
                                else None,
                                "source": metric["source_kind"],
                                "now": now,
                            },
                        )
                    for load in actual_set["loads"]:
                        self.session.execute(
                            text("""
                            INSERT INTO trainer.actual_load_components(
                                actual_load_component_id,actual_set_id,
                                planned_load_component_id,position,load_kind,equipment_id,equipment_snapshot,
                                item_count,mass,unit_code,mass_meaning,direction,raw_ambiguous_text,
                                assembly_snapshot,created_at)
                            VALUES (
                                :id,:set,:planned,:position,:kind,:equipment,
                                CAST(:snapshot AS jsonb),
                                :count,:mass,:unit,:meaning,:direction,:ambiguous,
                                CAST(:assembly AS jsonb),:now
                            )
                        """),
                            {
                                "id": uuid4(),
                                "set": set_id,
                                "planned": load.get("planned_load_component_id"),
                                "position": load["position"],
                                "kind": load["load_kind"],
                                "equipment": load.get("equipment_id"),
                                "snapshot": json.dumps(load["equipment_snapshot"]),
                                "count": load.get("item_count"),
                                "mass": load.get("mass"),
                                "unit": load.get("unit_code"),
                                "meaning": load.get("mass_meaning"),
                                "direction": load.get("direction"),
                                "ambiguous": load.get("raw_ambiguous_text"),
                                "assembly": None,
                                "now": now,
                            },
                        )
                source = exercise.get("resolved_replacement_source")
                if source is not None:
                    replacement_rows.append((execution_id, source))
        for added_id, source in replacement_rows:
            self.session.execute(
                text("""
                INSERT INTO trainer.replacement_links(
                    added_execution_id,skipped_execution_id,created_at
                )
                VALUES (:added,:skipped,:now)
            """),
                {
                    "added": added_id,
                    "skipped": execution_ids[
                        (source["block_position"], source["exercise_position"])
                    ]
                    if source["kind"] == "skipped_execution"
                    else None,
                    "now": now,
                },
            )
        for metric in report["workout_metrics"]:
            self.session.execute(
                text("""
                INSERT INTO trainer.workout_metrics(
                    workout_metric_id,session_revision_id,metric_kind,
                    value,unit_code,source_kind,derived_value_id,created_at)
                VALUES (:id,:revision,:kind,:value,:unit,:source,:derived,:now)
            """),
                {
                    "id": uuid4(),
                    "revision": revision_id,
                    "kind": metric["metric_kind"],
                    "value": metric["value"],
                    "unit": metric["unit_code"],
                    "source": metric["source_kind"],
                    "derived": metric.get("derived_value_id"),
                    "now": now,
                },
            )
        if correction is not None:
            updated = self.session.execute(
                text("""
                UPDATE trainer.workout_sessions SET current_revision_id=:revision
                WHERE session_id=:session AND current_revision_id=:source
                RETURNING session_id
            """),
                {
                    "revision": revision_id,
                    "session": session_id,
                    "source": UUID(correction["source_revision_id"]),
                },
            ).scalar_one_or_none()
            if updated is None:
                raise ContextRepositoryError("stale_preview", "correction target changed", 409)
            self._reconcile_corrected_records(
                old_revision=UUID(correction["source_revision_id"]),
                new_revision=revision_id,
                session_id=session_id,
                profile_id=profile_id,
                before_tree=correction["before"],
                after_tree=self._load_saved_tree(session_id, revision_id),
                record_effects=typed.get("record_effects", {}),
                result_id=result_id,
                changes=changes,
                now=now,
            )
        after = self._load_saved_tree(session_id, revision_id)
        self._create_record_candidates(
            session_id=session_id,
            revision_id=revision_id,
            profile_id=profile_id,
            confirmation_id=confirmation_id,
            now=now,
            changes=changes,
            result_id=result_id,
        )
        before = correction["before"] if correction else None
        self.repo._insert_change(
            changes[0],
            result_id,
            "correct" if correction else "create",
            before,
            after,
            correction["reason"] if correction else None,
            now,
        )
        if correction is not None:
            old_revision = UUID(correction["source_revision_id"])
            invalidated = list(
                self.session.execute(
                    text("""
                UPDATE trainer.derived_values d SET state='stale',
                    stale_reason='source workout revision was corrected'
                WHERE d.state='current' AND EXISTS (
                    SELECT 1 FROM trainer.derived_value_inputs i
                    WHERE i.derived_value_id=d.derived_value_id
                      AND i.source_object_type='workout_session'
                      AND i.source_object_id=:session AND i.source_revision_id=:source
                ) RETURNING d.derived_value_id
            """),
                    {"session": session_id, "source": old_revision},
                )
                .scalars()
                .all()
            )
            for effect in typed.get("calculation_effects", []):
                retired_id = UUID(effect["retire_best_id"])
                retired = self.session.execute(
                    text("""
                        UPDATE trainer.calculated_bests SET is_current=false
                        WHERE calculated_best_id=:id AND is_current
                        RETURNING calculated_best_id
                    """),
                    {"id": retired_id},
                ).scalar_one_or_none()
                if retired is None:
                    raise ContextRepositoryError(
                        "stale_dependency", "calculated best changed during correction", 409
                    )
                retired_change = {
                    "object_type": "calculated_best",
                    "object_id": str(retired_id),
                    "revision_id": None,
                }
                self.repo._insert_change(
                    retired_change,
                    result_id,
                    "correct",
                    {
                        "is_current": True,
                        "value": effect["retire_value"],
                        "achieved_on": effect["retire_date"],
                        "derived_value_id": effect["retire_derived_value_id"],
                    },
                    {
                        "is_current": False,
                        "reason": "source workout revision was corrected",
                    },
                    correction["reason"],
                    now,
                )
                fallback_id = effect.get("fallback_best_id")
                if fallback_id is not None:
                    fallback_uuid = UUID(fallback_id)
                    if effect.get("fallback_create"):
                        self.session.execute(
                            text("""
                                INSERT INTO trainer.calculated_bests(
                                    calculated_best_id,profile_id,exercise_id,metric_kind,
                                    value,unit_code,condition_id,derived_value_id,achieved_on,
                                    source_session_id,source_session_revision_id,
                                    source_actual_set_id,is_current,supersedes_best_id,
                                    confirmation_id,created_at
                                )
                                SELECT :id,d.profile_id,p.exercise_id,p.metric_kind,
                                       d.result_value,d.unit_code,p.condition_id,p.derived_value_id,
                                       r.local_start_date,p.session_id,p.session_revision_id,
                                       p.source_actual_set_id,true,:previous,:confirmation,:now
                                FROM trainer.derived_progress_points p
                                JOIN trainer.derived_values d USING(derived_value_id)
                                JOIN trainer.workout_session_revisions r
                                  ON r.session_id=p.session_id
                                 AND r.session_revision_id=p.session_revision_id
                                WHERE p.derived_value_id=:derived AND d.state='current'
                                  AND NOT EXISTS (
                                      SELECT 1 FROM trainer.calculated_bests b
                                      WHERE b.derived_value_id=p.derived_value_id
                                  )
                                RETURNING derived_value_id
                            """),
                            {
                                "id": fallback_uuid,
                                "previous": retired_id,
                                "confirmation": confirmation_id,
                                "now": now,
                                "derived": UUID(effect["fallback_derived_value_id"]),
                            },
                        ).scalar_one_or_none()
                    else:
                        promoted = self.session.execute(
                            text("""
                                UPDATE trainer.calculated_bests b SET is_current=true
                                WHERE b.calculated_best_id=:id AND NOT b.is_current
                                  AND EXISTS (
                                      SELECT 1 FROM trainer.derived_values d
                                      WHERE d.derived_value_id=b.derived_value_id
                                        AND d.state='current'
                                  )
                                RETURNING b.derived_value_id
                            """),
                            {"id": fallback_uuid},
                        ).scalar_one_or_none()
                        if promoted is None:
                            raise ContextRepositoryError(
                                "stale_dependency", "fallback calculated best changed", 409
                            )
                    fallback_change = {
                        "object_type": "calculated_best",
                        "object_id": str(fallback_uuid),
                        "revision_id": None,
                    }
                    self.repo._insert_change(
                        fallback_change,
                        result_id,
                        "create" if effect.get("fallback_create") else "correct",
                        None if effect.get("fallback_create") else {"is_current": False},
                        {
                            "is_current": True,
                            "value": effect["fallback_value"],
                            "achieved_on": effect["fallback_date"],
                            "reason": "best restored from remaining current calculations",
                        },
                        correction["reason"],
                        now,
                    )
        return invalidated

    def _create_record_candidates(
        self,
        *,
        session_id: UUID,
        revision_id: UUID,
        profile_id: UUID,
        confirmation_id: UUID,
        now: datetime,
        changes: list[dict[str, Any]],
        result_id: UUID,
    ) -> list[UUID]:
        """Create eligible facts under the workout confirmation transaction."""
        rows = list(
            self.session.execute(
                text("""
            SELECT e.exercise_id,e.exercise_revision_id,b.position AS block_position,
                   e.position AS exercise_position,s.actual_set_id,s.position AS set_position,
                   s.status,s.full_reps,s.partial_reps,s.side,r.local_start_date,er.unilateral
            FROM trainer.actual_sets s
            JOIN trainer.exercise_executions e USING(execution_id)
            JOIN trainer.exercise_revisions er ON er.exercise_revision_id=e.exercise_revision_id
            JOIN trainer.actual_blocks b USING(actual_block_id)
            JOIN trainer.workout_session_revisions r USING(session_revision_id)
            WHERE b.session_revision_id=:revision
            ORDER BY b.position,e.position,s.position
        """),
                {"revision": revision_id},
            ).mappings()
        )
        winners: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            if row["status"] != "performed" or (
                row["partial_reps"] is not None and row["partial_reps"] > 0
            ):
                continue
            side = row["side"]
            if side is None:
                if row["unilateral"]:
                    continue
                side = "not_applicable"
            set_id = UUID(str(row["actual_set_id"]))
            metrics = {
                item["metric_kind"]: item
                for item in self.session.execute(
                    text("""
                    SELECT metric_kind,value,unit_code FROM trainer.actual_metric_values
                    WHERE actual_set_id=:set
                """),
                    {"set": set_id},
                ).mappings()
            }
            loads = list(
                self.session.execute(
                    text("""
                SELECT load_kind,equipment_id,equipment_snapshot,item_count,mass,unit_code,
                       mass_meaning,direction,raw_ambiguous_text
                FROM trainer.actual_load_components WHERE actual_set_id=:set ORDER BY position
            """),
                    {"set": set_id},
                ).mappings()
            )
            signature: list[dict[str, Any]] = []
            complete = True
            external_numeric: list[dict[str, Any]] = []
            for load in loads:
                kind = load["load_kind"]
                raw = load["raw_ambiguous_text"]
                if raw is not None:
                    complete = False
                    break
                item: dict[str, Any] = {
                    "load_kind": kind,
                    "direction": load["direction"],
                    "equipment_type": None,
                    "equipment_id": str(load["equipment_id"]) if load["equipment_id"] else None,
                    "item_count": decimal_text(load["item_count"]),
                    "mass_kg": None,
                    "mass_meaning": load["mass_meaning"],
                }
                snapshot = dict(load["equipment_snapshot"] or {})
                equipment_type = (snapshot.get("revision") or {}).get("equipment_type")
                if not equipment_type:
                    equipment_type = snapshot.get("equipment_type")
                item["equipment_type"] = equipment_type
                if kind == "external":
                    if (
                        load["mass"] is None
                        or load["unit_code"] != "kilogram"
                        or load["mass_meaning"] not in ("total_external_mass", "per_item_mass")
                        or not equipment_type
                        or load["item_count"] is None
                    ):
                        complete = False
                        break
                    item["mass_kg"] = decimal_text(load["mass"])
                    external_numeric.append(dict(item))
                elif kind == "band":
                    if load["equipment_id"] is None or load["direction"] not in (
                        "assistance",
                        "resistance",
                    ):
                        complete = False
                        break
                    if load["mass"] is None:
                        item["mass_kg"] = None
                    elif load["unit_code"] == "kilogram":
                        item["mass_kg"] = decimal_text(load["mass"])
                    else:
                        complete = False
                        break
                elif kind == "bodyweight":
                    # Body mass is deliberately contextual and never enters this key.
                    item["equipment_type"] = "bodyweight"
                    item["mass_kg"] = None
                else:
                    complete = False
                    break
                signature.append(item)
            if not complete:
                continue
            exercise_id = UUID(str(row["exercise_id"]))
            exercise_revision_id = UUID(str(row["exercise_revision_id"]))
            base = {
                "exercise_id": exercise_id,
                "exercise_revision_id": exercise_revision_id,
                "actual_set_id": set_id,
                "achieved_on": row["local_start_date"],
                "side": side,
                "load_signature": signature,
                "block_position": row["block_position"],
                "exercise_position": row["exercise_position"],
                "set_position": row["set_position"],
            }
            specs: list[
                tuple[str, Decimal, str, int | None, Decimal | None, list[dict[str, Any]]]
            ] = []
            reps = row["full_reps"]
            reported_reps = metrics.get("repetitions")
            if (
                reps is not None
                and reps > 0
                and (reported_reps is None or Decimal(str(reported_reps["value"])) == Decimal(reps))
                and signature
            ):
                specs.append(
                    ("record_full_reps", Decimal(reps), "repetition", None, None, signature)
                )
            duration = metrics.get("duration")
            if (
                duration
                and Decimal(str(duration["value"])) > 0
                and duration["unit_code"] == "second"
                and signature
            ):
                specs.append(
                    (
                        "record_hold_duration",
                        Decimal(str(duration["value"])),
                        "second",
                        None,
                        None,
                        signature,
                    )
                )
            if reps is not None and reps > 0 and len(external_numeric) == 1:
                mass = Decimal(external_numeric[0]["mass_kg"])
                comparison_loads = [dict(item) for item in signature]
                comparison_loads[
                    signature.index(next(i for i in signature if i["load_kind"] == "external"))
                ]["mass_kg"] = None
                specs.append(
                    ("record_external_load", mass, "kilogram", reps, None, comparison_loads)
                )
            distance = metrics.get("distance")
            if (
                duration
                and distance
                and Decimal(str(duration["value"])) > 0
                and Decimal(str(distance["value"])) > 0
                and duration["unit_code"] == "second"
                and distance["unit_code"] == "meter"
            ):
                specs.append(
                    (
                        "record_cardio_time",
                        Decimal(str(duration["value"])),
                        "second",
                        None,
                        Decimal(str(distance["value"])),
                        signature,
                    )
                )
            for metric_kind, value, unit, fixed_reps, fixed_distance, load_signature in specs:
                key_body = {
                    "metric_kind": metric_kind,
                    "unit_code": unit,
                    "side": side,
                    "load_signature": load_signature,
                    "fixed_reps": fixed_reps,
                    "fixed_distance_m": decimal_text(fixed_distance)
                    if fixed_distance is not None
                    else None,
                    "rule_version": RECORD_RULE_VERSION,
                }
                condition_key = canonical_condition_key(key_body)
                readable = {**key_body, "scope": "comparable", "unknown_fields": []}
                identity = (str(exercise_id), metric_kind, condition_key)
                candidate = {
                    **base,
                    "metric_kind": metric_kind,
                    "value": value,
                    "unit_code": unit,
                    "condition_key": condition_key,
                    "condition_snapshot": readable,
                    "condition_body": key_body,
                    "fixed_reps": fixed_reps,
                    "fixed_distance_m": fixed_distance,
                }
                winner = winners.get(identity)
                if winner is None or strictly_improves(metric_kind, value, winner["value"]):
                    winners[identity] = candidate
        created: list[UUID] = []
        for _key, candidate in sorted(winners.items()):
            condition_id = self.session.execute(
                text("""
                INSERT INTO trainer.comparison_conditions(condition_id,exercise_id,rule_version,
                    normalized_key,readable_snapshot,created_at)
                VALUES (:id,:exercise,:version,:key,CAST(:snapshot AS jsonb),:now)
                ON CONFLICT(exercise_id,rule_version,normalized_key) DO NOTHING
                RETURNING condition_id
            """),
                {
                    "id": uuid4(),
                    "exercise": candidate["exercise_id"],
                    "version": RECORD_RULE_VERSION,
                    "key": candidate["condition_key"],
                    "snapshot": json.dumps(candidate["condition_snapshot"]),
                    "now": now,
                },
            ).scalar_one_or_none()
            if condition_id is None:
                condition_id = self.session.execute(
                    text("""
                    SELECT condition_id FROM trainer.comparison_conditions
                    WHERE exercise_id=:exercise AND rule_version=:version AND normalized_key=:key
                """),
                    {
                        "exercise": candidate["exercise_id"],
                        "version": RECORD_RULE_VERSION,
                        "key": candidate["condition_key"],
                    },
                ).scalar_one()
            group_key = ":".join(
                (
                    str(profile_id),
                    str(candidate["exercise_id"]),
                    candidate["metric_kind"],
                    candidate["condition_key"],
                )
            )
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": group_key}
            )
            current = self.session.execute(
                text("""
                SELECT value FROM trainer.actual_records WHERE profile_id=:profile
                  AND exercise_id=:exercise AND metric_kind=:metric AND condition_id=:condition
                  AND is_current FOR UPDATE
            """),
                {
                    "profile": profile_id,
                    "exercise": candidate["exercise_id"],
                    "metric": candidate["metric_kind"],
                    "condition": condition_id,
                },
            ).scalar_one_or_none()
            if current is not None and not strictly_improves(
                candidate["metric_kind"], candidate["value"], Decimal(str(current))
            ):
                continue
            candidate_id = uuid4()
            self.session.execute(
                text("""
                INSERT INTO trainer.record_candidates(candidate_id,profile_id,exercise_id,
                    exercise_revision_id,metric_kind,value,unit_code,condition_id,session_revision_id,
                    actual_set_id,technique_self_report,status,decision_confirmation_id,invalidated_reason,created_at)
                VALUES (:id,:profile,:exercise,:exercise_revision,:metric,:value,:unit,:condition,
                    :revision,:set,NULL,'pending',NULL,NULL,:now)
            """),
                {
                    "id": candidate_id,
                    "profile": profile_id,
                    "exercise": candidate["exercise_id"],
                    "exercise_revision": candidate["exercise_revision_id"],
                    "metric": candidate["metric_kind"],
                    "value": candidate["value"],
                    "unit": candidate["unit_code"],
                    "condition": condition_id,
                    "revision": revision_id,
                    "set": candidate["actual_set_id"],
                    "now": now,
                },
            )
            created.append(candidate_id)
            change = {
                "object_type": "record_candidate",
                "object_id": str(candidate_id),
                "revision_id": None,
            }
            self.repo._insert_change(
                change,
                result_id,
                "create",
                None,
                {
                    "candidate_id": str(candidate_id),
                    "profile_id": str(profile_id),
                    "exercise_id": str(candidate["exercise_id"]),
                    "metric_kind": candidate["metric_kind"],
                    "value": str(candidate["value"]),
                    "unit_code": candidate["unit_code"],
                    "condition_id": str(condition_id),
                    "session_revision_id": str(revision_id),
                    "actual_set_id": str(candidate["actual_set_id"]),
                },
                "eligible confirmed workout fact",
                now,
            )
            changes.append(change)
        return created

    @staticmethod
    def _set_evidence_fingerprint(
        *,
        local_start_date: Any,
        exercise: dict[str, Any],
        actual_set: dict[str, Any],
    ) -> str:
        metrics = sorted(
            [
                {
                    "metric_kind": item["metric_kind"],
                    "value": decimal_text(item["value"]),
                    "unit_code": item["unit_code"],
                }
                for item in actual_set.get("metrics", [])
            ],
            key=lambda item: item["metric_kind"],
        )
        loads = []
        for item in sorted(actual_set.get("loads", []), key=lambda value: value["position"]):
            snapshot = dict(item.get("equipment_snapshot") or {})
            equipment_type = (snapshot.get("revision") or {}).get("equipment_type")
            if not equipment_type:
                equipment_type = snapshot.get("equipment_type")
            loads.append(
                {
                    "load_kind": item["load_kind"],
                    "direction": item["direction"],
                    "equipment_type": equipment_type,
                    "equipment_id": str(item["equipment_id"]) if item["equipment_id"] else None,
                    "item_count": decimal_text(item["item_count"]),
                    "mass": decimal_text(item["mass"]),
                    "unit_code": item["unit_code"],
                    "mass_meaning": item["mass_meaning"],
                    "raw_ambiguous_text": item["raw_ambiguous_text"],
                }
            )
        return digest_payload(
            {
                "exercise_id": str(exercise["exercise_id"]),
                "exercise_revision_id": str(exercise["exercise_revision_id"]),
                "local_start_date": str(local_start_date),
                "status": actual_set["status"],
                "set_type": actual_set.get("set_type", actual_set.get("set_type_code")),
                "full_reps": actual_set["full_reps"],
                "partial_reps": actual_set["partial_reps"],
                "side": actual_set["side"],
                "metrics": metrics,
                "loads": loads,
                "rom_state": actual_set["rom_state"],
                "technique_state": actual_set["technique_state"],
            }
        )

    def _record_set_fingerprints(self, tree: dict[str, Any]) -> dict[UUID, list[tuple[UUID, str]]]:
        result: dict[UUID, list[tuple[UUID, str]]] = {}
        root_date = tree["local_start_date"]
        for block_index, block in enumerate(tree["blocks"], start=1):
            for exercise_index, exercise in enumerate(block["exercises"], start=1):
                for set_index, actual_set in enumerate(exercise["sets"], start=1):
                    fingerprint = self._set_evidence_fingerprint(
                        local_start_date=root_date, exercise=exercise, actual_set=actual_set
                    )
                    actual_set_id = actual_set.get("actual_set_id") or uuid5(
                        NAMESPACE_URL,
                        ":".join(
                            (
                                str(exercise["exercise_id"]),
                                str(block.get("position", block_index)),
                                str(exercise.get("position", exercise_index)),
                                str(actual_set.get("position", set_index)),
                            )
                        ),
                    )
                    result.setdefault(UUID(str(exercise["exercise_id"])), []).append(
                        (UUID(str(actual_set_id)), fingerprint)
                    )
        return result

    def _plan_record_correction(
        self,
        *,
        session_id: UUID,
        old_revision: UUID,
        before_tree: dict[str, Any],
        after_tree: dict[str, Any],
        report: dict[str, Any],
        record_effects: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Describe correction outcomes without creating domain rows or IDs."""
        before = self._record_set_fingerprints(before_tree)
        after = self._record_set_fingerprints(after_tree)
        before_by_set = {
            actual_set_id: fingerprint
            for values in before.values()
            for actual_set_id, fingerprint in values
        }
        before_counts = {
            (exercise_id, fingerprint): sum(value == fingerprint for _, value in values)
            for exercise_id, values in before.items()
            for _, fingerprint in values
        }
        after_counts = {
            (exercise_id, fingerprint): sum(value == fingerprint for _, value in values)
            for exercise_id, values in after.items()
            for _, fingerprint in values
        }
        records = list(
            self.session.execute(
                text("""
                    SELECT r.record_id,r.profile_id,r.exercise_id,r.metric_kind,r.value,
                           r.condition_id,r.actual_set_id,r.is_current,r.achieved_on,
                           r.created_at,r.candidate_id,
                           r.source_fingerprint,
                           coalesce(dc.after_snapshot->>'validity','valid') AS validity
                    FROM trainer.actual_records r
                    LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                      WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                      ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                    WHERE r.session_id=:session ORDER BY r.record_id
                """),
                {"session": session_id},
            ).mappings()
        )
        expected_source_record_ids = set(record_effects.get("source_records_to_revalidate", []))
        actual_source_record_ids = {str(item["record_id"]) for item in records}
        if actual_source_record_ids != expected_source_record_ids:
            raise ContextRepositoryError(
                "stale_preview", "source records changed after correction preview", 409
            )
        changed: list[dict[str, Any]] = []
        affected: set[tuple[UUID, UUID, str, UUID]] = set()
        for record in records:
            record_id = UUID(str(record["record_id"]))
            old_fp = before_by_set.get(UUID(str(record["actual_set_id"])))
            exercise_id = UUID(str(record["exercise_id"]))
            equivalent = (
                old_fp is not None
                and record["source_fingerprint"] == old_fp
                and before_counts.get((exercise_id, old_fp), 0) == 1
                and after_counts.get((exercise_id, old_fp), 0) == 1
            )
            if equivalent or record["validity"] == "source_invalidated":
                outcome = "retained_valid"
            else:
                outcome = "invalidated_source"
                if record["is_current"]:
                    affected.add(
                        (
                            UUID(str(record["profile_id"])),
                            exercise_id,
                            record["metric_kind"],
                            UUID(str(record["condition_id"])),
                        )
                    )
            changed.append(
                {
                    "record_id": str(record_id),
                    "candidate_id": str(record["candidate_id"]) if record["candidate_id"] else None,
                    "was_current": bool(record["is_current"]),
                    "outcome": outcome,
                    "value": str(record["value"]),
                }
            )
        plan: list[dict[str, Any]] = []
        invalidated_record_ids = {
            item["record_id"] for item in changed if item["outcome"] == "invalidated_source"
        }
        for candidate_id in record_effects.get("pending_candidates_to_invalidate", []):
            plan.append(
                {"candidate_id": candidate_id, "outcome": "invalidated_pending_source_revision"}
            )
        for item in changed:
            plan.append(
                {
                    "record_id": item["record_id"],
                    "candidate_id": item["candidate_id"],
                    "outcome": item["outcome"],
                    "was_current": item["was_current"],
                    "value": item["value"],
                }
            )
        for profile, exercise, metric, condition in sorted(
            affected, key=lambda x: tuple(map(str, x))
        ):
            key = self.session.execute(
                text("""
                    SELECT normalized_key FROM trainer.comparison_conditions
                    WHERE condition_id=:id
                """),
                {"id": condition},
            ).scalar_one()
            fallback: list[dict[str, Any]] = []
            group_records: list[dict[str, Any]] = []
            if "historical_unknown" not in key:
                rows = (
                    self.session.execute(
                        text("""
                        SELECT r.record_id,r.value,r.achieved_on,r.created_at
                        FROM trainer.actual_records r
                        LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                          WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                          ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                        WHERE r.profile_id=:profile AND r.exercise_id=:exercise
                          AND r.metric_kind=:metric AND r.condition_id=:condition
                          AND coalesce(dc.after_snapshot->>'validity','valid')='valid'
                        ORDER BY r.achieved_on,r.created_at,r.record_id
                    """),
                        {
                            "profile": profile,
                            "exercise": exercise,
                            "metric": metric,
                            "condition": condition,
                        },
                    )
                    .mappings()
                    .all()
                )
                rows = [row for row in rows if str(row["record_id"]) not in invalidated_record_ids]
                winner = None
                for row in rows:
                    if winner is None or strictly_improves(
                        metric, Decimal(str(row["value"])), Decimal(str(winner["value"]))
                    ):
                        winner = row
                fallback = [
                    {"record_id": str(row["record_id"]), "value": str(row["value"])} for row in rows
                ]
                group_records = [
                    {"record_id": str(row["record_id"]), "value": str(row["value"])} for row in rows
                ]
            else:
                group_records = []
            plan.append(
                {
                    "exercise_id": str(exercise),
                    "metric_kind": metric,
                    "condition_id": str(condition),
                    "outcome": "recompute_current",
                    "fallback_candidates": fallback,
                    "group_records_digest": digest_payload(group_records),
                    "fallback_winner_record_id": str(winner["record_id"])
                    if fallback and winner
                    else None,
                }
            )
        plan.extend(self._preview_record_candidate_potentials(report, plan))
        return plan

    def _preview_record_candidate_potentials(
        self, report: dict[str, Any], correction_plan: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Purely describe candidates derivable from a corrected report draft."""
        winners: dict[tuple[str, str, str], dict[str, Any]] = {}
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                unilateral = self.session.execute(
                    text("""
                        SELECT unilateral FROM trainer.exercise_revisions
                        WHERE exercise_revision_id=:revision
                    """),
                    {"revision": exercise["exercise_revision_id"]},
                ).scalar_one()
                for actual_set in exercise["sets"]:
                    if actual_set["status"] != "performed" or (
                        actual_set["partial_reps"] is not None and actual_set["partial_reps"] > 0
                    ):
                        continue
                    side = actual_set["side"]
                    if side is None:
                        if unilateral:
                            continue
                        side = "not_applicable"
                    signature: list[dict[str, Any]] = []
                    external_numeric: list[dict[str, Any]] = []
                    complete = True
                    for load in sorted(actual_set["loads"], key=lambda item: item["position"]):
                        if load.get("raw_ambiguous_text") is not None:
                            complete = False
                            break
                        snapshot = dict(load.get("equipment_snapshot") or {})
                        revision_snapshot = snapshot.get("revision") or {}
                        equipment_type = revision_snapshot.get("equipment_type") or snapshot.get(
                            "equipment_type"
                        )
                        item = {
                            "load_kind": load["load_kind"],
                            "direction": load.get("direction"),
                            "equipment_type": equipment_type,
                            "equipment_id": str(load["equipment_id"])
                            if load.get("equipment_id")
                            else None,
                            "item_count": decimal_text(load.get("item_count")),
                            "mass_kg": None,
                            "mass_meaning": load.get("mass_meaning"),
                        }
                        if load["load_kind"] == "external":
                            if (
                                load.get("mass") is None
                                or load.get("unit_code") != "kilogram"
                                or load.get("mass_meaning")
                                not in ("total_external_mass", "per_item_mass")
                                or not equipment_type
                                or load.get("item_count") is None
                            ):
                                complete = False
                                break
                            item["mass_kg"] = decimal_text(load["mass"])
                            external_numeric.append(dict(item))
                        elif load["load_kind"] == "band":
                            if load.get("equipment_id") is None or load.get("direction") not in (
                                "assistance",
                                "resistance",
                            ):
                                complete = False
                                break
                            if load.get("mass") is not None:
                                if load.get("unit_code") != "kilogram":
                                    complete = False
                                    break
                                item["mass_kg"] = decimal_text(load["mass"])
                        elif load["load_kind"] == "bodyweight":
                            item["equipment_type"] = "bodyweight"
                        else:
                            complete = False
                            break
                        signature.append(item)
                    if not complete:
                        continue
                    metrics = {item["metric_kind"]: item for item in actual_set["metrics"]}
                    reps = actual_set["full_reps"]
                    reported_reps = metrics.get("repetitions")
                    specs: list[
                        tuple[str, Decimal, str, int | None, Decimal | None, list[dict[str, Any]]]
                    ] = []
                    if (
                        reps is not None
                        and reps > 0
                        and signature
                        and (
                            reported_reps is None
                            or Decimal(str(reported_reps["value"])) == Decimal(reps)
                        )
                    ):
                        specs.append(
                            ("record_full_reps", Decimal(reps), "repetition", None, None, signature)
                        )
                    duration = metrics.get("duration")
                    if (
                        duration
                        and Decimal(str(duration["value"])) > 0
                        and duration["unit_code"] == "second"
                        and signature
                    ):
                        specs.append(
                            (
                                "record_hold_duration",
                                Decimal(str(duration["value"])),
                                "second",
                                None,
                                None,
                                signature,
                            )
                        )
                    if reps is not None and reps > 0 and len(external_numeric) == 1:
                        comparison_loads = [dict(item) for item in signature]
                        ext_index = next(
                            i for i, item in enumerate(signature) if item["load_kind"] == "external"
                        )
                        comparison_loads[ext_index]["mass_kg"] = None
                        specs.append(
                            (
                                "record_external_load",
                                Decimal(external_numeric[0]["mass_kg"]),
                                "kilogram",
                                reps,
                                None,
                                comparison_loads,
                            )
                        )
                    distance = metrics.get("distance")
                    if (
                        duration
                        and distance
                        and Decimal(str(duration["value"])) > 0
                        and Decimal(str(distance["value"])) > 0
                        and duration["unit_code"] == "second"
                        and distance["unit_code"] == "meter"
                    ):
                        specs.append(
                            (
                                "record_cardio_time",
                                Decimal(str(duration["value"])),
                                "second",
                                None,
                                Decimal(str(distance["value"])),
                                signature,
                            )
                        )
                    for metric, value, unit, fixed_reps, fixed_distance, loads in specs:
                        key_body = {
                            "metric_kind": metric,
                            "unit_code": unit,
                            "side": side,
                            "load_signature": loads,
                            "fixed_reps": fixed_reps,
                            "fixed_distance_m": decimal_text(fixed_distance),
                            "rule_version": RECORD_RULE_VERSION,
                        }
                        condition_key = canonical_condition_key(key_body)
                        identity = (str(exercise["exercise_id"]), metric, condition_key)
                        candidate = {
                            "exercise_id": str(exercise["exercise_id"]),
                            "exercise_revision_id": str(exercise["exercise_revision_id"]),
                            "metric_kind": metric,
                            "value": value,
                            "unit_code": unit,
                            "condition_key": condition_key,
                            "condition": {**key_body, "scope": "comparable", "unknown_fields": []},
                            "partial_reps_unknown": actual_set["partial_reps"] is None,
                            "block_position": block["position"],
                            "exercise_position": exercise["position"],
                            "set_position": actual_set["position"],
                        }
                        current = winners.get(identity)
                        if current is None or strictly_improves(metric, value, current["value"]):
                            winners[identity] = candidate
        outcomes: list[dict[str, Any]] = []
        for candidate in winners.values():
            current_row = (
                self.session.execute(
                    text("""
                    SELECT r.record_id,r.value FROM trainer.actual_records r
                    JOIN trainer.comparison_conditions cc USING(condition_id)
                    WHERE r.profile_id=(
                        SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL
                    )
                      AND r.exercise_id=:exercise AND r.metric_kind=:metric
                      AND cc.normalized_key=:key AND r.is_current
                """),
                    {
                        "exercise": candidate["exercise_id"],
                        "metric": candidate["metric_kind"],
                        "key": candidate["condition_key"],
                    },
                )
                .mappings()
                .one_or_none()
            )
            current = dict(current_row) if current_row is not None else None
            planned_group = next(
                (
                    item
                    for item in correction_plan
                    if item.get("outcome") == "recompute_current"
                    and item["exercise_id"] == candidate["exercise_id"]
                    and item["metric_kind"] == candidate["metric_kind"]
                    and str(
                        self.session.execute(
                            text("""
                                SELECT normalized_key FROM trainer.comparison_conditions
                                WHERE condition_id=:id
                            """),
                            {"id": item["condition_id"]},
                        ).scalar_one()
                    )
                    == candidate["condition_key"]
                ),
                None,
            )
            if planned_group is not None:
                winner_id = planned_group.get("fallback_winner_record_id")
                fallback = next(
                    (
                        row
                        for row in planned_group["fallback_candidates"]
                        if row["record_id"] == winner_id
                    ),
                    None,
                )
                current = {"record_id": winner_id, "value": fallback["value"]} if fallback else None
            eligible = current is None or strictly_improves(
                candidate["metric_kind"], candidate["value"], Decimal(str(current["value"]))
            )
            outcomes.append(
                {
                    **candidate,
                    "value": decimal_text(candidate["value"]),
                    "outcome": "candidate_if_confirmed" if eligible else "not_strictly_better",
                    "current_record_id": str(current["record_id"]) if current else None,
                    "current_value": str(current["value"]) if current else None,
                }
            )
        return sorted(
            outcomes,
            key=lambda item: (
                item["exercise_id"],
                item["metric_kind"],
                item["condition_key"],
                item["block_position"],
                item["exercise_position"],
                item["set_position"],
            ),
        )

    def source_fingerprint_for_set(self, actual_set_id: UUID) -> str:
        row = (
            self.session.execute(
                text("""
            SELECT r.local_start_date,e.exercise_id,e.exercise_revision_id,s.*
            FROM trainer.actual_sets s
            JOIN trainer.exercise_executions e USING(execution_id)
            JOIN trainer.actual_blocks b USING(actual_block_id)
            JOIN trainer.workout_session_revisions r USING(session_revision_id)
            WHERE s.actual_set_id=:set
        """),
                {"set": actual_set_id},
            )
            .mappings()
            .one()
        )
        actual_set = dict(row)
        actual_set["metrics"] = [
            dict(item)
            for item in self.session.execute(
                text("""
            SELECT metric_kind,value,unit_code FROM trainer.actual_metric_values
            WHERE actual_set_id=:set ORDER BY metric_kind
        """),
                {"set": actual_set_id},
            ).mappings()
        ]
        actual_set["loads"] = [
            dict(item)
            for item in self.session.execute(
                text("""
            SELECT position,load_kind,direction,equipment_id,equipment_snapshot,item_count,mass,
                   unit_code,mass_meaning,raw_ambiguous_text
            FROM trainer.actual_load_components WHERE actual_set_id=:set ORDER BY position
        """),
                {"set": actual_set_id},
            ).mappings()
        ]
        exercise = {
            "exercise_id": row["exercise_id"],
            "exercise_revision_id": row["exercise_revision_id"],
        }
        return self._set_evidence_fingerprint(
            local_start_date=row["local_start_date"], exercise=exercise, actual_set=actual_set
        )

    def _reconcile_corrected_records(
        self,
        *,
        old_revision: UUID,
        new_revision: UUID,
        session_id: UUID,
        profile_id: UUID,
        before_tree: dict[str, Any],
        after_tree: dict[str, Any],
        record_effects: dict[str, Any],
        result_id: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        before = self._record_set_fingerprints(before_tree)
        after = self._record_set_fingerprints(after_tree)
        before_by_set = {
            actual_set_id: fingerprint
            for values in before.values()
            for actual_set_id, fingerprint in values
        }
        before_counts = {
            (exercise_id, fingerprint): sum(value == fingerprint for _, value in values)
            for exercise_id, values in before.items()
            for _, fingerprint in values
        }
        after_counts = {
            (exercise_id, fingerprint): sum(value == fingerprint for _, value in values)
            for exercise_id, values in after.items()
            for _, fingerprint in values
        }
        changes_by_id = {str(item["object_id"]): item for item in changes}
        candidate_ids = [
            UUID(value) for value in record_effects.get("pending_candidates_to_invalidate", [])
        ]
        for candidate_id in candidate_ids:
            candidate = (
                self.session.execute(
                    text("""
                    SELECT candidate_id,status FROM trainer.record_candidates
                    WHERE candidate_id=:id AND session_revision_id=:revision FOR UPDATE
                """),
                    {"id": candidate_id, "revision": old_revision},
                )
                .mappings()
                .one_or_none()
            )
            if candidate is None or candidate["status"] != "pending":
                continue
            self.session.execute(
                text("""
                    UPDATE trainer.record_candidates SET status='invalidated',
                      invalidated_reason='source_revision_superseded'
                    WHERE candidate_id=:id AND status='pending'
                """),
                {"id": candidate_id},
            )
            self.repo._insert_change(
                changes_by_id[str(candidate_id)],
                result_id,
                "correct",
                {"candidate_id": str(candidate_id), "status": "pending"},
                {
                    "candidate_id": str(candidate_id),
                    "status": "invalidated",
                    "invalidated_reason": "source_revision_superseded",
                    "source_revision_id": str(old_revision),
                    "replacement_revision_id": str(new_revision),
                },
                "source_revision_superseded",
                now,
            )

        source_records = list(
            self.session.execute(
                text("""
                    SELECT r.*,cc.normalized_key,
                      coalesce(dc.after_snapshot->>'validity','valid') AS current_validity
                    FROM trainer.actual_records r
                    JOIN trainer.comparison_conditions cc USING(condition_id)
                    LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                      WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                      ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                    WHERE r.session_id=:session
                    ORDER BY r.record_id FOR UPDATE OF r
                """),
                {"session": session_id},
            ).mappings()
        )
        lost_current_groups: set[tuple[UUID, UUID, str, UUID]] = set()
        for record in source_records:
            old_fingerprint = before_by_set.get(UUID(str(record["actual_set_id"])))
            exercise_key = UUID(str(record["exercise_id"]))
            equivalent = (
                old_fingerprint is not None
                and record["source_fingerprint"] == old_fingerprint
                and before_counts.get((exercise_key, old_fingerprint), 0) == 1
                and after_counts.get((exercise_key, old_fingerprint), 0) == 1
            )
            if equivalent or record["current_validity"] == "source_invalidated":
                continue
            if record["is_current"]:
                self.session.execute(
                    text("UPDATE trainer.actual_records SET is_current=false WHERE record_id=:id"),
                    {"id": record["record_id"]},
                )
                lost_current_groups.add(
                    (
                        UUID(str(record["profile_id"])),
                        UUID(str(record["exercise_id"])),
                        record["metric_kind"],
                        UUID(str(record["condition_id"])),
                    )
                )
            after_snapshot = {
                "record_id": str(record["record_id"]),
                "is_current": False,
                "validity": "source_invalidated",
                "current_exit_reason": "source_corrected",
                "source_revision_id": str(old_revision),
                "replacement_revision_id": str(new_revision),
            }
            self.repo._insert_change(
                changes_by_id[str(record["record_id"])],
                result_id,
                "correct",
                {**dict(record), "is_current": bool(record["is_current"])},
                after_snapshot,
                "source_corrected",
                now,
            )
            if record["candidate_id"] is not None:
                candidate = (
                    self.session.execute(
                        text("""
                        SELECT candidate_id,status FROM trainer.record_candidates
                        WHERE candidate_id=:id FOR UPDATE
                    """),
                        {"id": record["candidate_id"]},
                    )
                    .mappings()
                    .one_or_none()
                )
                if candidate is not None and candidate["status"] == "accepted":
                    candidate_id = UUID(str(candidate["candidate_id"]))
                    candidate_change = changes_by_id.get(str(candidate_id))
                    if candidate_change is None:
                        raise ContextRepositoryError(
                            "inconsistent_domain_state",
                            "accepted candidate missing from correction change plan",
                            500,
                        )
                    self.session.execute(
                        text("""
                            UPDATE trainer.record_candidates
                            SET status='invalidated',invalidated_reason='source_corrected'
                            WHERE candidate_id=:id AND status='accepted'
                        """),
                        {"id": candidate_id},
                    )
                    self.repo._insert_change(
                        candidate_change,
                        result_id,
                        "correct",
                        {"candidate_id": str(candidate_id), "status": "accepted"},
                        {
                            "candidate_id": str(candidate_id),
                            "status": "invalidated",
                            "invalidated_reason": "source_corrected",
                            "record_id": str(record["record_id"]),
                            "source_revision_id": str(old_revision),
                            "replacement_revision_id": str(new_revision),
                        },
                        "source_corrected",
                        now,
                    )

        for profile, exercise, metric, condition in sorted(
            lost_current_groups, key=lambda item: tuple(map(str, item))
        ):
            condition_row = self.session.execute(
                text(
                    """SELECT normalized_key FROM trainer.comparison_conditions
                       WHERE condition_id=:id"""
                ),
                {"id": condition},
            ).scalar_one()
            if "historical_unknown" in condition_row:
                continue
            rows = list(
                self.session.execute(
                    text("""
                        SELECT r.*,
                          coalesce(dc.after_snapshot->>'validity','valid') AS current_validity
                        FROM trainer.actual_records r
                        LEFT JOIN LATERAL (SELECT after_snapshot FROM trainer.domain_changes d
                          WHERE d.object_type='actual_record' AND d.object_id=r.record_id
                          ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
                        WHERE r.profile_id=:profile AND r.exercise_id=:exercise
                          AND r.metric_kind=:metric
                          AND r.condition_id=:condition AND NOT r.is_current
                          AND coalesce(dc.after_snapshot->>'validity','valid')='valid'
                        ORDER BY r.achieved_on,r.created_at,r.record_id FOR UPDATE OF r
                    """),
                    {
                        "profile": profile,
                        "exercise": exercise,
                        "metric": metric,
                        "condition": condition,
                    },
                ).mappings()
            )
            winner = None
            for row in rows:
                if winner is None or strictly_improves(
                    metric, Decimal(str(row["value"])), Decimal(str(winner["value"]))
                ):
                    winner = row
            if winner is None:
                continue
            self.session.execute(
                text("UPDATE trainer.actual_records SET is_current=true WHERE record_id=:id"),
                {"id": winner["record_id"]},
            )
            self.repo._insert_change(
                changes_by_id[str(winner["record_id"])],
                result_id,
                "correct",
                {**dict(winner), "is_current": False},
                {
                    "record_id": str(winner["record_id"]),
                    "is_current": True,
                    "validity": "valid",
                    "current_exit_reason": None,
                    "current_exit_change_id": None,
                    "restored_after_source_correction": str(new_revision),
                },
                "source correction restored the best remaining confirmed record",
                now,
            )

    def _load_saved_tree(self, session_id: UUID, revision_id: UUID) -> dict[str, Any]:
        """Application-path exact read-back used before the transaction commits."""
        root = (
            self.session.execute(
                text("""
            SELECT s.session_id,s.profile_id,s.plan_id,r.session_revision_id,r.plan_revision_id,
                   r.started_at,r.local_start_date,r.ended_at,r.duration_seconds,r.session_type,
                   r.name,r.lifecycle,r.effort,r.comment,r.report_gaps
            FROM trainer.workout_sessions s JOIN trainer.workout_session_revisions r
              ON r.session_id=s.session_id AND r.session_revision_id=:revision
            WHERE s.session_id=:session AND r.session_revision_id=:revision
        """),
                {"session": session_id, "revision": revision_id},
            )
            .mappings()
            .one()
        )
        tree: dict[str, Any] = self.repo._json_row(root)
        tree["blocks"] = []
        for block in self.session.execute(
            text("""
            SELECT * FROM trainer.actual_blocks
            WHERE session_revision_id=:revision ORDER BY position
        """),
            {"revision": revision_id},
        ).mappings():
            item = dict(block)
            item["exercises"] = []
            for exercise in self.session.execute(
                text("""
                SELECT * FROM trainer.exercise_executions
                WHERE actual_block_id=:block ORDER BY position
            """),
                {"block": block["actual_block_id"]},
            ).mappings():
                ex = dict(exercise)
                ex["sets"] = []
                for actual_set in self.session.execute(
                    text("""
                    SELECT * FROM trainer.actual_sets
                    WHERE execution_id=:execution ORDER BY position
                """),
                    {"execution": exercise["execution_id"]},
                ).mappings():
                    aset = dict(actual_set)
                    aset["metrics"] = [
                        dict(row)
                        for row in self.session.execute(
                            text("""
                        SELECT metric_kind,value,unit_code,custom_definition,source_kind
                        FROM trainer.actual_metric_values
                        WHERE actual_set_id=:set ORDER BY metric_kind
                    """),
                            {"set": actual_set["actual_set_id"]},
                        ).mappings()
                    ]
                    aset["loads"] = [
                        dict(row)
                        for row in self.session.execute(
                            text("""
                        SELECT * FROM trainer.actual_load_components
                        WHERE actual_set_id=:set ORDER BY position
                    """),
                            {"set": actual_set["actual_set_id"]},
                        ).mappings()
                    ]
                    ex["sets"].append(aset)
                ex["replacement_source"] = (
                    self.session.execute(
                        text("""
                    SELECT skipped_execution_id
                    FROM trainer.replacement_links WHERE added_execution_id=:execution
                """),
                        {"execution": exercise["execution_id"]},
                    )
                    .mappings()
                    .one_or_none()
                )
                item["exercises"].append(ex)
            tree["blocks"].append(item)
        tree["workout_metrics"] = [
            dict(row)
            for row in self.session.execute(
                text("""
            SELECT metric_kind,value,unit_code,source_kind,derived_value_id
            FROM trainer.workout_metrics WHERE session_revision_id=:revision ORDER BY metric_kind
        """),
                {"revision": revision_id},
            ).mappings()
        ]
        return tree

    def query_record_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        from base64 import urlsafe_b64decode, urlsafe_b64encode

        filters = {
            "exercise_id": payload.get("exercise_id"),
            "metric_kind": payload.get("metric_kind"),
            "status": payload.get("status", "pending"),
        }
        cursor_created = cursor_id = None
        if payload.get("cursor"):
            try:
                cursor = json.loads(
                    urlsafe_b64decode(payload["cursor"] + "=" * (-len(payload["cursor"]) % 4))
                )
                if cursor["filters"] != filters:
                    raise ValueError
                cursor_created = datetime.fromisoformat(cursor["created_at"])
                cursor_id = UUID(cursor["candidate_id"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise ContextRepositoryError(
                    "validation_error", "record cursor is invalid", 400
                ) from error
        where = [
            "c.profile_id=(SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL)",
            "c.status=:status",
        ]
        params: dict[str, Any] = {"status": filters["status"], "limit": payload["limit"] + 1}
        if filters["exercise_id"]:
            where.append("c.exercise_id=:exercise")
            params["exercise"] = filters["exercise_id"]
        if filters["metric_kind"]:
            where.append("c.metric_kind=:metric")
            params["metric"] = filters["metric_kind"]
        if cursor_created is not None:
            where.append("(c.created_at,c.candidate_id)<(:created,:candidate)")
            params.update(created=cursor_created, candidate=cursor_id)
        rows = list(
            self.session.execute(
                text(f"""
            SELECT c.*,r.local_start_date,s.session_id,s.current_revision_id,e.exercise_revision_id,
                   cc.rule_version,cc.readable_snapshot,cc.normalized_key,
                   cr.record_id AS current_record_id,cr.value AS current_value
            FROM trainer.record_candidates c
            JOIN trainer.workout_session_revisions r USING(session_revision_id)
            JOIN trainer.workout_sessions s USING(session_id)
            JOIN trainer.actual_sets aset ON aset.actual_set_id=c.actual_set_id
            JOIN trainer.exercise_executions e ON e.execution_id=aset.execution_id
              AND e.exercise_id=c.exercise_id
            JOIN trainer.actual_blocks b ON b.actual_block_id=e.actual_block_id
              AND b.session_revision_id=c.session_revision_id
            JOIN trainer.comparison_conditions cc USING(condition_id)
            LEFT JOIN trainer.actual_records cr ON cr.profile_id=c.profile_id
              AND cr.exercise_id=c.exercise_id AND cr.metric_kind=c.metric_kind
              AND cr.condition_id=c.condition_id AND cr.is_current
            WHERE {" AND ".join(where)}
            ORDER BY c.created_at DESC,c.candidate_id DESC LIMIT :limit
        """),
                params,
            ).mappings()
        )
        has_more = len(rows) > payload["limit"]
        rows = rows[: payload["limit"]]
        items = []
        for row in rows:
            condition_snapshot = dict(row["readable_snapshot"])
            if "load_signature" in condition_snapshot:
                condition_snapshot["loads"] = condition_snapshot.pop("load_signature")
            condition = {
                **condition_snapshot,
                "condition_id": row["condition_id"],
                "normalized_key": row["normalized_key"],
            }
            eligible = (
                row["status"] == "pending"
                and row["current_revision_id"] == row["session_revision_id"]
                and (
                    row["current_value"] is None
                    or strictly_improves(
                        row["metric_kind"],
                        Decimal(str(row["value"])),
                        Decimal(str(row["current_value"])),
                    )
                )
            )
            items.append(
                {
                    "candidate_id": row["candidate_id"],
                    "source_session_ref": {
                        "object_type": "workout_session",
                        "object_id": row["session_id"],
                        "revision_id": row["session_revision_id"],
                    },
                    "source_set_ref": {
                        "object_type": "actual_set",
                        "object_id": row["actual_set_id"],
                        "revision_id": None,
                    },
                    "exercise_ref": {
                        "object_type": "exercise",
                        "object_id": row["exercise_id"],
                        "revision_id": row["exercise_revision_id"],
                    },
                    "achieved_on": row["local_start_date"],
                    "metric_kind": row["metric_kind"],
                    "candidate_value": {"exact_value": row["value"], "unit_code": row["unit_code"]},
                    "condition": condition,
                    "status": row["status"],
                    "current_record_ref": {
                        "object_type": "actual_record",
                        "object_id": row["current_record_id"],
                        "revision_id": None,
                    }
                    if row["current_record_id"]
                    else None,
                    "invalidated_reason": row["invalidated_reason"],
                    "decision_confirmation_ref": {
                        "object_type": "confirmation",
                        "object_id": row["decision_confirmation_id"],
                        "revision_id": None,
                    }
                    if row["decision_confirmation_id"]
                    else None,
                    "eligible_now": eligible,
                }
            )
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            raw = json.dumps(
                {
                    "filters": filters,
                    "created_at": last["created_at"].isoformat(),
                    "candidate_id": str(last["candidate_id"]),
                },
                sort_keys=True,
            )
            next_cursor = urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        return {"items": items, "next_cursor": next_cursor}

    def query_records(self, payload: dict[str, Any]) -> dict[str, Any]:
        from base64 import urlsafe_b64decode, urlsafe_b64encode

        filters = {
            "exercise_id": payload.get("exercise_id"),
            "metric_kind": payload.get("metric_kind"),
            "condition_id": payload.get("condition_id"),
            "view": payload["view"],
        }
        cursor_date = cursor_id = None
        if payload.get("cursor"):
            try:
                cursor = json.loads(
                    urlsafe_b64decode(payload["cursor"] + "=" * (-len(payload["cursor"]) % 4))
                )
                if cursor["filters"] != filters:
                    raise ValueError
                cursor_date = date.fromisoformat(cursor["achieved_on"])
                cursor_id = UUID(cursor["record_id"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise ContextRepositoryError(
                    "validation_error", "record cursor is invalid", 400
                ) from error
        where = ["r.profile_id=(SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL)"]
        params: dict[str, Any] = {"limit": payload["limit"] + 1}
        for key, column in (
            ("exercise_id", "exercise_id"),
            ("metric_kind", "metric_kind"),
            ("condition_id", "condition_id"),
        ):
            if filters[key] is not None:
                where.append(f"r.{column}=:{key}")
                params[key] = filters[key]
        if payload["view"] == "current":
            where.append("r.is_current")
            where.append("cc.normalized_key NOT LIKE '%historical_unknown%'")
            where.append("coalesce(dc.after_snapshot->>'validity','valid')<>'source_invalidated'")
            order = "r.exercise_id,r.metric_kind,r.condition_id,r.record_id"
            if cursor_id:
                where.append(
                    "(r.exercise_id,r.metric_kind,r.condition_id,r.record_id)>(:ce,:cm,:cc,:cr)"
                )
                cursor_bits = json.loads(
                    urlsafe_b64decode(payload["cursor"] + "=" * (-len(payload["cursor"]) % 4))
                )
                params.update(
                    ce=cursor_bits["exercise_id"],
                    cm=cursor_bits["metric_kind"],
                    cc=cursor_bits["condition_id"],
                    cr=cursor_id,
                )
        else:
            if payload["view"] == "history":
                where.append("NOT r.is_current")
            order = "r.achieved_on DESC,r.record_id DESC"
            if cursor_date is not None:
                where.append("(r.achieved_on,r.record_id)<(:cursor_date,:cursor_id)")
                params.update(cursor_date=cursor_date, cursor_id=cursor_id)
        rows = list(
            self.session.execute(
                text(f"""
            SELECT r.*,e.exercise_revision_id,cc.readable_snapshot,cc.normalized_key,
                   dc.operation,dc.after_snapshot,dc.domain_change_id
            FROM trainer.actual_records r
            JOIN trainer.exercise_revisions e ON e.exercise_id=r.exercise_id
              AND e.exercise_revision_id=r.exercise_revision_id
            JOIN trainer.comparison_conditions cc USING(condition_id)
            LEFT JOIN LATERAL (SELECT * FROM trainer.domain_changes d
              WHERE d.object_type='actual_record' AND d.object_id=r.record_id
              ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1) dc ON true
            WHERE {" AND ".join(where)} ORDER BY {order} LIMIT :limit
        """),
                params,
            ).mappings()
        )
        has_more = len(rows) > payload["limit"]
        rows = rows[: payload["limit"]]
        items = []
        for row in rows:
            snapshot = dict(row["after_snapshot"] or {})
            condition_snapshot = dict(row["readable_snapshot"])
            if "load_signature" in condition_snapshot:
                condition_snapshot["loads"] = condition_snapshot.pop("load_signature")
            condition = {
                **condition_snapshot,
                "condition_id": row["condition_id"],
                "normalized_key": row["normalized_key"],
            }
            unknown = "historical_unknown" in row["normalized_key"]
            exit_reason = snapshot.get("current_exit_reason")
            if exit_reason is None and unknown and not row["is_current"]:
                exit_reason = "historical_unknown"
            validity = snapshot.get("validity", "valid")
            items.append(
                {
                    "record_id": row["record_id"],
                    "exercise_ref": {
                        "object_type": "exercise",
                        "object_id": row["exercise_id"],
                        "revision_id": row["exercise_revision_id"],
                    },
                    "metric_kind": row["metric_kind"],
                    "value": {"exact_value": row["value"], "unit_code": row["unit_code"]},
                    "achieved_on": row["achieved_on"],
                    "condition": condition,
                    "is_current": row["is_current"],
                    "is_historical_import": row["is_historical_import"],
                    "validity": validity,
                    "source_session_ref": {
                        "object_type": "workout_session",
                        "object_id": row["session_id"],
                        "revision_id": row["session_revision_id"],
                    }
                    if row["session_id"]
                    else None,
                    "source_set_ref": {
                        "object_type": "actual_set",
                        "object_id": row["actual_set_id"],
                        "revision_id": None,
                    }
                    if row["actual_set_id"]
                    else None,
                    "source_candidate_ref": {
                        "object_type": "record_candidate",
                        "object_id": row["candidate_id"],
                        "revision_id": None,
                    }
                    if row["candidate_id"]
                    else None,
                    "confirmation_ref": {
                        "object_type": "confirmation",
                        "object_id": row["confirmation_id"],
                        "revision_id": None,
                    },
                    "supersedes_record_ref": {
                        "object_type": "actual_record",
                        "object_id": row["supersedes_record_id"],
                        "revision_id": None,
                    }
                    if row["supersedes_record_id"]
                    else None,
                    "body_mass_kg": row["body_mass"],
                    "current_exit_reason": exit_reason,
                    "current_exit_change_id": snapshot.get("current_exit_change_id"),
                    "comment": row["comment"],
                }
            )
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            raw = {
                "filters": filters,
                "achieved_on": last["achieved_on"].isoformat(),
                "record_id": str(last["record_id"]),
            }
            if payload["view"] == "current":
                raw.update(
                    exercise_id=str(last["exercise_id"]),
                    metric_kind=last["metric_kind"],
                    condition_id=str(last["condition_id"]),
                )
            next_cursor = (
                urlsafe_b64encode(json.dumps(raw, sort_keys=True).encode()).decode().rstrip("=")
            )
        return {"items": items, "next_cursor": next_cursor}
