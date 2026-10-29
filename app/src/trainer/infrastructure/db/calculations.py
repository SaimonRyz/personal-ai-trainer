"""Confirmation boundary for E-G03 calculation drafts.

Uses the established draft/preview tables and idempotency journal. The caller
owns the transaction; no helper commits a partial result.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.orm import Session

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError


class CalculationRepository(ContextRepository):
    def __init__(
        self, session: Session, loader: Callable[[Session, dict[str, Any]], dict[str, Any]]
    ) -> None:
        super().__init__(session)
        self._loader = loader

    @staticmethod
    def _render_preview(payload: dict[str, Any], *, detail_level: str = "full") -> str:
        rendered = ContextRepository._render_preview(payload, detail_level=detail_level)
        if payload.get("calculated", {}).get("metric_kind") == "e1rm":
            return "Расчётный e1RM — оценка, не фактический рекорд.\n" + rendered
        return rendered

    def create_calculation_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        if payload["mode"] == "ephemeral":
            operation, replay = self._reserve("CMD-17", key, payload["request_digest"])
            if replay is not None:
                refs = [
                    {"object_type": ref["object_type"], "object_id": ref["object_id"]}
                    for ref in replay.get("source_refs", [])
                ]
                self._audit(
                    operation,
                    "CMD-17",
                    correlation_id,
                    causation_id,
                    key,
                    refs,
                    replayed=True,
                )
                return replay
            calculated = self._loader(self.session, payload)
            response = self._ephemeral_result(payload, calculated)
            self._finish(operation, response)
            refs = [
                {"object_type": ref["object_type"], "object_id": ref["object_id"]}
                for ref in response["source_refs"]
            ]
            self._audit(operation, "CMD-17", correlation_id, causation_id, key, refs)
            return response
        if payload["mode"] != "persist":
            raise ContextRepositoryError(
                "calculation_mode_not_ready", "this calculation mode is not implemented yet", 409
            )
        operation, replay = self._reserve("CMD-17", key, payload["request_digest"])
        if replay is not None:
            self._audit_replay(operation, "CMD-17", correlation_id, causation_id, key, replay)
            return replay
        calculated = self._loader(self.session, payload)
        calculation_id, draft_id, revision_id = uuid4(), uuid4(), uuid4()
        changes = [
            {"object_type": name, "object_id": str(calculation_id), "revision_id": None}
            for name in ("derived_value", "derived_progress_point")
        ]
        prior = calculated.get("prior_calculation")
        if prior is not None:
            changes.append(
                {
                    "object_type": "derived_value",
                    "object_id": prior["derived_value_id"],
                    "revision_id": None,
                }
            )
        if calculated["best_effect"]["decision"] in ("new_current", "strict_improvement"):
            previous_best = calculated["best_effect"]["previous"]
            if previous_best is not None:
                changes.append(
                    {
                        "object_type": "calculated_best",
                        "object_id": previous_best["calculated_best_id"],
                        "revision_id": None,
                    }
                )
            changes.append(
                {"object_type": "calculated_best", "object_id": str(uuid4()), "revision_id": None}
            )
        typed = {
            "intent": "calculation",
            "calculation_id": str(calculation_id),
            "request": payload,
            "calculated": calculated,
            "profile_id": calculated["profile_id"],
            "change_plan": changes,
            "gaps": [],
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
        }
        now = datetime.now(UTC)
        digest = digest_payload(typed)
        self.session.execute(
            text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,
                latest_revision_id,expires_at,created_at)
            VALUES (:draft,'calculation',:profile,'open',:revision,:expires,:now)
        """),
            {
                "draft": draft_id,
                "profile": calculated["profile_id"],
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
        response = {
            "state": "draft",
            "calculation_id": str(calculation_id),
            "draft": {
                "draft_id": str(draft_id),
                "revision_id": str(revision_id),
                "revision_no": 1,
                "content_digest": digest,
                "dependencies": [],
                "gaps": [],
                "preview_candidate": {
                    "summary": "Расчёт e1RM подготовлен к подтверждению",
                    "confirmation_kind": "normal",
                    "complete": True,
                },
            },
        }
        self._finish(operation, response)
        refs = [
            {"object_type": str(c["object_type"]), "object_id": str(c["object_id"])}
            for c in changes
        ]
        self._audit(operation, "CMD-17", correlation_id, causation_id, key, refs)
        return response

    @staticmethod
    def _ephemeral_result(payload: dict[str, Any], calculated: dict[str, Any]) -> dict[str, Any]:
        source_refs: dict[str, dict[str, Any]] = {}
        for item in calculated.get("normalized_inputs", []):
            ref = item.get("source_ref")
            if ref is not None:
                source_refs[json.dumps(ref, sort_keys=True)] = ref
        return {
            "calculation_id": str(uuid5(NAMESPACE_URL, payload["request_digest"])),
            "method_id": calculated["method_id"],
            "method_version": calculated["method_version"],
            "metric_kind": calculated["metric_kind"],
            "value": calculated["value"],
            "completeness": calculated["completeness"],
            "state": "ephemeral",
            "input_digest": calculated["input_digest"],
            "result_digest": calculated["result_digest"],
            "source_refs": list(source_refs.values()),
            "applicability_version": calculated["applicability_version"],
            "included_count": calculated["included_count"],
            "excluded_count": calculated["excluded_count"],
            "exclusions": calculated.get("exclusions", []),
            "execution_attestation": calculated.get("attestation"),
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
        operation, replay = self._reserve("CMD-00", key, payload["request_digest"])
        if replay is not None:
            if replay.get("_confirmed_preview_id") != str(preview_id):
                raise ContextRepositoryError("idempotency_conflict", "different preview", 409)
            self._audit_replay(operation, "CMD-00", correlation_id, causation_id, key, replay)
            replay.pop("_confirmed_preview_id", None)
            replay["status"] = "already_committed"
            return replay
        row = (
            self.session.execute(
                text("""
            SELECT p.*,r.typed_payload,r.trust_state,r.confirmation_kind,d.draft_id,
                   d.state,d.expires_at,d.latest_revision_id,d.intent_type
            FROM trainer.previews p JOIN trainer.draft_revisions r USING(draft_revision_id)
            JOIN trainer.drafts d USING(draft_id) WHERE p.preview_id=:preview FOR UPDATE
        """),
                {"preview": preview_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["intent_type"] != "calculation":
            raise ContextRepositoryError("not_found", "calculation preview not found", 404)
        if (
            str(row["draft_revision_id"]) != str(payload["revision_id"])
            or row["content_digest"] != payload["preview_digest"]
            or row["latest_revision_id"] != row["draft_revision_id"]
        ):
            raise ContextRepositoryError("stale_preview", "calculation preview changed", 409)
        if row["confirmation_kind"] != payload["confirmation_kind"]:
            raise ContextRepositoryError("ambiguous_confirmation", "confirmation kind differs", 400)
        if (
            not row["is_complete"]
            or row["shown_message_id"] is None
            or row["trust_state"] != "shown"
        ):
            raise ContextRepositoryError("incomplete_preview", "show the full preview first", 400)
        now = datetime.now(UTC)
        if row["state"] != "open" or row["expires_at"] <= now:
            raise ContextRepositoryError("already_superseded", "draft is no longer open", 409)
        shown = (
            self.session.execute(
                text("""
            SELECT conversation_id,shown_message_id,cross_dialog FROM trainer.preview_show_events
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
        same = payload["confirmation_mode"] == "same_dialog"
        valid = shown is not None and (
            (
                same
                and not shown["cross_dialog"]
                and row["conversation_id"] == payload["context"]["conversation_id"]
                and payload.get("referenced_preview_id") is None
                and payload.get("reshown_message_id") is None
            )
            or (
                not same
                and shown["cross_dialog"]
                and str(payload.get("referenced_preview_id")) == str(preview_id)
                and payload.get("reshown_message_id") == shown["shown_message_id"]
            )
        )
        if not valid or payload["confirmation_message_id"] != payload["context"]["message_id"]:
            raise ContextRepositoryError(
                "ambiguous_confirmation", "preview was not shown here", 400
            )
        typed = dict(row["typed_payload"])
        self.session.execute(
            text("SELECT profile_id FROM trainer.profiles WHERE profile_id=:id FOR UPDATE"),
            {"id": typed["profile_id"]},
        ).scalar_one()
        try:
            fresh = self._loader(self.session, typed["request"])
        except ContextRepositoryError as exc:
            if (
                exc.code == "invalid_dependency"
                and typed.get("calculated", {}).get("prior_calculation") is not None
            ):
                raise ContextRepositoryError(
                    "stale_dependency", "selected prior calculation changed after preview", 409
                ) from exc
            raise
        if digest_payload(fresh) != digest_payload(typed["calculated"]):
            raise ContextRepositoryError(
                "stale_dependency", "calculation or best effect changed", 409
            )
        confirmation, result = uuid4(), uuid4()
        changes = typed["change_plan"]
        response = {
            "commit_result_id": str(result),
            "confirmation_id": str(confirmation),
            "operation_id": str(operation),
            "status": "committed",
            "domain_changes": changes,
            "created_revisions": [],
            "invalidated_calculations": [],
            "committed_at": now.isoformat(),
        }
        self.session.execute(
            text("""
            INSERT INTO trainer.confirmations(confirmation_id,preview_id,draft_revision_id,
                conversation_id,user_message_id,kind,explicit_expression,confirmed_at,created_at)
            VALUES (:id,:preview,:revision,:conversation,:message,'normal',:expression,:now,:now)
        """),
            {
                "id": confirmation,
                "preview": preview_id,
                "revision": row["draft_revision_id"],
                "conversation": payload["context"]["conversation_id"],
                "message": payload["confirmation_message_id"],
                "expression": payload["explicit_intent"],
                "now": now,
            },
        )
        self.session.execute(
            text("""
            INSERT INTO trainer.commit_results(commit_result_id,confirmation_id,operation_id,
                result_payload,committed_at,created_at)
            VALUES (:id,:confirmation,:operation,CAST(:payload AS jsonb),:now,:now)
        """),
            {
                "id": result,
                "confirmation": confirmation,
                "operation": operation,
                "payload": json.dumps(response),
                "now": now,
            },
        )
        self._persist(typed, confirmation, result, now)
        if fail_before_commit:
            raise RuntimeError("synthetic fault before transaction commit")
        self.session.execute(
            text("UPDATE trainer.drafts SET state='confirmed' WHERE draft_id=:id"),
            {"id": row["draft_id"]},
        )
        self.session.execute(
            text("""
                UPDATE trainer.idempotent_operations
                SET state='committed',commit_result_id=:result,
                    response_payload=CAST(:payload AS jsonb),updated_at=:now
                WHERE operation_id=:operation
            """),
            {
                "result": result,
                "payload": json.dumps({**response, "_confirmed_preview_id": str(preview_id)}),
                "now": now,
                "operation": operation,
            },
        )
        self._audit(operation, "CMD-00", correlation_id, causation_id, key, changes)
        return response

    def _persist(
        self, typed: dict[str, Any], confirmation: UUID, result: UUID, now: datetime
    ) -> None:
        calc, source = typed["calculated"], typed["request"]["source"]
        condition = calc["condition"]
        condition_id = condition["existing_condition_id"] or str(uuid4())
        self.session.execute(
            text("""
            INSERT INTO trainer.comparison_conditions(condition_id,exercise_id,rule_version,
                normalized_key,readable_snapshot,created_at)
            VALUES (:id,:exercise,:rule,:key,CAST(:snapshot AS jsonb),:now)
            ON CONFLICT(exercise_id,rule_version,normalized_key) DO NOTHING
        """),
            {
                "id": condition_id,
                "exercise": source["exercise_id"],
                "rule": condition["rule_version"],
                "key": condition["normalized_key"],
                "snapshot": json.dumps(condition["readable_snapshot"]),
                "now": now,
            },
        )
        condition_id = self.session.execute(
            text("""
            SELECT condition_id FROM trainer.comparison_conditions
            WHERE exercise_id=:exercise AND rule_version=:rule AND normalized_key=:key
        """),
            {
                "exercise": source["exercise_id"],
                "rule": condition["rule_version"],
                "key": condition["normalized_key"],
            },
        ).scalar_one()
        existing = self.session.execute(
            text("""
            SELECT derived_value_id FROM trainer.derived_progress_points
            WHERE method_id='e1rm_brzycki' AND method_version='1'
              AND source_actual_set_id=:source AND condition_id=:condition
        """),
            {"source": source["actual_set_id"], "condition": condition_id},
        ).scalar_one_or_none()
        if existing:
            raise ContextRepositoryError(
                "already_calculated", "source and condition already saved", 409
            )
        derived = typed["calculation_id"]
        prior = calc.get("prior_calculation")
        assumptions = {**calc["calculation_assumptions"], "metric_kind": "estimated_1rm"}
        self.session.execute(
            text("""
            INSERT INTO trainer.derived_values(derived_value_id,profile_id,method_id,method_version,
                result_value,unit_code,calculated_at,assumptions,state,supersedes_derived_value_id,
                persistence_reason,created_at)
            VALUES (:id,:profile,'e1rm_brzycki','1',:value,'kilogram',:now,
                    CAST(:assumptions AS jsonb),'current',:prior,'progress',:now)
        """),
            {
                "id": derived,
                "profile": calc["profile_id"],
                "value": calc["value"]["exact_value"],
                "now": now,
                "assumptions": json.dumps(assumptions),
                "prior": prior["derived_value_id"] if prior else None,
            },
        )
        for item in calc["normalized_inputs"]:
            ref = item["source_ref"]
            self.session.execute(
                text("""
                INSERT INTO trainer.derived_value_inputs(derived_value_id,source_kind,
                    source_object_type,source_object_id,source_revision_id,input_role,source_value,
                    source_unit,snapshot,created_at)
                VALUES (:id,:kind,:type,:object,:revision,:role,:value,:unit,
                        CAST(:snapshot AS jsonb),:now)
            """),
                {
                    "id": derived,
                    "kind": "user" if item["role"] == "execution_attestation" else "system",
                    "type": ref["object_type"],
                    "object": ref["object_id"],
                    "revision": ref["revision_id"],
                    "role": item["role"],
                    "value": item["normalized_value"],
                    "unit": item["unit_code"],
                    "snapshot": json.dumps(item["snapshot"]),
                    "now": now,
                },
            )
        self.session.execute(
            text("""
            INSERT INTO trainer.derived_progress_points(derived_value_id,method_id,method_version,
                metric_kind,exercise_id,exercise_revision_id,condition_id,session_id,
                session_revision_id,source_actual_set_id,completeness,included_count,excluded_count,
                input_digest,result_digest,created_at)
            VALUES (:id,'e1rm_brzycki','1','estimated_1rm',:exercise,:exercise_revision,:condition,
                    :session,:session_revision,:source,'complete',1,0,:input,:result,:now)
        """),
            {
                "id": derived,
                "exercise": source["exercise_id"],
                "exercise_revision": source["exercise_revision_id"],
                "condition": condition_id,
                "session": source["session_id"],
                "session_revision": source["session_revision_id"],
                "source": source["actual_set_id"],
                "input": calc["input_digest"],
                "result": calc["result_digest"],
                "now": now,
            },
        )
        previous_best = calc["best_effect"]["previous"]
        best_change = next(
            (
                change
                for change in typed["change_plan"]
                if change["object_type"] == "calculated_best"
                and (
                    previous_best is None
                    or change["object_id"] != previous_best["calculated_best_id"]
                )
            ),
            None,
        )
        if best_change and previous_best is not None:
            previous_change = next(
                change
                for change in typed["change_plan"]
                if change["object_type"] == "calculated_best"
                and change["object_id"] == previous_best["calculated_best_id"]
            )
            before_row = (
                self.session.execute(
                    text("""
                        SELECT calculated_best_id,profile_id,exercise_id,metric_kind,value,
                               unit_code,condition_id,derived_value_id,achieved_on,
                               source_session_id,source_session_revision_id,source_actual_set_id,
                               is_current,supersedes_best_id
                        FROM trainer.calculated_bests
                        WHERE calculated_best_id=:id AND is_current
                        FOR UPDATE
                    """),
                    {"id": previous_change["object_id"]},
                )
                .mappings()
                .one_or_none()
            )
            if before_row is None:
                raise ContextRepositoryError(
                    "stale_dependency", "previous calculated best changed", 409
                )
            before = dict(before_row)
            updated = self.session.execute(
                text("""
                    UPDATE trainer.calculated_bests SET is_current=false
                    WHERE calculated_best_id=:id AND is_current
                    RETURNING calculated_best_id
                """),
                {"id": previous_change["object_id"]},
            ).scalar_one_or_none()
            if updated is None:
                raise ContextRepositoryError(
                    "stale_dependency", "previous calculated best changed", 409
                )
            self._insert_change(
                previous_change,
                result,
                "correct",
                before,
                {**before, "is_current": False},
                "A strictly better confirmed e1RM became current in the same condition.",
                now,
            )
        if best_change:
            previous = previous_best
            self.session.execute(
                text("""
                INSERT INTO trainer.calculated_bests(calculated_best_id,profile_id,exercise_id,
                    metric_kind,value,unit_code,condition_id,derived_value_id,achieved_on,
                    source_session_id,source_session_revision_id,source_actual_set_id,is_current,
                    supersedes_best_id,confirmation_id,created_at)
                VALUES (:id,:profile,:exercise,'estimated_1rm',:value,'kilogram',:condition,
                        :derived,:date,:session,:revision,:source,true,:previous,:confirmation,:now)
            """),
                {
                    "id": best_change["object_id"],
                    "profile": calc["profile_id"],
                    "exercise": source["exercise_id"],
                    "value": calc["value"]["exact_value"],
                    "condition": condition_id,
                    "derived": derived,
                    "date": calc["source"]["local_start_date"],
                    "session": source["session_id"],
                    "revision": source["session_revision_id"],
                    "source": source["actual_set_id"],
                    "previous": previous["calculated_best_id"] if previous else None,
                    "confirmation": confirmation,
                    "now": now,
                },
            )
        for change in typed["change_plan"]:
            if (
                previous_best is not None
                and change["object_type"] == "calculated_best"
                and change["object_id"] == previous_best["calculated_best_id"]
            ):
                continue
            elif prior is not None and change["object_id"] == prior["derived_value_id"]:
                updated = self.session.execute(
                    text("""
                        UPDATE trainer.derived_values SET state='replaced',stale_reason=NULL
                        WHERE derived_value_id=:id AND state='stale'
                          AND supersedes_derived_value_id IS NULL
                        RETURNING derived_value_id
                    """),
                    {"id": prior["derived_value_id"]},
                ).scalar_one_or_none()
                if updated is None:
                    raise ContextRepositoryError(
                        "stale_dependency", "selected prior calculation changed", 409
                    )
                self._insert_change(
                    change,
                    result,
                    "correct",
                    {"state": "stale", "derived_value_id": prior["derived_value_id"]},
                    {"state": "replaced", "superseded_by": str(derived)},
                    None,
                    now,
                )
            else:
                self._insert_change(change, result, "create", None, calc, None, now)
