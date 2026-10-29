"""Read-time E-G03 query implementation; external volume is never persisted."""

from __future__ import annotations

import json
import unicodedata
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from trainer.domain.context import digest_payload
from trainer.domain.progress import (
    E1RMAttestation,
    E1RMInput,
    VolumeSet,
    calculate_brzycki_e1rm,
    calculate_external_working_volume,
    classify_calculated_best,
)
from trainer.domain.records import canonical_condition_key
from trainer.infrastructure.db.calculations import CalculationRepository
from trainer.infrastructure.db.context_repository import (
    ContextDatabaseExecutor,
    ContextRepositoryError,
)


class ProgressQueryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _ref(object_type: str, object_id: Any, revision_id: Any = None) -> dict[str, str | None]:
        return {
            "object_type": object_type,
            "object_id": str(object_id),
            "revision_id": str(revision_id) if revision_id is not None else None,
        }

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        filters = {
            key: payload.get(key)
            for key in (
                "metric_kind",
                "date_from",
                "date_to",
                "exercise_id",
                "condition_id",
                "include_history",
                "limit",
            )
        }
        filter_digest = digest_payload(filters)
        after = self._decode_cursor(payload.get("cursor"), filter_digest)
        profile = (
            self.session.execute(
                text("""
                    SELECT profile_id FROM trainer.profiles
                    WHERE archived_at IS NULL
                    ORDER BY created_at LIMIT 2
                """)
            )
            .scalars()
            .all()
        )
        if len(profile) != 1:
            raise RuntimeError("progress query requires exactly one active profile")
        profile_id = profile[0]
        metric = payload["metric_kind"]
        start, end = (
            date.fromisoformat(payload["date_from"]),
            date.fromisoformat(payload["date_to"]),
        )
        items: list[dict[str, Any]]
        if metric == "external_working_set_volume":
            items = self._volume_items(profile_id, start, end, payload.get("exercise_id"))
        elif metric == "estimated_1rm":
            items = self._e1rm_items(profile_id, start, end, payload)
        else:
            items = self._actual_record_items(profile_id, start, end, payload)
        presented = [self._present(item) for item in items]
        presented.sort(key=lambda item: item["sort_key"])
        eligible_actual_ids = frozenset(
            str(item["record_ref"]["object_id"])
            for item in items
            if item["category"] == "actual_record"
            and item.get("comparison_eligible", item["lifecycle"] == "current")
        )
        comparison_status = self._comparison_status(presented, eligible_actual_ids)
        remaining = [item for item in presented if after is None or item["sort_key"] > after]
        page = remaining[: payload["limit"]]
        has_more = len(remaining) > len(page)
        next_cursor = (
            self._encode_cursor(filter_digest, page[-1]["sort_key"]) if has_more and page else None
        )
        for item in page:
            item.pop("sort_key")
        return {
            "items": page,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "gaps": [
                {
                    "path": f"items[{index}].point.value",
                    "code": "missing",
                    "disposition": "leave_unknown",
                    "message_safe": "Для этого показателя недостаточно полных исходных данных.",
                }
                for index, item in enumerate(page)
                if item["kind"] == "derived" and item["point"]["completeness"] == "insufficient"
            ],
            "comparison_status": comparison_status,
        }

    @staticmethod
    def _present(item: dict[str, Any]) -> dict[str, Any]:
        category = item["category"]
        kind = "actual" if category == "actual_record" else "derived"
        exercise_ref = item["exercise_ref"]
        session_ref = item.get("source_session_ref")
        set_ref = item.get("source_set_ref")
        inputs = json.loads(json.dumps(item.get("inputs", []), default=str))
        if kind == "actual":
            point = {
                "record_ref": item["record_ref"],
                "exercise_ref": exercise_ref,
                "metric_kind": item["metric_kind"],
                "value": item["value"],
                "achieved_on": item["result_date"],
                "condition_id": item["condition_id"],
                "session_ref": session_ref,
                "actual_set_ref": set_ref,
                "is_current": item["lifecycle"] == "current",
                "is_historical": item["is_historical"],
            }
        else:
            session_revision_ref = (
                {
                    **session_ref,
                    "object_type": "workout_session_revision",
                    "object_id": session_ref["revision_id"],
                    "revision_id": None,
                }
                if session_ref and session_ref.get("revision_id")
                else None
            )
            point = {
                "derived_value_ref": item.get("source_derived_value_ref"),
                "method_id": item["method_id"],
                "method_version": item["method_version"],
                "metric_kind": item["metric_kind"],
                "value": item["value"],
                "achieved_on": item["result_date"],
                "exercise_ref": exercise_ref,
                "exercise_revision_ref": item.get("exercise_revision_ref"),
                "session_ref": session_ref,
                "session_revision_ref": session_revision_ref,
                "source_actual_set_ref": set_ref,
                "condition_id": item.get("condition_id"),
                "state": "ephemeral" if category == "external_volume" else item["lifecycle"],
                "completeness": item["completeness"],
                "included_count": item["included_count"],
                "excluded_count": item["excluded_count"],
                "inputs": inputs,
                "exclusions": item.get("excluded_sources", []),
                "input_digest": item.get("input_digest"),
                "result_digest": item.get("result_digest"),
                "stale_reason": item.get("stale_reason"),
            }
        if kind == "actual":
            identity = {"kind": kind, "record_ref": point["record_ref"]}
        elif point["derived_value_ref"] is not None:
            identity = {"kind": kind, "derived_value_ref": point["derived_value_ref"]}
        else:
            identity = {
                "kind": kind,
                "method_id": point["method_id"],
                "method_version": point["method_version"],
                "session_ref": point["session_ref"],
                "exercise_ref": point["exercise_ref"],
                "inputs": point["inputs"],
            }
        key = digest_payload(identity)
        session_id = session_ref["object_id"] if session_ref else ""
        revision_id = session_ref["revision_id"] if session_ref else ""
        sort_key = [str(point["achieved_on"]), str(session_id), str(revision_id), kind, key]
        return {"kind": kind, "point_key": key, "point": point, "sort_key": sort_key}

    @staticmethod
    def _comparison_status(
        items: list[dict[str, Any]], eligible_actual_ids: frozenset[str] | None = None
    ) -> str:
        if not items:
            return "no_points"
        comparable_items = [
            item
            for item in items
            if (
                item["kind"] == "actual"
                and (
                    item["point"]["is_current"]
                    if eligible_actual_ids is None
                    else str(item["point"]["record_ref"]["object_id"]) in eligible_actual_ids
                )
                or item["kind"] == "derived"
                and item["point"]["state"] in ("current", "ephemeral")
            )
        ]
        if not comparable_items:
            return "insufficient"
        group_keys = {
            (
                str(item["point"]["exercise_ref"]["object_id"]),
                str(item["point"].get("condition_id")),
                str(item["point"].get("method_id") or "actual"),
                str(item["point"].get("method_version") or "actual"),
            )
            for item in comparable_items
        }
        if len(group_keys) > 1:
            return "not_comparable"
        complete_dates = {
            str(item["point"]["achieved_on"])
            for item in comparable_items
            if item["kind"] == "actual" or item["point"]["completeness"] == "complete"
        }
        if len(complete_dates) >= 3:
            return "descriptive_direction"
        if len(complete_dates) == 2:
            return "two_points"
        if len(complete_dates) == 1:
            return "one_point"
        return "insufficient"

    @staticmethod
    def _actual_record_comparison_eligible(
        condition: dict[str, Any] | None, state: dict[str, Any] | None
    ) -> bool:
        """A superseded best remains valid; a corrected/unknown fact does not."""
        return bool(
            condition is not None
            and condition.get("scope") == "comparable"
            and condition.get("unknown_fields") == []
            and state is not None
            and state.get("validity", "valid") == "valid"
            and state.get("current_exit_reason") in (None, "better_record")
        )

    def _actual_record_items(
        self, profile_id: UUID, start: date, end: date, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text("""
                SELECT v.*,cc.readable_snapshot AS comparison_snapshot,
                       dc.after_snapshot AS record_state
                FROM trainer.v_best_results v
                JOIN trainer.comparison_conditions cc USING(condition_id)
                LEFT JOIN LATERAL (
                    SELECT d.after_snapshot FROM trainer.domain_changes d
                    WHERE d.object_type='actual_record'
                      AND d.object_id=v.source_actual_record_id
                    ORDER BY d.occurred_at DESC,d.domain_change_id DESC LIMIT 1
                ) dc ON true
                WHERE v.profile_id=:profile AND v.metric_kind=:metric
                  AND v.result_date BETWEEN :start AND :end
                  AND (CAST(:exercise AS uuid) IS NULL OR v.exercise_id=CAST(:exercise AS uuid))
                  AND (CAST(:condition AS uuid) IS NULL OR v.condition_id=CAST(:condition AS uuid))
                  AND (:history OR v.is_current)
                ORDER BY v.result_date,v.exercise_id,v.category,v.session_id,v.actual_set_id
            """),
            {
                "profile": profile_id,
                "metric": payload["metric_kind"],
                "start": start,
                "end": end,
                "exercise": payload.get("exercise_id"),
                "condition": payload.get("condition_id"),
                "history": payload["include_history"],
            },
        ).mappings()
        return [
            {
                "category": row["category"],
                "record_ref": self._ref("actual_record", row["source_actual_record_id"])
                if row["source_actual_record_id"]
                else None,
                "is_historical": bool(row["is_historical"]),
                "comparison_eligible": self._actual_record_comparison_eligible(
                    row["comparison_snapshot"], row["record_state"]
                ),
                "metric_kind": row["metric_kind"],
                "value": {"exact_value": row["value"], "unit_code": row["unit_code"]},
                "result_date": row["result_date"],
                "exercise_ref": self._ref("exercise", row["exercise_id"]),
                "condition_id": row["condition_id"],
                "source_session_ref": self._ref(
                    "workout_session", row["session_id"], row["session_revision_id"]
                )
                if row["session_id"]
                else None,
                "source_set_ref": self._ref(
                    "actual_set", row["actual_set_id"], row["session_revision_id"]
                )
                if row["actual_set_id"]
                else None,
                "source_derived_value_ref": self._ref(
                    "derived_value", row["source_derived_value_id"]
                )
                if row["source_derived_value_id"]
                else None,
                "method_id": None,
                "method_version": None,
                "completeness": "complete",
                "lifecycle": "current" if row["is_current"] else "historical",
                "included_count": 1,
                "excluded_count": 0,
                "included_source_refs": [],
                "excluded_sources": [],
            }
            for row in rows
        ]

    def _e1rm_items(
        self, profile_id: UUID, start: date, end: date, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text("""
                SELECT p.*,d.result_value,d.state,d.stale_reason,d.method_id,d.method_version,
                       cc.readable_snapshot,r.local_start_date
                FROM trainer.derived_progress_points p
                JOIN trainer.derived_values d USING(derived_value_id)
                JOIN trainer.comparison_conditions cc USING(condition_id)
                JOIN trainer.workout_sessions s ON s.session_id=p.session_id
                JOIN trainer.workout_session_revisions r
                  ON r.session_id=p.session_id AND r.session_revision_id=p.session_revision_id
                WHERE d.profile_id=:profile AND p.metric_kind='estimated_1rm'
                  AND r.local_start_date BETWEEN :start AND :end
                  AND (CAST(:exercise AS uuid) IS NULL OR p.exercise_id=CAST(:exercise AS uuid))
                  AND (CAST(:condition AS uuid) IS NULL OR p.condition_id=CAST(:condition AS uuid))
                  AND (:history OR d.state='current')
                ORDER BY r.local_start_date,p.exercise_id,p.condition_id,
                         p.created_at,p.derived_value_id
            """),
            {
                "profile": profile_id,
                "start": start,
                "end": end,
                "exercise": payload.get("exercise_id"),
                "condition": payload.get("condition_id"),
                "history": payload["include_history"],
            },
        ).mappings()
        items = []
        for row in rows:
            state = str(row["state"])
            items.append(
                {
                    "category": "calculated_point",
                    "metric_kind": "estimated_1rm",
                    "value": {"exact_value": row["result_value"], "unit_code": "kilogram"},
                    "result_date": row["local_start_date"],
                    "exercise_ref": self._ref(
                        "exercise", row["exercise_id"], row["exercise_revision_id"]
                    ),
                    "condition_id": row["condition_id"],
                    "source_session_ref": self._ref(
                        "workout_session", row["session_id"], row["session_revision_id"]
                    ),
                    "source_set_ref": self._ref(
                        "actual_set", row["source_actual_set_id"], row["session_revision_id"]
                    ),
                    "source_derived_value_ref": self._ref("derived_value", row["derived_value_id"]),
                    "method_id": row["method_id"],
                    "method_version": row["method_version"],
                    "completeness": row["completeness"],
                    "lifecycle": state,
                    "stale_reason": row["stale_reason"],
                    "exercise_revision_ref": self._ref(
                        "exercise_revision", row["exercise_revision_id"]
                    ),
                    "included_count": row["included_count"],
                    "excluded_count": row["excluded_count"],
                    "included_source_refs": [],
                    "excluded_sources": [],
                    "inputs": [
                        {
                            "role": input_row["input_role"],
                            "source_ref": self._ref(
                                input_row["source_object_type"],
                                input_row["source_object_id"],
                                input_row["source_revision_id"],
                            ),
                            "normalized_value": input_row["source_value"],
                            "unit_code": input_row["source_unit"],
                            "snapshot": input_row["snapshot"],
                        }
                        for input_row in self.session.execute(
                            text("""
                            SELECT input_role,source_object_type,source_object_id,
                                   source_revision_id,
                                   source_value,source_unit,snapshot
                            FROM trainer.derived_value_inputs WHERE derived_value_id=:id
                            ORDER BY input_role,source_object_type,source_object_id,
                                     source_revision_id
                        """),
                            {"id": row["derived_value_id"]},
                        ).mappings()
                    ],
                    "input_digest": row["input_digest"],
                    "result_digest": row["result_digest"],
                }
            )
        return items

    def _volume_items(
        self, profile_id: UUID, start: date, end: date, exercise_filter: UUID | None
    ) -> list[dict[str, Any]]:
        sessions = self.session.execute(
            text("""
                SELECT s.session_id,s.current_revision_id,r.local_start_date
                FROM trainer.workout_sessions s
                JOIN trainer.workout_session_revisions r
                  ON r.session_id=s.session_id AND r.session_revision_id=s.current_revision_id
                WHERE s.profile_id=:profile AND s.archived_at IS NULL
                  AND s.logically_deleted_at IS NULL
                  AND r.local_start_date BETWEEN :start AND :end
                ORDER BY r.local_start_date,s.session_id
            """),
            {"profile": profile_id, "start": start, "end": end},
        ).mappings()
        results: list[dict[str, Any]] = []
        for session in sessions:
            rows = self.session.execute(
                text("""
                    SELECT e.execution_id,e.exercise_id,e.exercise_revision_id,x.unilateral,
                           aset.actual_set_id,aset.logical_set_no,aset.round_no,aset.side,
                           aset.set_type,aset.status,aset.full_reps,aset.partial_reps,
                           l.actual_load_component_id,l.load_kind,l.mass,l.unit_code,l.mass_meaning,
                           l.item_count,l.raw_ambiguous_text
                    FROM trainer.actual_blocks b
                    JOIN trainer.exercise_executions e USING(actual_block_id)
                    JOIN trainer.exercise_revisions x
                      ON x.exercise_id=e.exercise_id
                     AND x.exercise_revision_id=e.exercise_revision_id
                    LEFT JOIN trainer.actual_sets aset USING(execution_id)
                    LEFT JOIN trainer.actual_load_components l USING(actual_set_id)
                    WHERE b.session_revision_id=:revision
                      AND (CAST(:exercise AS uuid) IS NULL OR e.exercise_id=CAST(:exercise AS uuid))
                    ORDER BY e.exercise_id,e.position,aset.position,l.position
                """),
                {"revision": session["current_revision_id"], "exercise": exercise_filter},
            ).mappings()
            grouped_exercises: dict[UUID, dict[str, Any]] = {}
            for row in rows:
                exercise = grouped_exercises.setdefault(
                    row["exercise_id"],
                    {
                        "exercise_revision_id": row["exercise_revision_id"],
                        "sets": {},
                    },
                )
                if row["actual_set_id"] is None:
                    continue
                entry = exercise["sets"].setdefault(
                    str(row["actual_set_id"]),
                    {
                        "execution_id": row["execution_id"],
                        "exercise_id": row["exercise_id"],
                        "exercise_revision_id": row["exercise_revision_id"],
                        "actual_set_id": row["actual_set_id"],
                        "session_revision_id": session["current_revision_id"],
                        "unilateral": bool(row["unilateral"]),
                        "logical_set_no": row["logical_set_no"],
                        "round_no": row["round_no"],
                        "side": row["side"],
                        "set_type": row["set_type"],
                        "status": row["status"],
                        "full_reps": row["full_reps"],
                        "partial_reps": row["partial_reps"],
                        "loads": [],
                    },
                )
                if row["load_kind"] is not None:
                    entry["loads"].append(dict(row))
            for exercise_id, exercise_data in grouped_exercises.items():
                exercise_sets = list(exercise_data["sets"].values())
                inputs: list[VolumeSet] = []
                expected_sides: dict[tuple[UUID, int, int | None], set[str]] = {}
                for item in exercise_sets:
                    if item["unilateral"] and item["set_type"] == "working":
                        key = (item["execution_id"], int(item["logical_set_no"]), item["round_no"])
                        if item["side"] in ("left", "right"):
                            expected_sides.setdefault(key, set()).add(item["side"])
                for item in exercise_sets:
                    external = [load for load in item["loads"] if load["load_kind"] == "external"]
                    non_external = [
                        load
                        for load in item["loads"]
                        if load["load_kind"] in ("bodyweight", "band")
                    ]
                    exclusion_kind = "unknown_load"
                    valid = bool(external)
                    for load in external:
                        if load["raw_ambiguous_text"] is not None or load["mass_meaning"] not in (
                            "total_external_mass",
                            "per_item_mass",
                        ):
                            valid = False
                            exclusion_kind = "ambiguous_load"
                            break
                        if load["mass"] is None or load["unit_code"] not in ("kilogram", "pound"):
                            valid = False
                            exclusion_kind = "unknown_load"
                            break
                        if load["mass_meaning"] == "per_item_mass" and load["item_count"] is None:
                            valid = False
                            exclusion_kind = "unknown_item_count"
                            break
                    if not external and non_external:
                        exclusion_kind = "non_external_load"
                    elif not external and item["loads"]:
                        exclusion_kind = "unknown_load"
                    mass = Decimal(0)
                    if valid:
                        for load in external:
                            component = Decimal(str(load["mass"]))
                            if load["unit_code"] == "pound":
                                component *= Decimal("0.45359237")
                            if load["mass_meaning"] == "per_item_mass":
                                component *= Decimal(str(load["item_count"]))
                            mass += component
                    if item["unilateral"] and item["set_type"] == "working":
                        key = (item["execution_id"], int(item["logical_set_no"]), item["round_no"])
                        expected: tuple[str, ...] = ("left", "right")
                        actual_sides = expected_sides.get(key, set())
                        performed = tuple(sorted(actual_sides))
                    else:
                        expected = ()
                        performed = ()
                    inputs.append(
                        VolumeSet(
                            actual_set_id=item["actual_set_id"],
                            set_type=item["set_type"],
                            status=item["status"],
                            mass_kg=mass if valid else None,
                            full_reps=item["full_reps"],
                            load_kind="external" if valid else exclusion_kind,
                            partial_reps=item["partial_reps"],
                            performed_sides=performed,
                            expected_sides=expected,
                        )
                    )
                result = calculate_external_working_volume(tuple(inputs))
                volume_inputs: list[dict[str, Any]] = [
                    {
                        "role": "session_revision",
                        "source_ref": self._ref(
                            "workout_session_revision", session["current_revision_id"]
                        ),
                        "normalized_value": None,
                        "unit_code": None,
                        "snapshot": {"local_start_date": session["local_start_date"].isoformat()},
                    }
                ]
                for item in exercise_sets:
                    set_ref = self._ref(
                        "actual_set", item["actual_set_id"], item["session_revision_id"]
                    )
                    volume_inputs.append(
                        {
                            "role": "actual_set",
                            "source_ref": set_ref,
                            "normalized_value": str(item["full_reps"])
                            if item["full_reps"] is not None
                            else None,
                            "unit_code": "repetition" if item["full_reps"] is not None else None,
                            "snapshot": {
                                "set_type": item["set_type"],
                                "status": item["status"],
                                "partial_reps": item["partial_reps"],
                                "side": item["side"],
                                "logical_set_no": item["logical_set_no"],
                                "round_no": item["round_no"],
                            },
                        }
                    )
                    for load in item["loads"]:
                        volume_inputs.append(
                            {
                                "role": "load_component",
                                "source_ref": self._ref(
                                    "actual_load_component",
                                    load["actual_load_component_id"],
                                    item["session_revision_id"],
                                ),
                                "normalized_value": str(load["mass"])
                                if load["mass"] is not None
                                else None,
                                "unit_code": load["unit_code"],
                                "snapshot": json.loads(json.dumps(dict(load), default=str)),
                            }
                        )
                results.append(
                    {
                        "category": "external_volume",
                        "metric_kind": "external_working_set_volume",
                        "value": {
                            "exact_value": result.value_kg_reps,
                            "unit_code": "kilogram_repetition",
                        }
                        if result.value_kg_reps is not None
                        else None,
                        "result_date": session["local_start_date"],
                        "exercise_ref": self._ref("exercise", exercise_id),
                        "exercise_revision_ref": self._ref(
                            "exercise_revision", exercise_data["exercise_revision_id"]
                        ),
                        "condition_id": None,
                        "source_session_ref": self._ref(
                            "workout_session", session["session_id"], session["current_revision_id"]
                        ),
                        "source_set_ref": None,
                        "source_derived_value_ref": None,
                        "method_id": "external_working_set_volume",
                        "method_version": "1",
                        "completeness": result.status,
                        "lifecycle": "current",
                        "inputs": volume_inputs,
                        "stale_reason": None,
                        "included_count": len(result.included_set_ids),
                        "excluded_count": len(result.excluded),
                        "included_source_refs": [
                            self._ref("actual_set", set_id, session["current_revision_id"])
                            for set_id in result.included_set_ids
                        ],
                        "excluded_sources": [
                            {
                                "source_ref": self._ref(
                                    "actual_set", set_id, session["current_revision_id"]
                                ),
                                "reason_code": {
                                    "warmup": "warmup",
                                    "skipped": "skipped_set",
                                    "interrupted_set": "interrupted_set",
                                    "partial_repetitions": "partial_repetitions",
                                    "unknown_load": "unknown_load",
                                    "non_external_load": "non_external_load",
                                    "ambiguous_load": "ambiguous_load",
                                    "unknown_item_count": "unknown_item_count",
                                    "unknown_repetitions": "unknown_repetitions",
                                    "invalid_load_shape": "unknown_item_count",
                                    "missing_or_unknown_side": "unknown_side",
                                    "not_working": "unsupported_exercise",
                                }.get(reason, "no_applicable_external_volume"),
                                "label": {
                                    "warmup": "Разминка не входит в рабочий объём",
                                    "skipped": "Подход пропущен",
                                    "interrupted_set": (
                                        "Подход прерван; учтены известные полные повторы"
                                    ),
                                    "partial_repetitions": (
                                        "Частичные повторы не включены; полные учтены"
                                    ),
                                    "unsupported_or_unknown_load": (
                                        "Нагрузка неизвестна или неприменима"
                                    ),
                                    "unknown_repetitions": "Число полных повторов неизвестно",
                                    "invalid_load_shape": "Неоднозначный состав нагрузки",
                                    "missing_or_unknown_side": (
                                        "Нет полного отчёта по обеим сторонам"
                                    ),
                                    "not_working": "Подход не относится к рабочим",
                                }.get(reason, "Нет применимого полного вклада в объём"),
                            }
                            for set_id, reason in result.excluded
                        ],
                    }
                )
        return results

    @staticmethod
    def _decode_cursor(cursor: str | None, filter_digest: str) -> list[str] | None:
        if cursor is None:
            return None
        try:
            raw = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("invalid progress cursor") from error
        boundary = payload.get("sort_key")
        if (
            payload.get("filter_digest") != filter_digest
            or not isinstance(boundary, list)
            or len(boundary) != 5
            or any(not isinstance(part, str) for part in boundary)
        ):
            raise RuntimeError("progress cursor does not match query filters")
        return boundary

    @staticmethod
    def _encode_cursor(filter_digest: str, sort_key: list[str]) -> str:
        raw = json.dumps(
            {"filter_digest": filter_digest, "sort_key": sort_key},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return urlsafe_b64encode(raw).decode().rstrip("=")


class ProgressDatabaseExecutor:
    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def query_progress(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        with self._session_factory() as session:
            return ProgressQueryRepository(session).query(payload)

    def __call__(self, *, action: str, **kwargs: Any) -> dict[str, Any]:
        if action != "query_progress":
            raise RuntimeError("unsupported progress action")
        return self.query_progress(**kwargs)


class ProgressAwareExecutor:
    """Route E-G03 reads/calculations and preserve the established command executor."""

    def __init__(
        self,
        engine: Engine,
        delegate: ContextDatabaseExecutor,
        *,
        eligible_exercise_ids: frozenset[UUID] = frozenset(),
        eligible_grip_conditions: frozenset[str] = frozenset(),
    ) -> None:
        self._progress = ProgressDatabaseExecutor(engine)
        self._delegate = delegate
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        self._eligible_exercise_ids = eligible_exercise_ids
        self._eligible_grip_conditions = frozenset(
            self._canonical_label(value) for value in eligible_grip_conditions
        )

    def __call__(self, *, action: str, **kwargs: Any) -> dict[str, Any]:
        if action == "query_progress":
            return self._progress(action=action, **kwargs)
        if action == "create_calculation_draft":
            return self._calculation_action(action, kwargs)
        if action == "create_preview" and self._is_calculation_revision(kwargs):
            return self._calculation_action(action, kwargs)
        if action == "confirm" and self._is_calculation_preview(kwargs):
            return self._calculation_action(action, kwargs)
        return self._delegate(action=action, **kwargs)

    def _calculation_action(self, action: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        with self._session_factory() as session:
            try:
                repository = CalculationRepository(session, self._load_e1rm)
                result: dict[str, Any] = getattr(repository, action)(**kwargs)
                session.commit()
                return result
            except Exception as error:
                session.rollback()
                self._delegate.record_failed_attempt(action, kwargs, error)
                raise

    def _is_calculation_revision(self, kwargs: dict[str, Any]) -> bool:
        revision_id = kwargs.get("payload", {}).get("revision_id")
        if not revision_id:
            return False
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text("""
                        SELECT 1 FROM trainer.draft_revisions r
                        JOIN trainer.drafts d USING(draft_id)
                        WHERE r.draft_revision_id=:revision AND d.intent_type='calculation'
                    """),
                    {"revision": revision_id},
                ).scalar_one_or_none()
            )

    def _is_calculation_preview(self, kwargs: dict[str, Any]) -> bool:
        preview_id = kwargs.get("preview_id")
        if not preview_id:
            return False
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text("""
                        SELECT 1 FROM trainer.previews p
                        JOIN trainer.draft_revisions r USING(draft_revision_id)
                        JOIN trainer.drafts d USING(draft_id)
                        WHERE p.preview_id=:preview AND d.intent_type='calculation'
                    """),
                    {"preview": preview_id},
                ).scalar_one_or_none()
            )

    @staticmethod
    def _ref(object_type: str, object_id: Any, revision_id: Any) -> dict[str, Any]:
        return {
            "object_type": object_type,
            "object_id": str(object_id),
            "revision_id": str(revision_id) if revision_id is not None else None,
        }

    @staticmethod
    def _canonical_label(label: str) -> str:
        normalized = unicodedata.normalize("NFC", label)
        return " ".join(normalized.split()).casefold()

    def _configured_grip_condition(self, label: str) -> str:
        grip_code = self._canonical_label(label)
        if not grip_code or grip_code not in self._eligible_grip_conditions:
            raise ContextRepositoryError(
                "unsupported_grip_condition",
                "exact grip condition has not been configured locally",
                422,
            )
        return grip_code

    def _load_e1rm(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("method_id") == "external_working_set_volume":
            return self._load_external_volume(session, payload)
        source = payload["source"]
        attestation = payload["execution_attestation"]
        if UUID(str(source["exercise_id"])) not in self._eligible_exercise_ids:
            raise ContextRepositoryError(
                "unsupported_exercise", "exact exercise identity has not been configured", 422
            )
        row = (
            session.execute(
                text("""
                    SELECT ws.profile_id,ws.current_revision_id,wsr.local_start_date,
                           ws.session_id,wsr.session_revision_id,ex.execution_id,
                           ex.exercise_id,ex.exercise_revision_id,ex.result,
                           er.canonical_name,er.category_code AS category,er.unilateral,
                           aset.actual_set_id,aset.set_type,aset.status,aset.full_reps,
                           aset.partial_reps,aset.failure_reached,aset.reported_rir,
                           (SELECT count(*) FROM trainer.actual_metric_values m
                            WHERE m.actual_set_id=aset.actual_set_id) AS metric_count
                    FROM trainer.workout_sessions ws
                    JOIN trainer.workout_session_revisions wsr
                      ON wsr.session_id=ws.session_id
                     AND wsr.session_revision_id=:session_revision
                    JOIN trainer.actual_blocks b ON b.session_revision_id=wsr.session_revision_id
                    JOIN trainer.exercise_executions ex ON ex.actual_block_id=b.actual_block_id
                    JOIN trainer.exercise_revisions er
                      ON er.exercise_id=ex.exercise_id
                     AND er.exercise_revision_id=ex.exercise_revision_id
                    JOIN trainer.actual_sets aset ON aset.execution_id=ex.execution_id
                    WHERE ws.session_id=:session AND ws.session_id=wsr.session_id
                      AND ws.current_revision_id=wsr.session_revision_id
                      AND ex.exercise_id=:exercise
                      AND ex.exercise_revision_id=:exercise_revision
                      AND aset.actual_set_id=:actual_set
                    FOR UPDATE OF ws
                """),
                {
                    "session": source["session_id"],
                    "session_revision": source["session_revision_id"],
                    "exercise": source["exercise_id"],
                    "exercise_revision": source["exercise_revision_id"],
                    "actual_set": source["actual_set_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError(
                "source_revision_not_current", "exact workout source is unavailable or stale", 409
            )
        if row["unilateral"]:
            raise ContextRepositoryError(
                "unsupported_exercise",
                "only the exact approved barbell bench exercise is supported",
                422,
            )
        if row["category"] != "strength" or row["result"] not in ("performed", "partial"):
            raise ContextRepositoryError(
                "ineligible_set_state", "exercise execution is ineligible", 422
            )
        loads = list(
            session.execute(
                text("""
                    SELECT actual_load_component_id,load_kind,equipment_id,equipment_snapshot,
                           item_count,mass,unit_code,mass_meaning,direction,raw_ambiguous_text
                    FROM trainer.actual_load_components
                    WHERE actual_set_id=:set ORDER BY position
                """),
                {"set": source["actual_set_id"]},
            ).mappings()
        )
        external = [item for item in loads if item["load_kind"] == "external"]
        if (
            not external
            or len(external) != len(loads)
            or any(
                item["raw_ambiguous_text"] is not None
                or item["mass"] is None
                or item["unit_code"] not in ("kilogram", "pound")
                or item["mass_meaning"] not in ("total_external_mass", "per_item_mass")
                or (item["mass_meaning"] == "per_item_mass" and item["item_count"] is None)
                for item in external
            )
        ):
            raise ContextRepositoryError(
                "known_external_load_required", "external load is ambiguous", 422
            )
        equipment_types: set[str] = set()
        mass = Decimal(0)
        for item in external:
            snapshot = dict(item["equipment_snapshot"] or {})
            revision_snapshot = snapshot.get("revision") or {}
            equipment_type = revision_snapshot.get("equipment_type") or snapshot.get(
                "equipment_type"
            )
            if equipment_type:
                equipment_types.add(str(equipment_type))
            component = Decimal(str(item["mass"]))
            if item["unit_code"] == "pound":
                component *= Decimal("0.45359237")
            if item["mass_meaning"] == "per_item_mass":
                component *= Decimal(str(item["item_count"]))
            mass += component
        if "barbell" not in equipment_types:
            raise ContextRepositoryError(
                "unsupported_load_configuration",
                "source lacks an exact barbell load component",
                422,
            )
        grip_label = str(attestation["grip_condition"])
        grip_code = self._configured_grip_condition(grip_label)
        typed_attestation = E1RMAttestation(
            source_actual_set_id=str(source["actual_set_id"]),
            grip_condition_code=grip_code,
            bench_pause_condition=(
                "paused"
                if attestation["bench_pause_condition"] == "intentional_chest_pause"
                else "touch_and_go"
            ),
            standard_confirmed=attestation["standard_confirmed"],
            spotter_released_before_rep=attestation["spotter_released_before_rep"],
            no_assistance_during_rep=attestation["no_assistance_during_rep"],
        )
        value = E1RMInput(
            exercise_code="barbell_bench_press",
            actual_set_id=str(row["actual_set_id"]),
            source_revision_id=str(row["session_revision_id"]),
            set_type=str(row["set_type"]),
            status=str(row["status"]),
            full_reps=row["full_reps"],
            partial_reps=row["partial_reps"],
            failure_reached=row["failure_reached"],
            reported_rir=row["reported_rir"],
            load_kg=mass,
            load_kind="external",
            attestation=typed_attestation,
        )
        try:
            result = calculate_brzycki_e1rm(value)
        except ValueError as error:
            raise ContextRepositoryError(
                str(error), "source set is ineligible for e1RM", 422
            ) from error
        condition = {
            "exercise_id": str(row["exercise_id"]),
            "applicability_version": "e1rm_brzycki_applicability_v1",
            "grip_condition_code": grip_code,
            "bench_pause_condition": attestation["bench_pause_condition"],
            "bench_standard_version": "barbell_bench_standard_v1",
            "load_kind": "external",
            "mass_meaning": "total_external_mass",
            "method_id": "e1rm_brzycki",
            "method_version": "1",
        }
        rule_version = "e1rm_brzycki_condition_v1"
        normalized_key = canonical_condition_key(condition)
        condition_row = (
            session.execute(
                text("""
                SELECT condition_id,readable_snapshot FROM trainer.comparison_conditions
                WHERE exercise_id=:exercise AND rule_version=:rule AND normalized_key=:key
            """),
                {"exercise": row["exercise_id"], "rule": rule_version, "key": normalized_key},
            )
            .mappings()
            .one_or_none()
        )
        condition_id = condition_row["condition_id"] if condition_row else None
        readable_condition = {
            "exercise_id": str(row["exercise_id"]),
            "grip_condition_code": grip_code,
            "bench_pause_condition": attestation["bench_pause_condition"],
            "bench_standard_version": "barbell_bench_standard_v1",
            "load_kind": "external",
            "mass_meaning": "total_external_mass",
            "method_id": "e1rm_brzycki",
            "method_version": "1",
        }
        prior_calculation = None
        prior_id = payload.get("prior_derived_value_id")
        if prior_id is not None:
            if condition_id is None:
                raise ContextRepositoryError(
                    "invalid_dependency",
                    "selected prior calculation has no matching condition",
                    409,
                )
            prior = (
                session.execute(
                    text("""
                        SELECT d.derived_value_id,d.state,d.supersedes_derived_value_id,
                               d.profile_id,d.method_id,d.method_version,d.result_value,d.unit_code,
                               p.exercise_id,p.condition_id,r.local_start_date
                        FROM trainer.derived_values d
                        JOIN trainer.derived_progress_points p USING(derived_value_id)
                        JOIN trainer.workout_session_revisions r
                          ON r.session_id=p.session_id
                         AND r.session_revision_id=p.session_revision_id
                        WHERE d.derived_value_id=:id
                          AND d.profile_id=:profile AND d.method_id='e1rm_brzycki'
                          AND d.method_version='1' AND d.state='stale'
                          AND d.supersedes_derived_value_id IS NULL
                          AND p.exercise_id=:exercise AND p.condition_id=:condition
                          AND NOT EXISTS (
                              SELECT 1 FROM trainer.derived_values successor
                              WHERE successor.supersedes_derived_value_id=d.derived_value_id
                          )
                        FOR UPDATE OF d
                    """),
                    {
                        "id": prior_id,
                        "profile": row["profile_id"],
                        "exercise": row["exercise_id"],
                        "condition": condition_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if prior is None:
                raise ContextRepositoryError(
                    "invalid_dependency",
                    "selected prior calculation is unavailable or incompatible",
                    409,
                )
            prior_calculation = {
                "derived_value_id": str(prior["derived_value_id"]),
                "derived_value_ref": self._ref("derived_value", prior["derived_value_id"], None),
                "value": str(prior["result_value"]),
                "unit": prior["unit_code"],
                "achieved_on": prior["local_start_date"].isoformat(),
                "state": prior["state"],
                "method_id": prior["method_id"],
                "method_version": prior["method_version"],
                "condition_id": str(prior["condition_id"]),
            }
        current_best = None
        if condition_id is not None:
            current_best = (
                session.execute(
                    text("""
                        SELECT b.calculated_best_id,b.value,b.achieved_on,b.source_actual_set_id,
                               b.derived_value_id,d.state
                        FROM trainer.calculated_bests b
                        JOIN trainer.derived_values d USING(derived_value_id)
                        WHERE b.profile_id=:profile AND b.exercise_id=:exercise
                          AND b.metric_kind='estimated_1rm' AND b.condition_id=:condition
                          AND b.is_current AND d.state='current'
                        FOR UPDATE OF b
                    """),
                    {
                        "profile": row["profile_id"],
                        "exercise": row["exercise_id"],
                        "condition": condition_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
        input_snapshots: list[dict[str, Any]] = [
            {
                "role": "session_revision",
                "source_ref": self._ref(
                    "workout_session", row["session_id"], row["session_revision_id"]
                ),
                "normalized_value": None,
                "unit_code": None,
                "snapshot": {"local_start_date": row["local_start_date"].isoformat()},
            },
            {
                "role": "exercise_revision",
                "source_ref": self._ref(
                    "exercise", row["exercise_id"], row["exercise_revision_id"]
                ),
                "normalized_value": None,
                "unit_code": None,
                "snapshot": {"canonical_name": row["canonical_name"], "category": row["category"]},
            },
            {
                "role": "actual_set",
                "source_ref": self._ref(
                    "actual_set", row["actual_set_id"], row["session_revision_id"]
                ),
                "normalized_value": str(row["full_reps"]),
                "unit_code": "repetition",
                "snapshot": {
                    "set_type": row["set_type"],
                    "status": row["status"],
                    "full_reps": row["full_reps"],
                    "partial_reps": row["partial_reps"],
                    "failure_reached": row["failure_reached"],
                    "reported_rir": str(row["reported_rir"])
                    if row["reported_rir"] is not None
                    else None,
                },
            },
            {
                "role": "execution_attestation",
                "source_ref": self._ref(
                    "actual_set", row["actual_set_id"], row["session_revision_id"]
                ),
                "normalized_value": None,
                "unit_code": None,
                "snapshot": {
                    **attestation,
                    "grip_condition_code": grip_code,
                    "standard_version": "barbell_bench_standard_v1",
                    "standard_text": (
                        "Полное разгибание рук в начале и конце; "
                        "контролируемое касание груди без отбива; "
                        "ягодицы остаются на скамье; упор ногами разрешён; "
                        "страхующий отпускает штангу "
                        "до повтора; помощь во время повтора запрещена."
                    ),
                },
            },
            *[
                {
                    "role": "load_component",
                    "source_ref": self._ref(
                        "actual_load_component",
                        item["actual_load_component_id"],
                        row["session_revision_id"],
                    ),
                    "normalized_value": str(item["mass"]),
                    "unit_code": item["unit_code"],
                    "snapshot": json.loads(json.dumps(dict(item), default=str)),
                }
                for item in external
            ],
        ]
        input_snapshots.sort(
            key=lambda item: (
                item["role"],
                item["source_ref"]["object_type"],
                item["source_ref"]["object_id"],
                item["source_ref"]["revision_id"],
            )
        )
        inputs_digest_payload = {
            "method_id": "e1rm_brzycki",
            "method_version": "1",
            "applicability_version": "e1rm_brzycki_applicability_v1",
            "inputs": input_snapshots,
            "condition": condition,
        }
        input_digest = digest_payload(inputs_digest_payload)
        result_digest = digest_payload(
            {
                "method_id": "e1rm_brzycki",
                "method_version": "1",
                "metric_kind": "estimated_1rm",
                "value": str(result),
                "unit_code": "kilogram",
                "completeness": "complete",
            }
        )
        inputs_digest_payload["input_digest"] = input_digest
        inputs_digest_payload["result_digest"] = result_digest
        best_value = Decimal(str(current_best["value"])) if current_best else None
        best_decision = classify_calculated_best(result, best_value)
        return {
            "profile_id": str(row["profile_id"]),
            "source": {
                **source,
                "local_start_date": row["local_start_date"].isoformat(),
            },
            "exercise": {"canonical_name": row["canonical_name"]},
            "condition": {
                "rule_version": rule_version,
                "normalized_key": normalized_key,
                "readable_snapshot": readable_condition,
                "existing_condition_id": str(condition_id) if condition_id else None,
                "condition_will_be_created": condition_id is None,
            },
            "normalized_inputs": input_snapshots,
            "value": {"exact_value": str(result), "unit_code": "kilogram"},
            "metric_kind": "estimated_1rm",
            "method_id": "e1rm_brzycki",
            "method_version": "1",
            "applicability_version": "e1rm_brzycki_applicability_v1",
            "completeness": "complete",
            "input_digest": input_digest,
            "result_digest": result_digest,
            "condition_id": str(condition_id) if condition_id else None,
            "condition_key": normalized_key,
            "condition_rule_version": rule_version,
            "condition_snapshot": readable_condition,
            "prior_calculation": prior_calculation,
            "mass_kg": str(mass),
            "attestation": attestation,
            "calculation_assumptions": inputs_digest_payload,
            "best_effect": {
                "decision": best_decision,
                "previous": {
                    "calculated_best_id": str(current_best["calculated_best_id"]),
                    "value": str(current_best["value"]),
                    "achieved_on": current_best["achieved_on"].isoformat(),
                }
                if current_best
                else None,
                "candidate": {
                    "value": str(result),
                    "achieved_on": row["local_start_date"].isoformat(),
                    "source_actual_set_id": str(row["actual_set_id"]),
                },
            },
            "change_plan": [
                {"object_type": "derived_value", "object_id": "pending", "revision_id": None},
                {
                    "object_type": "derived_progress_point",
                    "object_id": "pending",
                    "revision_id": None,
                },
            ],
            "gaps": [],
        }

    def _load_external_volume(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload["source"]
        row = (
            session.execute(
                text("""
                    SELECT s.profile_id,s.session_id,r.session_revision_id,r.local_start_date,
                           e.exercise_id,e.exercise_revision_id
                    FROM trainer.workout_sessions s
                    JOIN trainer.workout_session_revisions r
                      ON r.session_id=s.session_id AND r.session_revision_id=s.current_revision_id
                    JOIN trainer.actual_blocks b ON b.session_revision_id=r.session_revision_id
                    JOIN trainer.exercise_executions e USING(actual_block_id)
                    WHERE s.session_id=:session AND r.session_revision_id=:revision
                      AND e.exercise_id=:exercise AND s.archived_at IS NULL
                      AND s.logically_deleted_at IS NULL
                      AND s.profile_id=(SELECT profile_id FROM trainer.profiles
                                        WHERE archived_at IS NULL)
                    ORDER BY e.position LIMIT 1
                """),
                {
                    "session": source["session_id"],
                    "revision": source["session_revision_id"],
                    "exercise": source["exercise_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError(
                "source_revision_not_current",
                "exact current workout session and exercise are required",
                409,
            )
        items = ProgressQueryRepository(session)._volume_items(
            row["profile_id"],
            row["local_start_date"],
            row["local_start_date"],
            UUID(str(row["exercise_id"])),
        )
        point = next(
            (
                item
                for item in items
                if item["source_session_ref"]["object_id"] == str(row["session_id"])
                and item["source_session_ref"]["revision_id"] == str(row["session_revision_id"])
            ),
            None,
        )
        session_ref = self._ref("workout_session", row["session_id"], row["session_revision_id"])
        exercise_ref = self._ref("exercise", row["exercise_id"], row["exercise_revision_id"])
        inputs = (
            point["inputs"]
            if point
            else [
                {
                    "role": "session_revision",
                    "source_ref": self._ref(
                        "workout_session_revision", row["session_revision_id"], None
                    ),
                    "normalized_value": None,
                    "unit_code": None,
                    "snapshot": {"local_start_date": row["local_start_date"].isoformat()},
                },
                {
                    "role": "exercise_revision",
                    "source_ref": exercise_ref,
                    "normalized_value": None,
                    "unit_code": None,
                    "snapshot": {},
                },
            ]
        )
        value = point["value"] if point else None
        completeness = point["completeness"] if point else "insufficient"
        exclusions = point["excluded_sources"] if point else []
        included_count = point["included_count"] if point else 0
        excluded_count = point["excluded_count"] if point else 0
        digest_inputs = {
            "method_id": "external_working_set_volume",
            "method_version": "1",
            "source": source,
            "inputs": inputs,
            "exclusions": exclusions,
        }
        input_digest = digest_payload(digest_inputs)
        result_digest = digest_payload(
            {
                "method_id": "external_working_set_volume",
                "method_version": "1",
                "metric_kind": "external_working_set_volume",
                "value": str(value["exact_value"]) if value else None,
                "unit_code": value["unit_code"] if value else None,
                "completeness": completeness,
            }
        )
        return {
            "method_id": "external_working_set_volume",
            "method_version": "1",
            "metric_kind": "external_working_set_volume",
            "value": {
                "exact_value": str(value["exact_value"]),
                "unit_code": value["unit_code"],
            }
            if value
            else None,
            "completeness": completeness,
            "input_digest": input_digest,
            "result_digest": result_digest,
            "applicability_version": "external_working_set_volume_v1",
            "included_count": included_count,
            "excluded_count": excluded_count,
            "normalized_inputs": inputs,
            "exclusions": exclusions,
            "attestation": None,
            "source_refs": [session_ref, exercise_ref],
        }
