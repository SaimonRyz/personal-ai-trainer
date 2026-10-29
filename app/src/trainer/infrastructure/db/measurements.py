# ruff: noqa: E501
"""E-G06 immutable body measurements using shared draft/preview/confirmation."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from trainer.domain.context import digest_payload
from trainer.domain.measurements import (
    FACTORS,
    HEIGHT,
    KINDS,
    MASS,
    SCALE,
    calculate_bmi,
    normalize_session,
    select_latest_candidate,
)
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError


def _decimal(value: Any) -> str:
    return format(Decimal(str(value)), "f")


class MeasurementRepository:
    def __init__(self, context: ContextRepository) -> None:
        self.context = context
        self.session = context.session

    def _profile(self) -> dict[str, Any]:
        profile = (
            self.session.execute(
                text(
                    "SELECT profile_id,current_revision_id FROM trainer.profiles WHERE archived_at IS NULL"
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile is None:
            raise ContextRepositoryError("insufficient_data", "confirmed profile required", 422)
        return dict(profile)

    def _lock_profile(self, profile_id: str) -> None:
        """Serialize all E-G06 confirms before inspecting duplicate/current state."""
        found = self.session.execute(
            text(
                "SELECT profile_id FROM trainer.profiles "
                "WHERE profile_id=:profile AND archived_at IS NULL FOR UPDATE"
            ),
            {"profile": profile_id},
        ).scalar_one_or_none()
        if found is None:
            raise ContextRepositoryError("stale_dependency", "measurement profile changed", 409)

    def _catalog_digest(self, *, lock: bool = False) -> str:
        """Pin closed unit/type rows and the exact application conversion rule."""
        if lock:
            self.session.execute(text("SELECT trainer.lock_measurement_catalog()"))
        unit_rows = self.session.execute(
            text(
                "SELECT code,description,seed_version,archived_at FROM trainer.unit_catalog "
                "WHERE code IN ('kilogram','pound','centimeter','millimeter','inch') "
                "ORDER BY code"
            )
        ).mappings()
        kind_rows = self.session.execute(
            text(
                "SELECT code,description,seed_version,archived_at "
                "FROM trainer.measurement_kind_catalog ORDER BY code"
            )
        ).mappings()
        units = [dict(row) for row in unit_rows]
        kinds = [dict(row) for row in kind_rows]
        if (
            {row["code"] for row in units} != set(FACTORS)
            or {row["code"] for row in kinds} != KINDS
            or any(
                row["seed_version"] != 1 or row["archived_at"] is not None
                for row in [*units, *kinds]
            )
        ):
            raise ContextRepositoryError(
                "inconsistent_domain_state", "measurement catalog is inconsistent", 500
            )
        return digest_payload(
            {
                "units": [
                    {
                        "code": row["code"],
                        "description": row["description"],
                        "seed_version": row["seed_version"],
                    }
                    for row in units
                ],
                "kinds": [
                    {
                        "code": row["code"],
                        "description": row["description"],
                        "seed_version": row["seed_version"],
                    }
                    for row in kinds
                ],
                "conversion_version": "body_units_v1",
                "factors": {code: str(factor) for code, factor in sorted(FACTORS.items())},
                "scale": str(SCALE),
                "rounding": "ROUND_HALF_UP",
            }
        )

    def _current(self, root_id: str, profile_id: str, *, lock: bool = False) -> str | None:
        suffix = " FOR UPDATE" if lock else ""
        value = self.session.execute(
            text(
                "SELECT current_session_id FROM trainer.body_measurement_heads "
                "WHERE measurement_root_id=:root AND profile_id=:profile" + suffix
            ),
            {"root": root_id, "profile": profile_id},
        ).scalar_one_or_none()
        return str(value) if value is not None else None

    def _read_session(self, session_id: str) -> dict[str, Any]:
        row = (
            self.session.execute(
                text(
                    "SELECT s.*,h.current_session_id FROM trainer.body_measurement_sessions s "
                    "JOIN trainer.body_measurement_heads h ON h.measurement_root_id=s.measurement_root_id "
                    "AND h.profile_id=s.profile_id WHERE s.measurement_session_id=:session"
                ),
                {"session": session_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContextRepositoryError("not_found", "measurement session not found", 404)
        values = self.session.execute(
            text(
                "SELECT * FROM trainer.body_measurement_values WHERE measurement_session_id=:session "
                "ORDER BY measurement_kind,side NULLS FIRST,measurement_value_id"
            ),
            {"session": session_id},
        ).mappings()
        return {
            "measurement_root_id": str(row["measurement_root_id"]),
            "session_id": str(row["measurement_session_id"]),
            "supersedes_session_id": str(row["supersedes_session_id"])
            if row["supersedes_session_id"]
            else None,
            "current": row["measurement_session_id"] == row["current_session_id"],
            "measured_on": row["measured_on"].isoformat(),
            "measured_time": row["measured_time"].isoformat() if row["measured_time"] else None,
            "conditions": row["conditions"],
            "comment": row["comment"],
            "confirmation_id": str(row["confirmation_id"]),
            "values": [self._value_view(dict(value)) for value in values],
        }

    @staticmethod
    def _value_view(value: dict[str, Any]) -> dict[str, Any]:
        verified = value["conversion_version"] == "body_units_v1"
        if verified:
            expected_unit = "kilogram" if value["measurement_kind"] == MASS else "centimeter"
            if (
                value["normalized_value"] is None
                or value["normalized_value"] <= 0
                or value["normalized_unit"] != expected_unit
            ):
                raise ContextRepositoryError(
                    "inconsistent_domain_state",
                    "verified measurement normalization is invalid",
                    500,
                )
        return {
            "measurement_value_id": str(value["measurement_value_id"]),
            "measurement_kind": value["measurement_kind"],
            "original_value": _decimal(value["original_value"]),
            "original_unit": value["original_unit"],
            "normalized_value": _decimal(value["normalized_value"])
            if verified and value["normalized_value"] is not None
            else None,
            "normalized_unit": value["normalized_unit"] if verified else None,
            "conversion_version": value["conversion_version"] if verified else None,
            "side": value["side"],
            "method_description": value["method_description"],
            "method_id": str(value["method_id"]) if value["method_id"] else None,
            "comment": value["comment"],
            "normalization_status": "verified_v1" if verified else "legacy_unverified",
        }

    def _resolve_method(
        self, value: dict[str, Any], profile_id: str
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        method = value["method"]
        if method is None or method["mode"] == "unknown":
            return None, None, None
        if method["mode"] == "new":
            description = method["description"].strip()
            if not description:
                raise ContextRepositoryError(
                    "validation_error", "method description must be nonblank"
                )
            return str(uuid4()), description, None
        source = (
            self.session.execute(
                text(
                    "SELECT v.measurement_value_id,v.measurement_kind,v.method_id,v.method_description,"
                    "s.measurement_session_id,s.measurement_root_id,h.current_session_id "
                    "FROM trainer.body_measurement_values v "
                    "JOIN trainer.body_measurement_sessions s ON s.measurement_session_id=v.measurement_session_id "
                    "JOIN trainer.body_measurement_heads h ON h.measurement_root_id=s.measurement_root_id "
                    "AND h.profile_id=s.profile_id WHERE v.measurement_value_id=:value AND s.profile_id=:profile"
                ),
                {"value": method["source_measurement_value_id"], "profile": profile_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            source is None
            or source["measurement_kind"] != value["measurement_kind"]
            or source["method_id"] is None
            or source["current_session_id"] != source["measurement_session_id"]
        ):
            raise ContextRepositoryError(
                "validation_error",
                "method source must be a current confirmed value of the same type",
            )
        return (
            str(source["method_id"]),
            source["method_description"],
            {
                "source_measurement_value_id": str(source["measurement_value_id"]),
                "source_session_id": str(source["measurement_session_id"]),
                "source_root_id": str(source["measurement_root_id"]),
            },
        )

    def _duplicates(
        self, profile_id: str, session: dict[str, Any], exclude_root: str | None
    ) -> list[str]:
        pairs = {(v["measurement_kind"], v["side"]) for v in session["values"]}
        rows = self.session.execute(
            text(
                "SELECT DISTINCT s.measurement_session_id,s.measurement_root_id,v.measurement_kind,v.side "
                "FROM trainer.body_measurement_heads h JOIN trainer.body_measurement_sessions s "
                "ON s.measurement_session_id=h.current_session_id "
                "JOIN trainer.body_measurement_values v ON v.measurement_session_id=s.measurement_session_id "
                "WHERE h.profile_id=:profile AND s.measured_on=:day "
                "AND s.measured_time IS NOT DISTINCT FROM :clock"
            ),
            {
                "profile": profile_id,
                "day": session["measured_on"],
                "clock": session["measured_time"],
            },
        ).mappings()
        return sorted(
            {
                str(r["measurement_session_id"])
                for r in rows
                if str(r["measurement_root_id"]) != exclude_root
                and (r["measurement_kind"], r["side"]) in pairs
            }
        )

    def _series_members(
        self, profile_id: str, kind: str, side: str | None, method_id: str | None
    ) -> list[dict[str, str | None]]:
        if kind not in (MASS, HEIGHT) and method_id is None:
            return []
        rows = self.session.execute(
            text(
                "SELECT s.measurement_session_id,v.measurement_value_id,s.measured_on,s.measured_time "
                "FROM trainer.body_measurement_heads h "
                "JOIN trainer.body_measurement_sessions s ON s.measurement_session_id=h.current_session_id "
                "JOIN trainer.body_measurement_values v ON v.measurement_session_id=s.measurement_session_id "
                "WHERE h.profile_id=:profile AND v.measurement_kind=:kind "
                "AND v.side IS NOT DISTINCT FROM :side "
                "AND v.method_id IS NOT DISTINCT FROM :method "
                "AND v.conversion_version='body_units_v1' "
                "ORDER BY s.measured_on DESC,s.measured_time DESC NULLS LAST,s.measurement_session_id DESC"
            ),
            {"profile": profile_id, "kind": kind, "side": side, "method": method_id},
        ).mappings()
        return [
            {
                "session_id": str(row["measurement_session_id"]),
                "measurement_value_id": str(row["measurement_value_id"]),
                "measured_on": row["measured_on"].isoformat(),
                "measured_time": row["measured_time"].isoformat() if row["measured_time"] else None,
            }
            for row in rows
        ]

    def _series_impacts(
        self,
        profile_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        new_session_id: str,
    ) -> list[dict[str, Any]]:
        impacts: list[dict[str, Any]] = []
        old_id = before["session_id"] if before else None
        for old in before["values"] if before else []:
            members = (
                self._series_members(
                    profile_id, old["measurement_kind"], old["side"], old["method_id"]
                )
                if old["normalization_status"] == "verified_v1"
                else []
            )
            impacts.append(
                {
                    "phase": "before",
                    "measurement_kind": old["measurement_kind"],
                    "side": old["side"],
                    "method_id": old["method_id"],
                    "comparability": "comparable" if members else "not_in_comparable_series",
                    "existing_current_points": members,
                    "loses_current_point": next(
                        (p for p in members if p["session_id"] == old_id), None
                    ),
                    "gains_current_point": None,
                    "prospective_current_points": [p for p in members if p["session_id"] != old_id],
                }
            )
        for new in after["values"]:
            method = new["method"]
            mode = "simple" if method is None else method["mode"]
            members = self._series_members(
                profile_id, new["measurement_kind"], new["side"], new["method_id"]
            )
            prospective = [p for p in members if p["session_id"] != old_id]
            point = {
                "session_id": new_session_id,
                "measurement_value_id": new["measurement_value_id"],
                "measured_on": after["measured_on"],
                "measured_time": after["measured_time"],
            }
            if mode != "unknown":
                prospective.append(point)
            impacts.append(
                {
                    "phase": "after",
                    "measurement_kind": new["measurement_kind"],
                    "side": new["side"],
                    "method_id": new["method_id"],
                    "method_mode": mode,
                    "method_source": new.get("method_source"),
                    "comparability": "not_in_comparable_series"
                    if mode == "unknown"
                    else "comparable",
                    "existing_current_points": members,
                    "loses_current_point": next(
                        (p for p in members if p["session_id"] == old_id), None
                    ),
                    "gains_current_point": point if mode != "unknown" else None,
                    "prospective_current_points": prospective,
                    "new_series_even_if_description_matches": mode == "new",
                }
            )
        return impacts

    def create_draft(
        self,
        *,
        payload: dict[str, Any],
        key: str,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        operation_id, replay = self.context._reserve("CMD-11", key, payload["request_digest"])
        if replay is not None:
            self.context._audit_replay(
                operation_id, "CMD-11", correlation_id, causation_id, key, replay
            )
            return replay
        profile = self._profile()
        profile_id = str(profile["profile_id"])
        self._lock_profile(profile_id)
        catalog_digest = self._catalog_digest()
        session = normalize_session(payload["session"])
        intent = payload["intent"]
        new_session_id = str(uuid4())
        root_id = new_session_id if intent == "create" else payload["measurement_root_id"]
        before = None
        expected = None
        if intent == "correct":
            expected = payload["expected_current_session_id"]
            current = self._current(root_id, profile_id)
            if current is None:
                raise ContextRepositoryError("not_found", "current measurement root not found", 404)
            if current != expected:
                raise ContextRepositoryError(
                    "revision_conflict", "measurement current session changed", 409
                )
            before = self._read_session(current)
        prepared = []
        for value in session["values"]:
            method_id, method_description, source = self._resolve_method(value, profile_id)
            prepared.append(
                {
                    **value,
                    "measurement_value_id": str(uuid4()),
                    "method_id": method_id,
                    "method_description": method_description,
                    "method_source": source,
                }
            )
        session["values"] = prepared
        duplicates = self._duplicates(profile_id, session, root_id if intent == "correct" else None)
        series_impacts = self._series_impacts(profile_id, before, session, new_session_id)
        typed = {
            "measurement_change": intent,
            "measurement_root_id": root_id,
            "profile_id": profile_id,
            "expected_current_session_id": expected,
            "before": before,
            "after": session,
            "reason": payload.get("reason"),
            "duplicates": duplicates,
            "duplicate_warning": {
                "kind": "potential_duplicate",
                "session_ids": duplicates,
                "automatic_merge": False,
            }
            if duplicates
            else None,
            "series_impacts": series_impacts,
            "explanation": self._preview_explanation(before, session, duplicates),
            "conversion_version": "body_units_v1",
            "catalog_digest": catalog_digest,
            "change_plan": [
                {
                    "object_type": "body_measurement_session",
                    "object_id": root_id,
                    "revision_id": new_session_id,
                }
            ],
        }
        draft_id, revision_id, now = uuid4(), uuid4(), datetime.now(UTC)
        digest = digest_payload(typed)
        self.session.execute(
            text(
                "INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at) "
                "VALUES (:draft,'measurement',:profile,'open',:revision,:expires,:now)"
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
                "INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,content_digest,trust_state,confirmation_kind,created_at) "
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
        deps = [
            self.context._add_dependency(
                revision_id,
                "profile",
                profile["profile_id"],
                profile["current_revision_id"],
                "measurement owner",
                now,
            )
        ]
        response = {
            "draft_id": str(draft_id),
            "revision_id": str(revision_id),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": deps,
            "gaps": [],
            "preview_candidate": {
                "summary": "Полный сеанс замеров готов к предпросмотру",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }
        self.context._finish(operation_id, response)
        self.context._audit(
            operation_id,
            "CMD-11",
            correlation_id,
            causation_id,
            key,
            [{"object_type": "draft", "object_id": str(draft_id), "revision_id": str(revision_id)}],
        )
        return response

    @staticmethod
    def _preview_explanation(
        before: dict[str, Any] | None, after: dict[str, Any], duplicates: list[str]
    ) -> list[str]:
        units = {
            "kilogram": "кг",
            "pound": "фунт",
            "centimeter": "см",
            "millimeter": "мм",
            "inch": "дюйм",
        }
        lines = []
        if before:
            lines.append(
                "Исправление заменит весь текущий сеанс. Прежняя редакция останется в истории; её точки выйдут из текущих рядов, если участвовали в них."
            )
        if duplicates:
            lines.append(
                "Возможный дубль сеансов: "
                + ", ".join(duplicates)
                + ". После явного подтверждения сохранится отдельный сеанс; автоматического объединения нет."
            )
        for value in after["values"]:
            lines.append(
                f"{value['measurement_kind']}, сторона: {value['side'] or 'не применима'}; исходное значение {value['original_value']} {units[value['original_unit']]}; расчёт преобразования {value['normalized_value']} {units[value['normalized_unit']]} по body_units_v1."
            )
            method = value["method"]
            if method is None:
                lines.append("Точка войдёт в сравнимый ряд своего типа.")
            elif method["mode"] == "unknown":
                lines.append(
                    "Способ замера неизвестен. Факт сохранится, но не войдёт в сравнимый ряд."
                )
            elif method["mode"] == "new":
                lines.append(
                    "Новый способ замера создаёт отдельный ряд даже при совпадении описания с прежним; точный ID метода и состав ряда показаны в series_impacts."
                )
            else:
                lines.append(
                    "Явно выбран прежний способ замера по точному источнику. Точка войдёт в ряд того же метода, типа и стороны; источник, ID метода и состав ряда показаны в series_impacts."
                )
        return lines

    def check_fresh(self, typed: dict[str, Any]) -> None:
        profile_id = typed["profile_id"]
        self._lock_profile(profile_id)
        try:
            catalog_digest = self._catalog_digest(lock=True)
        except ContextRepositoryError as error:
            if error.code != "inconsistent_domain_state":
                raise
            raise ContextRepositoryError(
                "stale_dependency", "measurement conversion rules or catalog changed", 409
            ) from error
        if catalog_digest != typed["catalog_digest"]:
            raise ContextRepositoryError(
                "stale_dependency", "measurement conversion rules or catalog changed", 409
            )
        if typed["measurement_change"] == "correct":
            current = self._current(typed["measurement_root_id"], profile_id, lock=True)
            if current != typed["expected_current_session_id"]:
                raise ContextRepositoryError(
                    "stale_preview", "measurement current session changed", 409
                )
        for value in typed["after"]["values"]:
            source = value.get("method_source")
            if (
                source
                and self._current(source["source_root_id"], profile_id, lock=True)
                != source["source_session_id"]
            ):
                raise ContextRepositoryError("stale_preview", "method source changed", 409)
        if (
            self._duplicates(
                profile_id,
                typed["after"],
                typed["measurement_root_id"] if typed["measurement_change"] == "correct" else None,
            )
            != typed["duplicates"]
        ):
            raise ContextRepositoryError("stale_preview", "potential duplicates changed", 409)
        if (
            self._series_impacts(
                profile_id, typed["before"], typed["after"], typed["change_plan"][0]["revision_id"]
            )
            != typed["series_impacts"]
        ):
            raise ContextRepositoryError("stale_preview", "measurement series impact changed", 409)

    def commit(
        self,
        typed: dict[str, Any],
        confirmation: UUID,
        result: UUID,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        self.check_fresh(typed)
        change = changes[0]
        root_id = typed["measurement_root_id"]
        session_id = change["revision_id"]
        before_id = typed["expected_current_session_id"]
        after = typed["after"]
        self.session.execute(
            text(
                "INSERT INTO trainer.body_measurement_sessions(measurement_session_id,profile_id,measurement_root_id,"
                "measured_on,measured_time,conditions,comment,supersedes_session_id,confirmation_id,created_at) "
                "VALUES (:session,:profile,:root,:day,:clock,:conditions,:comment,:before,:confirmation,:now)"
            ),
            {
                "session": session_id,
                "profile": typed["profile_id"],
                "root": root_id,
                "day": after["measured_on"],
                "clock": after["measured_time"],
                "conditions": after["conditions"],
                "comment": after["comment"],
                "before": before_id,
                "confirmation": confirmation,
                "now": now,
            },
        )
        for value in after["values"]:
            self.session.execute(
                text(
                    "INSERT INTO trainer.body_measurement_values(measurement_value_id,measurement_session_id,"
                    "measurement_kind,original_value,original_unit,normalized_value,normalized_unit,side,"
                    "method_description,method_id,conversion_version,comment,created_at) "
                    "VALUES (:id,:session,:kind,:original,:unit,:normalized,:normal_unit,:side,"
                    ":description,:method,:version,:comment,:now)"
                ),
                {
                    "id": value["measurement_value_id"],
                    "session": session_id,
                    "kind": value["measurement_kind"],
                    "original": value["original_value"],
                    "unit": value["original_unit"],
                    "normalized": value["normalized_value"],
                    "normal_unit": value["normalized_unit"],
                    "side": value["side"],
                    "description": value["method_description"],
                    "method": value["method_id"],
                    "version": value["conversion_version"],
                    "comment": value["comment"],
                    "now": now,
                },
            )
        if typed["measurement_change"] == "create":
            self.session.execute(
                text(
                    "INSERT INTO trainer.body_measurement_heads(measurement_root_id,profile_id,current_session_id,created_at) "
                    "VALUES (:root,:profile,:session,:now)"
                ),
                {
                    "root": root_id,
                    "profile": typed["profile_id"],
                    "session": session_id,
                    "now": now,
                },
            )
        else:
            self.session.execute(
                text(
                    "UPDATE trainer.body_measurement_heads SET current_session_id=:session "
                    "WHERE measurement_root_id=:root AND profile_id=:profile AND current_session_id=:before"
                ),
                {
                    "session": session_id,
                    "root": root_id,
                    "profile": typed["profile_id"],
                    "before": before_id,
                },
            )
        self.context._insert_change(
            change,
            result,
            typed["measurement_change"],
            typed["before"],
            self._read_session(session_id),
            typed["reason"],
            now,
        )

    def query(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        self.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        profile_id = str(self._profile()["profile_id"])
        mode = payload["mode"]
        if mode == "bmi":
            return self._bmi(profile_id)
        rows = list(
            self.session.execute(
                text(
                    "SELECT s.measurement_session_id,s.measured_on,s.measured_time,"
                    "h.current_session_id,s.measurement_root_id FROM trainer.body_measurement_sessions s "
                    "JOIN trainer.body_measurement_heads h ON h.measurement_root_id=s.measurement_root_id "
                    "AND h.profile_id=s.profile_id WHERE s.profile_id=:profile "
                    "ORDER BY s.measured_on DESC,s.measured_time DESC NULLS LAST,s.measurement_session_id DESC"
                ),
                {"profile": profile_id},
            ).mappings()
        )
        if mode == "series":
            source_id = payload["method_source_value_id"]
            method_id = None
            if source_id is not None:
                source = (
                    self.session.execute(
                        text(
                            "SELECT v.method_id,v.measurement_kind FROM trainer.body_measurement_values v "
                            "JOIN trainer.body_measurement_sessions s ON s.measurement_session_id=v.measurement_session_id "
                            "WHERE v.measurement_value_id=:id AND s.profile_id=:profile"
                        ),
                        {"id": source_id, "profile": profile_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    source is None
                    or source["measurement_kind"] != payload["measurement_kind"]
                    or source["method_id"] is None
                ):
                    raise ContextRepositoryError("validation_error", "invalid method source")
                method_id = str(source["method_id"])
        else:
            method_id = None
        selected = []
        for row in rows:
            current = row["measurement_session_id"] == row["current_session_id"]
            if mode == "sessions" and (
                (payload["view"] == "current" and not current)
                or (payload["view"] == "history" and current)
            ):
                continue
            if mode == "series" and not current:
                continue
            if (
                payload["date_from"]
                and not payload["date_from"] <= row["measured_on"].isoformat() <= payload["date_to"]
            ):
                continue
            session = self._read_session(str(row["measurement_session_id"]))
            if mode == "sessions":
                if (
                    payload["measurement_kind"] is not None or payload["side"] is not None
                ) and not any(
                    (
                        payload["measurement_kind"] is None
                        or v["measurement_kind"] == payload["measurement_kind"]
                    )
                    and (payload["side"] is None or v["side"] == payload["side"])
                    for v in session["values"]
                ):
                    continue
                selected.append(session)
            else:
                for value in session["values"]:
                    if (
                        value["measurement_kind"] != payload["measurement_kind"]
                        or value["side"] != payload["side"]
                    ):
                        continue
                    if value["normalization_status"] != "verified_v1":
                        continue
                    if value["method_id"] != method_id:
                        continue
                    selected.append(
                        {
                            "session_id": session["session_id"],
                            "measurement_value_id": value["measurement_value_id"],
                            "measured_on": session["measured_on"],
                            "measured_time": session["measured_time"],
                            **{
                                key: value[key]
                                for key in (
                                    "original_value",
                                    "original_unit",
                                    "normalized_value",
                                    "normalized_unit",
                                    "conversion_version",
                                    "side",
                                    "method_description",
                                    "method_id",
                                )
                            },
                        }
                    )
        return self._page(selected, payload, "items" if mode == "sessions" else "points")

    @staticmethod
    def _page(items: list[dict[str, Any]], payload: dict[str, Any], key: str) -> dict[str, Any]:
        filters = {
            name: value
            for name, value in payload.items()
            if name not in ("context", "cursor", "limit")
        }
        digest = digest_payload(filters)
        remaining = items
        if payload.get("cursor"):
            try:
                decoded = json.loads(base64.urlsafe_b64decode(payload["cursor"] + "=="))
                if decoded["digest"] != digest:
                    raise ValueError
                position = (
                    date.fromisoformat(decoded["measured_on"]),
                    time.fromisoformat(decoded["measured_time"])
                    if decoded["measured_time"] is not None
                    else None,
                    UUID(decoded["session_id"]),
                )
                remaining = [
                    item for item in items if MeasurementRepository._after_position(item, position)
                ]
            except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
                raise ContextRepositoryError(
                    "validation_error", "invalid measurement cursor"
                ) from None
        limit = payload["limit"]
        page = remaining[:limit]
        cursor = None
        if len(remaining) > limit and page:
            last = page[-1]
            cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "digest": digest,
                            "measured_on": last["measured_on"],
                            "measured_time": last["measured_time"],
                            "session_id": last["session_id"],
                        }
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
        return {key: page, "next_cursor": cursor}

    @staticmethod
    def _after_position(item: dict[str, Any], position: tuple[date, time | None, UUID]) -> bool:
        item_day = date.fromisoformat(item["measured_on"])
        cursor_day, cursor_clock, cursor_id = position
        if item_day != cursor_day:
            return item_day < cursor_day
        item_clock = time.fromisoformat(item["measured_time"]) if item["measured_time"] else None
        if item_clock != cursor_clock:
            if item_clock is None:
                return True
            if cursor_clock is None:
                return False
            return item_clock < cursor_clock
        return UUID(item["session_id"]).int < cursor_id.int

    def _bmi(self, profile_id: str) -> dict[str, Any]:
        sources = []
        for kind in (MASS, HEIGHT):
            candidates = [
                dict(row)
                for row in self.session.execute(
                    text(
                        "SELECT s.measurement_session_id,s.measured_on,s.measured_time,s.confirmation_id,v.* "
                        "FROM trainer.body_measurement_heads h JOIN trainer.body_measurement_sessions s "
                        "ON s.measurement_session_id=h.current_session_id JOIN trainer.body_measurement_values v "
                        "ON v.measurement_session_id=s.measurement_session_id "
                        "WHERE h.profile_id=:profile AND v.measurement_kind=:kind ORDER BY s.measured_on DESC"
                    ),
                    {"profile": profile_id, "kind": kind},
                ).mappings()
            ]
            if not candidates:
                raise ContextRepositoryError(
                    "insufficient_data", "weight or height unavailable", 422
                )
            row = select_latest_candidate(candidates)
            if row is None:
                raise ContextRepositoryError(
                    "insufficient_data", "latest measurement time ambiguous", 422
                )
            if row["conversion_version"] != "body_units_v1" or row["normalized_value"] is None:
                raise ContextRepositoryError(
                    "insufficient_data", "latest measurement not verified", 422
                )
            if (
                row["normalized_unit"] != ("kilogram" if kind == MASS else "centimeter")
                or row["normalized_value"] <= 0
            ):
                raise ContextRepositoryError(
                    "inconsistent_domain_state", "verified BMI source normalization is invalid", 500
                )
            sources.append(
                {
                    "session_id": str(row["measurement_session_id"]),
                    "measurement_value_id": str(row["measurement_value_id"]),
                    "confirmation_id": str(row["confirmation_id"]),
                    "measured_on": row["measured_on"].isoformat(),
                    "measured_time": row["measured_time"].isoformat()
                    if row["measured_time"]
                    else None,
                    "original_value": _decimal(row["original_value"]),
                    "original_unit": row["original_unit"],
                    "normalized_value": _decimal(row["normalized_value"]),
                    "normalized_unit": row["normalized_unit"],
                    "conversion_version": "body_units_v1",
                }
            )
        value = calculate_bmi(sources[0]["normalized_value"], sources[1]["normalized_value"])
        if value is None:
            raise ContextRepositoryError(
                "unsupported_combination", "BMI cannot be represented", 422
            )
        return {
            "value": value,
            "unit_code": "kg_per_m2",
            "method": "bmi_latest_confirmed_height_v1",
            "body_mass": sources[0],
            "height": sources[1],
        }
