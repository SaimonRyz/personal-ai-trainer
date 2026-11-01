# ruff: noqa: E501
"""Local evidence workflows and exact historical explanations.

The executor owns each transaction. No source URL is opened by this module.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from trainer.domain.context import digest_payload
from trainer.domain.evidence import validate_protocol, validate_recommendation_basis
from trainer.domain.program import assess_applicability
from trainer.infrastructure.db.context_repository import (
    ContextDatabaseExecutor,
    ContextRepository,
    ContextRepositoryError,
)
from trainer.infrastructure.db.progress import ProgressAwareExecutor


class EvidenceRepository(ContextRepository):
    def __init__(self, session: Any, *, owner_verifier: bool = False) -> None:
        super().__init__(session)
        self.owner_verifier = owner_verifier

    def _lock(self) -> None:
        # All evidence writers and recommendation confirmation share this lock.
        self.session.execute(text("SELECT pg_advisory_xact_lock(741852964)"))

    def _require_owner(self) -> None:
        if not self.owner_verifier:
            raise ContextRepositoryError(
                "capability_required", "owner_verifier credential required", 403
            )

    def _profile(self) -> str:
        rows = list(
            self.session.execute(
                text(
                    "SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL ORDER BY profile_id LIMIT 2"
                )
            ).scalars()
        )
        if len(rows) != 1:
            raise ContextRepositoryError("not_found", "single active owner profile required", 404)
        return str(rows[0])

    def _knowledge_snapshot(self, version_id: UUID, effective: datetime) -> dict[str, Any] | None:
        # Caller-provided past dates never restore eligibility for a new decision.
        self._lock()
        now = datetime.now(UTC)
        snapshot = super()._knowledge_snapshot(version_id, now)
        if snapshot is None:
            return None
        snapshot["verification"] = self._verification_snapshot(
            snapshot["status"]["status_revision_id"]
        )
        snapshot["sources"] = [
            self._json_row(r)
            for r in self.session.execute(
                text(
                    "SELECT l.source_id,l.relation,l.cited_locator,l.source_metadata_snapshot, "
                    "s.availability AS current_availability,s.last_checked_at "
                    "FROM trainer.conclusion_source_links l "
                    "JOIN trainer.scientific_sources s USING(source_id) "
                    "WHERE l.conclusion_version_id=:id "
                    "ORDER BY l.source_id,l.relation"
                ),
                {"id": version_id},
            ).mappings()
        ]
        snapshot["caveats"] = [
            self._json_row(r)
            for r in self.session.execute(
                text(
                    "SELECT caveat_type,description,related_conclusion_version_id,related_source_id "
                    "FROM trainer.knowledge_caveats WHERE conclusion_version_id=:id ORDER BY caveat_id"
                ),
                {"id": version_id},
            ).mappings()
        ]
        if (
            snapshot["status"]["status"] == "verified"
            and snapshot["version"]["review_due_on"] < now.date().isoformat()
        ):
            snapshot["effective_status"] = "outdated"
        else:
            snapshot["effective_status"] = snapshot["status"]["status"]
        return snapshot

    def _verification_snapshot(self, status_revision_id: Any) -> dict[str, Any] | None:
        row = (
            self.session.execute(
                text(
                    "SELECT p.protocol_run_id,p.protocol_version,p.performed_at,p.reviewer_profile_id,p.decision "
                    "FROM trainer.knowledge_status_revisions s JOIN trainer.verification_protocol_runs p USING(protocol_run_id) "
                    "WHERE s.status_revision_id=:id"
                ),
                {"id": status_revision_id},
            )
            .mappings()
            .one_or_none()
        )
        return self._json_row(row) if row is not None else None

    def _materialize_plan_recommendations(
        self, body: dict[str, Any], dependencies: list[dict[str, Any]], effective_types: set[str]
    ) -> None:
        recs = body["recommendations"]
        if not recs:
            raise ContextRepositoryError(
                "validation_error", "new plan requires at least one explanation", 422
            )
        profile = self.session.execute(
            text(
                "SELECT to_jsonb(r) FROM trainer.profile_revisions r WHERE profile_revision_id=:id"
            ),
            {"id": body["context_snapshot"]["profile_ref"]["revision_id"]},
        ).scalar_one()
        fact_tables = {
            "profile": ("profile_revisions", "profile_revision_id", "profile_id", "profiles"),
            "goal": ("goal_revisions", "goal_revision_id", "goal_id", "goals"),
            "exercise": ("exercise_revisions", "exercise_revision_id", "exercise_id", "exercises"),
            "training_place": (
                "training_place_revisions",
                "place_revision_id",
                "place_id",
                "training_places",
            ),
            "equipment_item": (
                "equipment_item_revisions",
                "equipment_revision_id",
                "equipment_id",
                "equipment_items",
            ),
            "program_version": ("program_versions", "program_version_id", "program_id", "programs"),
        }
        for rec in recs:
            if not self.session.execute(
                text(
                    "SELECT 1 FROM trainer.recommendation_kind_catalog WHERE code=:code AND archived_at IS NULL"
                ),
                {"code": rec["recommendation_kind"]},
            ).first():
                raise ContextRepositoryError("validation_error", "unknown recommendation kind", 422)
            rec["recommendation_id"] = str(uuid4())
            facts = {
                **profile,
                "profile_id": body["profile_id"],
                "effective_equipment_types": sorted(effective_types),
                "program_id": body["plan"].get("program_id"),
                "program_version_id": body["plan"].get("program_version_id"),
            }
            goal_types = set()
            for link in rec["fact_links"]:
                ref = link["ref"]
                spec = fact_tables.get(ref["object_type"])
                if spec is None or ref.get("revision_id") is None:
                    raise ContextRepositoryError(
                        "validation_error", "unsupported or inexact recommendation fact", 422
                    )
                table, revision, identity, aggregate = spec
                ownership = "" if ref["object_type"] == "exercise" else " AND a.profile_id=:profile"
                if ref["object_type"] == "profile":
                    ownership = " AND a.profile_id=:profile"
                row = self.session.execute(
                    text(
                        f"SELECT to_jsonb(r) FROM trainer.{table} r JOIN trainer.{aggregate} a USING({identity}) WHERE r.{revision}=:revision AND r.{identity}=:object{ownership}"
                    ),
                    {
                        "revision": ref["revision_id"],
                        "object": ref["object_id"],
                        "profile": body["profile_id"],
                    },
                ).scalar_one_or_none()
                if row is None:
                    raise ContextRepositoryError("not_found", "recommendation fact not found", 404)
                digest = digest_payload(row)
                if digest != link["snapshot_digest"]:
                    raise ContextRepositoryError(
                        "validation_error", "recommendation fact digest mismatch", 422
                    )
                dependencies.append(
                    {
                        "dependency_type": ref["object_type"],
                        "object_id": ref["object_id"],
                        "revision_id": ref["revision_id"],
                        "digest": digest,
                        "reason": "exact recommendation fact",
                    }
                )
                if ref["object_type"] == "goal":
                    goal_types.add(row["goal_type"])
            # Explicit exact goal links are the only source of goal-specific applicability.
            if len(goal_types) == 1:
                facts["goal_type"] = next(iter(goal_types))
            for link in rec["calculation_links"]:
                value = self.session.execute(
                    text(
                        "SELECT to_jsonb(v) FROM trainer.derived_values v WHERE derived_value_id=:id AND profile_id=:profile AND state='current'"
                    ),
                    {"id": link["derived_value_id"], "profile": body["profile_id"]},
                ).scalar_one_or_none()
                if value is None:
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
            for link in rec["conclusion_links"]:
                version_id = UUID(link["conclusion_version_id"])
                snapshot = self._knowledge_snapshot(version_id, datetime.now(UTC))
                if snapshot is None:
                    raise ContextRepositoryError(
                        "not_found", "recommendation conclusion not found", 404
                    )
                status = snapshot["status"]
                assessment = assess_applicability(snapshot["scopes"], facts)
                assessment["eligible"] = bool(
                    snapshot["effective_status"] == "verified"
                    and snapshot["is_current"]
                    and snapshot["version"]["confidence"] != "insufficient"
                    and assessment["outcome"] == "applicable"
                )
                link["status_at_decision"] = snapshot["effective_status"]
                link["applicability_assessment"] = assessment
                link["status_revision_ref"] = self._ref(
                    "knowledge_status_revision",
                    UUID(status["status_revision_id"]),
                    UUID(status["status_revision_id"]),
                )
                dependencies.append(
                    {
                        "dependency_type": "conclusion_version",
                        "object_id": str(version_id),
                        "revision_id": status["status_revision_id"],
                        "digest": digest_payload(snapshot),
                        "reason": "current recommendation conclusion status and applicability",
                    }
                )
                if link["role"] == "supports" and not assessment["eligible"]:
                    raise ContextRepositoryError(
                        "validation_error",
                        "noneligible knowledge cannot be used as verified plan support",
                        422,
                    )
            validate_recommendation_basis(rec)
            self._check_confidence(rec)
        body["evidence_basis"] = self._preview_basis(recs)

    def _materialize_program_evidence(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        gaps = super()._materialize_program_evidence(body)
        recs = body["version"]["recommendations"]
        for rec in recs:
            validate_recommendation_basis(rec)
            self._check_confidence(rec)
        body["evidence_basis"] = self._preview_basis(recs)
        return [
            gap
            for gap in gaps
            if not (
                gap["message_safe"]
                == "significant program design lacks applicable verified support"
                and any(
                    gap["path"] == f"version.recommendations[{i}].conclusion_links"
                    and rec["confidence"] == "insufficient"
                    for i, rec in enumerate(recs)
                )
            )
        ]

    def _preview_basis(self, recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "recommendation_id": rec["recommendation_id"],
                "confidence": rec["confidence"],
                "limitation": "Нет достаточной применимой проверенной научной опоры; решение основано на перечисленных допущениях."
                if rec["confidence"] == "insufficient"
                else None,
                "conclusions": [
                    {
                        "role": link["role"],
                        "snapshot": self._knowledge_snapshot(
                            UUID(link["conclusion_version_id"]), datetime.now(UTC)
                        ),
                    }
                    for link in rec["conclusion_links"]
                ],
            }
            for rec in recs
        ]

    def _check_confidence(self, rec: dict[str, Any]) -> None:
        ranks = {"insufficient": 0, "low": 1, "moderate": 2, "high": 3}
        for link in rec["conclusion_links"]:
            snap = self._knowledge_snapshot(UUID(link["conclusion_version_id"]), datetime.now(UTC))
            if snap is None:
                raise ContextRepositoryError("not_found", "conclusion not found", 404)
            if link["role"] == "supports" and (
                snap["effective_status"] != "verified"
                or snap["version"]["confidence"] == "insufficient"
            ):
                raise ContextRepositoryError(
                    "validation_error", "ineligible scientific support", 422
                )
            if (
                link["role"] == "supports"
                and ranks[rec["confidence"]] > ranks[snap["version"]["confidence"]]
            ):
                raise ContextRepositoryError(
                    "validation_error", "recommendation confidence exceeds scientific basis", 422
                )
            if (
                any(c["caveat_type"] == "conflict" for c in snap["caveats"])
                or any(s["relation"] == "contradicts" for s in snap["sources"])
            ) and ranks[rec["confidence"]] > 1:
                raise ContextRepositoryError(
                    "validation_error", "conflict requires reduced confidence", 422
                )

    def create_evidence_draft(self, **kwargs: Any) -> dict[str, Any]:
        payload = kwargs["payload"]
        replay = self._replay_draft("CMD-14", kwargs)
        if replay is not None:
            return replay
        self._lock()
        if "source_availability_update" in payload:
            update = payload["source_availability_update"]
            reason = str(update.get("reason", "")).strip()
            if not reason:
                raise ContextRepositoryError("validation_error", "reason cannot be blank", 422)
            source = self.session.execute(
                text("SELECT to_jsonb(s) FROM trainer.scientific_sources s WHERE source_id=:id AND archived_at IS NULL"),
                {"id": update["source_id"]},
            ).scalar_one_or_none()
            if source is None:
                raise ContextRepositoryError("not_found", "scientific source not found", 404)
            checked = source.get("last_checked_at")
            expected = update.get("expected_last_checked_at")
            if source["availability"] != update["expected_availability"] or (
                (checked is None) != (expected is None)
                or (checked is not None and expected is not None and datetime.fromisoformat(str(checked).replace("Z", "+00:00")) != datetime.fromisoformat(str(expected).replace("Z", "+00:00")))
            ):
                raise ContextRepositoryError("stale_preview", "source availability changed", 409)
            if not source.get("url"):
                raise ContextRepositoryError("validation_error", "source has no checked URL", 422)
            checked_at = datetime.now(UTC).isoformat()
            typed = {
                "evidence_change": "source_availability",
                "owner_profile_id": self._profile(),
                "source_availability_update": {
                    **update,
                    "reason": reason,
                    "checked_at": checked_at,
                    "source": source,
                },
                "change_plan": [
                    self._ref("scientific_source", UUID(update["source_id"]), None)
                ],
            }
            return self._draft(typed, "CMD-14", "evidence", "normal", kwargs)
        conclusion = payload["evidence"]["conclusion"]
        for field in ("topic", "statement", "population_conditions"):
            if not conclusion[field].strip():
                raise ContextRepositoryError(
                    "validation_error", "conclusion text cannot be blank", 422
                )
        self._validate_children(conclusion)
        previous = None
        conclusion_id = conclusion.get("conclusion_id") or str(uuid4())
        if conclusion.get("conclusion_id"):
            previous = (
                self.session.execute(
                    text(
                        "SELECT current_version_id,topic FROM trainer.conclusions WHERE conclusion_id=:id AND archived_at IS NULL"
                    ),
                    {"id": conclusion_id},
                )
                .mappings()
                .one_or_none()
            )
            if previous is None:
                raise ContextRepositoryError("not_found", "conclusion not found", 404)
            if previous["topic"] != conclusion["topic"]:
                raise ContextRepositoryError("conflict", "conclusion topic cannot change", 409)
        sources: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        for link in payload["evidence"]["sources"]:
            source = link["source"]
            for table, code in (
                ("scientific_source_type_catalog", source["source_type"]),
                ("scientific_source_availability_catalog", source["availability"]),
            ):
                if (
                    self.session.execute(
                        text(
                            f"SELECT 1 FROM trainer.{table} WHERE code=:code AND archived_at IS NULL"
                        ),
                        {"code": code},
                    ).first()
                    is None
                ):
                    raise ContextRepositoryError(
                        "validation_error", "unknown source vocabulary", 422
                    )
            row = self.session.execute(
                text(
                    "SELECT to_jsonb(s) FROM trainer.scientific_sources s WHERE citation_key=:key"
                ),
                {"key": source["citation_key"]},
            ).scalar_one_or_none()
            immutable_source_fields = set(source) - {"availability"}
            if row and any(row[key] != source[key] for key in immutable_source_fields):
                raise ContextRepositoryError(
                    "conflict", "citation metadata differs; existing source is immutable", 409
                )
            if source["citation_key"] in seen and seen[source["citation_key"]] != source:
                raise ContextRepositoryError(
                    "conflict", "same citation has different metadata", 409
                )
            seen[source["citation_key"]] = source
            source_id = (
                str(row["source_id"])
                if row
                else next(
                    (
                        s["source_id"]
                        for s in sources
                        if s["source"]["citation_key"] == source["citation_key"]
                    ),
                    str(uuid4()),
                )
            )
            sources.append({**link, "source_id": source_id, "existing": row is not None})
        version = str(uuid4())
        prior_id = str(previous["current_version_id"]) if previous else None
        typed = {
            "evidence_change": "content",
            "owner_profile_id": self._profile(),
            "conclusion_id": conclusion_id,
            "version_id": version,
            "previous_version_id": prior_id,
            "conclusion": conclusion,
            "sources": sources,
            "status_id": str(uuid4()),
            "change_plan": [self._ref("conclusion", UUID(conclusion_id), UUID(version))]
            + [
                self._ref("scientific_source", UUID(source_id), None)
                for source_id in sorted({s["source_id"] for s in sources if not s["existing"]})
            ],
        }
        typed["change_plan"].append(
            self._ref(
                "knowledge_status_revision", UUID(typed["status_id"]), UUID(typed["status_id"])
            )
        )
        return self._draft(typed, "CMD-14", "evidence", "normal", kwargs)

    @staticmethod
    def _validate_children(conclusion: dict[str, Any]) -> None:
        scope_fields = {
            "population",
            "goal",
            "activity_or_load",
            "setting",
            "inclusion",
            "exclusion",
            "readable_form",
        }
        caveat_fields = {
            "caveat_type",
            "description",
            "related_conclusion_version_id",
            "related_source_id",
        }
        for scope in conclusion["applicability"]:
            if (
                set(scope) - scope_fields
                or not all(
                    isinstance(scope.get(k), str) and scope[k].strip()
                    for k in ("population", "readable_form")
                )
                or not all(isinstance(scope.get(k), dict) for k in ("inclusion", "exclusion"))
            ):
                raise ContextRepositoryError("validation_error", "invalid applicability scope", 422)
        for caveat in conclusion["caveats"]:
            if (
                set(caveat) - caveat_fields
                or caveat.get("caveat_type") not in ("limitation", "conflict")
                or not str(caveat.get("description", "")).strip()
            ):
                raise ContextRepositoryError("validation_error", "invalid knowledge caveat", 422)
            for key in ("related_conclusion_version_id", "related_source_id"):
                if caveat.get(key) is not None:
                    try:
                        UUID(caveat[key])
                    except (ValueError, TypeError) as exc:
                        raise ContextRepositoryError(
                            "validation_error", "invalid caveat reference", 422
                        ) from exc

    def create_verification_draft(self, **kwargs: Any) -> dict[str, Any]:
        self._require_owner()
        replay = self._replay_draft("CMD-15", kwargs)
        if replay is not None:
            return replay
        self._lock()
        payload = kwargs["payload"]
        validate_protocol(payload)
        snapshot = self._knowledge_snapshot(
            UUID(payload["conclusion_version_id"]), datetime.now(UTC)
        )
        if snapshot is None or not snapshot["is_current"]:
            raise ContextRepositoryError("not_found", "current conclusion version not found", 404)
        if payload["decision"] == "verified":
            due = date.fromisoformat(snapshot["version"]["review_due_on"])
            if (
                not datetime.now(UTC).date()
                <= due
                <= datetime.now(UTC).date() + timedelta(days=365)
            ):
                raise ContextRepositoryError(
                    "validation_error", "review date must be within twelve months", 422
                )
            if not snapshot["scopes"] or not snapshot["sources"] or not snapshot["caveats"]:
                raise ContextRepositoryError(
                    "validation_error",
                    "verification requires sources, applicability and limitations",
                    422,
                )
            if any(
                s["source_metadata_snapshot"].get("availability") != "available"
                for s in snapshot["sources"]
            ):
                raise ContextRepositoryError(
                    "validation_error", "verification requires available full-text sources", 422
                )
            if (
                any(s["relation"] == "contradicts" for s in snapshot["sources"])
                or any(c["caveat_type"] == "conflict" for c in snapshot["caveats"])
            ) and snapshot["version"]["confidence"] not in ("low", "insufficient"):
                raise ContextRepositoryError(
                    "validation_error", "conflicting conclusion requires reduced confidence", 422
                )
        status_id, run_id = str(uuid4()), str(uuid4())
        typed = {
            "evidence_change": "verification",
            "owner_profile_id": self._profile(),
            "request": payload,
            "snapshot": snapshot,
            "version_id": payload["conclusion_version_id"],
            "status_id": status_id,
            "run_id": run_id,
            "change_plan": [
                self._ref("knowledge_status_revision", UUID(status_id), UUID(status_id)),
                self._ref("verification_protocol_run", UUID(run_id), UUID(run_id)),
            ],
        }
        return self._draft(typed, "CMD-15", "conclusion_verification", "owner_verification", kwargs)

    def _replay_draft(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        operation, replay = self._reserve(
            method, kwargs["key"], kwargs["payload"]["request_digest"]
        )
        kwargs["_reserved_operation"] = operation
        if replay is not None:
            self._audit_replay(
                operation,
                method,
                kwargs["correlation_id"],
                kwargs.get("causation_id"),
                kwargs["key"],
                replay,
            )
        return replay

    def _draft(
        self, typed: dict[str, Any], method: str, intent: str, kind: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        operation = kwargs["_reserved_operation"]
        draft, revision, now = uuid4(), uuid4(), datetime.now(UTC)
        digest = digest_payload(typed)
        self.session.execute(
            text(
                "INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at) "
                "VALUES (:draft,:intent,:profile,'open',:revision,:expires,:now)"
            ),
            {
                "draft": draft,
                "intent": intent,
                "profile": typed["owner_profile_id"],
                "revision": revision,
                "expires": now + timedelta(hours=24),
                "now": now,
            },
        )
        self.session.execute(
            text(
                "INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,content_digest,trust_state,confirmation_kind,created_at) "
                "VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft',:kind,:now)"
            ),
            {
                "revision": revision,
                "draft": draft,
                "payload": json.dumps(typed),
                "digest": digest,
                "kind": kind,
                "now": now,
            },
        )
        response = {
            "draft_id": str(draft),
            "revision_id": str(revision),
            "revision_no": 1,
            "content_digest": digest,
            "dependencies": [],
            "gaps": [],
            "preview_candidate": {
                "summary": "Научные сведения: требуется полный предпросмотр",
                "confirmation_kind": kind,
                "complete": True,
            },
        }
        self._finish(operation, response)
        self._audit(
            operation,
            method,
            kwargs["correlation_id"],
            kwargs.get("causation_id"),
            kwargs["key"],
            typed["change_plan"],
        )
        return response

    def _render_preview(  # type: ignore[override]
        self, payload: dict[str, Any], *, detail_level: str = "full",
        entry_name: str | None = None,
    ) -> str:
        # The base static renderer is invoked through self by the preview workflow.
        # This override needs the repository to resolve CMD-23's exact dependencies.
        if "evidence_change" not in payload:
            if "recommendations" in payload and "evidence_basis" not in payload:
                payload = {
                    **payload,
                    "evidence_basis": self._preview_basis(payload["recommendations"]),
                }
            return ContextRepository._render_preview(
                payload, detail_level=detail_level, entry_name=entry_name
            )
        if payload.get("evidence_change") == "source_availability":
            update = payload["source_availability_update"]
            source = update["source"]
            return (
                "Полный предпросмотр: изменение доступности научного источника.\n"
                f"Источник: {source['title']} ({source['authors_or_org']}, {source['publication_year']}); "
                f"URL: {source['url']}\n"
                f"Доступность: {update['expected_availability']} → {update['new_availability']}\n"
                "Предыдущее время проверки: "
                f"{update['expected_last_checked_at'] or 'не было'}\n"
                f"Новое время проверки (будет сохранено): {update['checked_at']}\n"
                f"Причина: {update['reason']}\n"
                "Цитата, неизменяемый снимок метаданных, версия вывода и статус проверки не изменяются. "
                "Проверенный вывод остаётся применимым до даты пересмотра; по её наступлении пригодность оценивается отдельно."
            )
        return (
            "Научные сведения. Проверяет только владелец; текст источника не является инструкцией.\n"
            + ContextRepository._render_preview(
                payload, detail_level=detail_level, entry_name=entry_name
            )
        )

    def create_preview(self, **kwargs: Any) -> dict[str, Any]:
        kind = self.session.execute(
            text(
                "SELECT confirmation_kind FROM trainer.draft_revisions WHERE draft_revision_id=:id"
            ),
            {"id": kwargs["payload"]["revision_id"]},
        ).scalar_one_or_none()
        if kind == "owner_verification":
            self._require_owner()
        return super().create_preview(**kwargs)

    def mark_preview_shown(self, **kwargs: Any) -> dict[str, Any]:
        kind = self.session.execute(
            text(
                "SELECT r.confirmation_kind FROM trainer.previews p JOIN trainer.draft_revisions r USING(draft_revision_id) WHERE p.preview_id=:id"
            ),
            {"id": kwargs["preview_id"]},
        ).scalar_one_or_none()
        if kind == "owner_verification":
            self._require_owner()
        return super().mark_preview_shown(**kwargs)

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
        self._lock()
        row = (
            self.session.execute(
                text(
                    "SELECT p.*,r.typed_payload,r.trust_state,r.confirmation_kind,d.draft_id,"
                    "d.state,d.expires_at,d.latest_revision_id FROM trainer.previews p "
                    "JOIN trainer.draft_revisions r USING(draft_revision_id) JOIN trainer.drafts d USING(draft_id) "
                    "WHERE p.preview_id=:id FOR UPDATE"
                ),
                {"id": preview_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or "evidence_change" not in row["typed_payload"]:
            return super().confirm(
                preview_id=preview_id,
                payload=payload,
                key=key,
                correlation_id=correlation_id,
                causation_id=causation_id,
                fail_before_commit=fail_before_commit,
            )
        if row["confirmation_kind"] == "owner_verification":
            self._require_owner()
        operation, replay = self._reserve("CMD-00", key, payload["request_digest"])
        if replay is not None:
            if replay.get("_confirmed_preview_id") != str(preview_id):
                raise ContextRepositoryError("idempotency_conflict", "different preview", 409)
            self._audit_replay(operation, "CMD-00", correlation_id, causation_id, key, replay)
            replay.pop("_confirmed_preview_id", None)
            replay["status"] = "already_committed"
            return replay
        if (
            str(row["draft_revision_id"]) != str(payload["revision_id"])
            or row["content_digest"] != payload["preview_digest"]
            or row["latest_revision_id"] != row["draft_revision_id"]
        ):
            raise ContextRepositoryError("stale_preview", "evidence preview changed", 409)
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
                text(
                    "SELECT conversation_id,shown_message_id,cross_dialog FROM trainer.preview_show_events "
                    "WHERE preview_id=:preview AND conversation_id=:conversation ORDER BY shown_at DESC,show_event_id DESC LIMIT 1"
                ),
                {"preview": preview_id, "conversation": payload["context"]["conversation_id"]},
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
        if (
            not valid
            or payload["confirmation_message_id"] != payload["context"]["message_id"]
            or not payload["explicit_intent"].strip()
        ):
            raise ContextRepositoryError(
                "ambiguous_confirmation", "preview was not shown here", 400
            )
        typed = dict(row["typed_payload"])
        self._check_evidence_fresh(typed)
        confirmation, result = uuid4(), uuid4()
        changes = typed["change_plan"]
        response = {
            "commit_result_id": str(result),
            "confirmation_id": str(confirmation),
            "operation_id": str(operation),
            "status": "committed",
            "domain_changes": changes,
            "created_revisions": [item for item in changes if item.get("revision_id")],
            "invalidated_calculations": [],
            "committed_at": now.isoformat(),
        }
        self.session.execute(
            text(
                "INSERT INTO trainer.confirmations(confirmation_id,preview_id,draft_revision_id,conversation_id,user_message_id,kind,explicit_expression,confirmed_at,created_at) "
                "VALUES (:id,:preview,:revision,:conversation,:message,:kind,:expression,:now,:now)"
            ),
            {
                "id": confirmation,
                "preview": preview_id,
                "revision": row["draft_revision_id"],
                "conversation": payload["context"]["conversation_id"],
                "message": payload["confirmation_message_id"],
                "kind": row["confirmation_kind"],
                "expression": payload["explicit_intent"],
                "now": now,
            },
        )
        self.session.execute(
            text(
                "INSERT INTO trainer.commit_results(commit_result_id,confirmation_id,operation_id,result_payload,committed_at,created_at) VALUES (:id,:confirmation,:operation,CAST(:payload AS jsonb),:now,:now)"
            ),
            {
                "id": result,
                "confirmation": confirmation,
                "operation": operation,
                "payload": json.dumps(response),
                "now": now,
            },
        )
        self._persist_evidence(typed, confirmation, now)
        for change in changes:
            change_snapshot = typed.get("snapshot")
            change_reason = typed.get("request", {}).get("reason")
            if typed["evidence_change"] == "source_availability":
                availability_result = typed.get("availability_result", {})
                change_snapshot = availability_result.get("before")
                change_after = availability_result.get("after", typed)
                change_reason = typed["source_availability_update"]["reason"]
            else:
                change_after = typed
            self._insert_change(
                change,
                result,
                "create" if typed["evidence_change"] == "content" else "correct",
                change_snapshot,
                change_after,
                change_reason,
                now,
            )
        self.session.execute(
            text("UPDATE trainer.drafts SET state='confirmed' WHERE draft_id=:id"),
            {"id": row["draft_id"]},
        )
        self.session.execute(
            text(
                "UPDATE trainer.idempotent_operations SET state='committed',commit_result_id=:result,response_payload=CAST(:payload AS jsonb),updated_at=:now WHERE operation_id=:operation"
            ),
            {
                "result": result,
                "payload": json.dumps({**response, "_confirmed_preview_id": str(preview_id)}),
                "now": now,
                "operation": operation,
            },
        )
        self._audit(operation, "CMD-00", correlation_id, causation_id, key, changes)
        if fail_before_commit:
            raise RuntimeError("synthetic fault before transaction commit")
        return response

    def _check_evidence_fresh(self, typed: dict[str, Any]) -> None:
        if self._profile() != typed["owner_profile_id"]:
            raise ContextRepositoryError("stale_preview", "owner changed", 409)
        if typed["evidence_change"] == "verification":
            snapshot = self._knowledge_snapshot(UUID(typed["version_id"]), datetime.now(UTC))
            if snapshot is None or snapshot != typed["snapshot"]:
                raise ContextRepositoryError(
                    "stale_preview", "conclusion status or content changed", 409
                )
            validate_protocol(typed["request"])
            if typed["request"]["decision"] == "verified":
                due = date.fromisoformat(snapshot["version"]["review_due_on"])
                today = datetime.now(UTC).date()
                if not today <= due <= today + timedelta(days=365):
                    raise ContextRepositoryError("stale_preview", "review date expired", 409)
            return
        if typed["evidence_change"] == "source_availability":
            update = typed["source_availability_update"]
            current = self.session.execute(
                text("SELECT availability,last_checked_at,url FROM trainer.scientific_sources WHERE source_id=:id AND archived_at IS NULL"),
                {"id": update["source_id"]},
            ).mappings().one_or_none()
            if (
                current is None
                or current["availability"] != update["expected_availability"]
                or current["url"] != update["source"].get("url")
            ):
                raise ContextRepositoryError("stale_preview", "source identity or availability changed", 409)
            expected = update.get("expected_last_checked_at")
            checked = current["last_checked_at"]
            if (checked is None) != (expected is None) or (
                checked is not None
                and checked.isoformat()
                != datetime.fromisoformat(str(expected).replace("Z", "+00:00")).isoformat()
            ):
                raise ContextRepositoryError("stale_preview", "source check timestamp changed", 409)
            return
        current = self.session.execute(
            text("SELECT current_version_id FROM trainer.conclusions WHERE conclusion_id=:id"),
            {"id": typed["conclusion_id"]},
        ).scalar_one_or_none()
        if (str(current) if current else None) != typed["previous_version_id"]:
            raise ContextRepositoryError("stale_preview", "conclusion version changed", 409)
        for link in typed["sources"]:
            row = self.session.execute(
                text(
                    "SELECT to_jsonb(s) FROM trainer.scientific_sources s WHERE citation_key=:key"
                ),
                {"key": link["source"]["citation_key"]},
            ).scalar_one_or_none()
            if (row is not None) != link["existing"] or (
                row
                and (
                    str(row["source_id"]) != link["source_id"]
                    or any(row[k] != v for k, v in link["source"].items() if k != "availability")
                )
            ):
                raise ContextRepositoryError(
                    "stale_preview", "source identity or metadata changed", 409
                )

    def _persist_evidence(self, typed: dict[str, Any], confirmation: UUID, now: datetime) -> None:
        if typed["evidence_change"] == "source_availability":
            update = typed["source_availability_update"]
            result = self.session.execute(
                text("SELECT trainer.apply_source_availability_update(:source,:expected_availability,:expected_checked,:availability,:confirmation)"),
                {
                    "source": update["source_id"],
                    "expected_availability": update["expected_availability"],
                    "expected_checked": update["expected_last_checked_at"],
                    "availability": update["new_availability"],
                    "confirmation": confirmation,
                },
            ).scalar_one()
            typed["availability_result"] = result
            return
        version = typed["version_id"]
        if typed["evidence_change"] == "verification":
            request = typed["request"]
            self.session.execute(
                text(
                    "INSERT INTO trainer.verification_protocol_runs(protocol_run_id,conclusion_version_id,reviewer_profile_id,protocol_version,checklist_result,decision,reason,performed_at,confirmation_id,created_at) "
                    "VALUES (:id,:version,:profile,:protocol,CAST(:checklist AS jsonb),:decision,:reason,:now,:confirmation,:now)"
                ),
                {
                    "id": typed["run_id"],
                    "version": version,
                    "profile": typed["owner_profile_id"],
                    "protocol": request["protocol_version"],
                    "checklist": json.dumps(request["checklist"]),
                    "decision": request["decision"],
                    "reason": request["reason"],
                    "now": now,
                    "confirmation": confirmation,
                },
            )
            self._status(
                version,
                typed["status_id"],
                request["decision"],
                request["reason"],
                now,
                typed["run_id"],
            )
            return
        conclusion = typed["conclusion"]
        if typed["previous_version_id"] is None:
            self.session.execute(
                text(
                    "INSERT INTO trainer.conclusions(conclusion_id,topic,created_at) VALUES (:id,:topic,:now)"
                ),
                {"id": typed["conclusion_id"], "topic": conclusion["topic"], "now": now},
            )
        self.session.execute(
            text(
                "INSERT INTO trainer.conclusion_versions(conclusion_version_id,conclusion_id,version_no,statement,population_conditions,confidence,review_due_on,supersedes_version_id,content_digest,created_at) "
                "VALUES (:version,:id,(SELECT coalesce(max(version_no),0)+1 FROM trainer.conclusion_versions WHERE conclusion_id=:id),:statement,:population,:confidence,:due,:prior,:digest,:now)"
            ),
            {
                "version": version,
                "id": typed["conclusion_id"],
                "statement": conclusion["statement"],
                "population": conclusion["population_conditions"],
                "confidence": conclusion["confidence"],
                "due": conclusion["review_due_on"],
                "prior": typed["previous_version_id"],
                "digest": digest_payload({"conclusion": conclusion, "sources": typed["sources"]}),
                "now": now,
            },
        )
        inserted: set[str] = set()
        for link in typed["sources"]:
            source = link["source"]
            if not link["existing"] and link["source_id"] not in inserted:
                self.session.execute(
                    text(
                        "INSERT INTO trainer.scientific_sources(source_id,citation_key,title,authors_or_org,publication_year,source_type,doi,pmid,url,metadata_snapshot,availability,conflict_of_interest_note,created_at) "
                        "VALUES (:id,:citation_key,:title,:authors_or_org,:publication_year,:source_type,:doi,:pmid,:url,CAST(:metadata_snapshot AS jsonb),:availability,:conflict_of_interest_note,:now)"
                    ),
                    {
                        **source,
                        "metadata_snapshot": json.dumps(source["metadata_snapshot"]),
                        "id": link["source_id"],
                        "now": now,
                    },
                )
                inserted.add(link["source_id"])
            self.session.execute(
                text(
                    "INSERT INTO trainer.conclusion_source_links(conclusion_version_id,source_id,relation,note,cited_locator,source_metadata_snapshot,created_at) VALUES (:version,:source,:relation,:note,:locator,CAST(:snapshot AS jsonb),:now)"
                ),
                {
                    "version": version,
                    "source": link["source_id"],
                    "relation": link["relation"],
                    "note": link["note"],
                    "locator": link["cited_locator"],
                    "snapshot": json.dumps(source),
                    "now": now,
                },
            )
        for scope in conclusion["applicability"]:
            self.session.execute(
                text(
                    "INSERT INTO trainer.applicability_scopes(scope_id,conclusion_version_id,population,goal,activity_or_load,setting,inclusion,exclusion,readable_form,created_at) "
                    "VALUES (:id,:version,:population,:goal,:activity_or_load,:setting,CAST(:inclusion AS jsonb),CAST(:exclusion AS jsonb),:readable_form,:now)"
                ),
                {
                    "goal": None,
                    "activity_or_load": None,
                    "setting": None,
                    **scope,
                    "id": uuid4(),
                    "version": version,
                    "inclusion": json.dumps(scope["inclusion"]),
                    "exclusion": json.dumps(scope["exclusion"]),
                    "now": now,
                },
            )
        for caveat in conclusion["caveats"]:
            self.session.execute(
                text(
                    "INSERT INTO trainer.knowledge_caveats(caveat_id,conclusion_version_id,caveat_type,description,related_conclusion_version_id,related_source_id,created_at) VALUES (:id,:version,:caveat_type,:description,:related_conclusion_version_id,:related_source_id,:now)"
                ),
                {
                    "related_conclusion_version_id": None,
                    "related_source_id": None,
                    **caveat,
                    "id": uuid4(),
                    "version": version,
                    "now": now,
                },
            )
        self._status(
            version, typed["status_id"], "draft", "New content awaits owner verification", now
        )
        self.session.execute(
            text(
                "UPDATE trainer.conclusions SET current_version_id=:version WHERE conclusion_id=:id"
            ),
            {"version": version, "id": typed["conclusion_id"]},
        )

    def _status(
        self,
        version: str,
        status_id: str,
        status: str,
        reason: str,
        now: datetime,
        run: str | None = None,
    ) -> None:
        self.session.execute(
            text(
                "INSERT INTO trainer.knowledge_status_revisions(status_revision_id,conclusion_version_id,revision_no,status,effective_at,reason,protocol_run_id,created_at) "
                "VALUES (:id,:version,(SELECT coalesce(max(revision_no),0)+1 FROM trainer.knowledge_status_revisions WHERE conclusion_version_id=:version),:status,:now,:reason,:run,:now)"
            ),
            {
                "id": status_id,
                "version": version,
                "status": status,
                "now": now,
                "reason": reason,
                "run": run,
            },
        )

    def query_eligible_conclusions(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        items, conflicts = [], []
        as_of = max(date.fromisoformat(payload["decision_date"]), datetime.now(UTC).date())
        versions = list(
            self.session.execute(
                text(
                    "SELECT current_version_id FROM trainer.conclusions WHERE topic=:topic AND archived_at IS NULL AND current_version_id IS NOT NULL ORDER BY conclusion_id"
                ),
                {"topic": payload["topic"]},
            ).scalars()
        )
        for version in versions:
            snap = self._knowledge_snapshot(version, datetime.now(UTC))
            if snap is None:
                continue
            assessment = assess_applicability(snap["scopes"], payload["applicability_snapshot"])
            if (
                snap["effective_status"] != "verified"
                or snap["version"]["confidence"] == "insufficient"
                or date.fromisoformat(snap["version"]["review_due_on"]) < as_of
                or assessment["outcome"] != "applicable"
            ):
                continue
            assessment["eligible"] = True
            items.append(
                {
                    "conclusion_version_id": str(version),
                    "status": "verified",
                    "confidence": snap["version"]["confidence"],
                    "review_due_on": snap["version"]["review_due_on"],
                    "source_ids": sorted({s["source_id"] for s in snap["sources"]}),
                    "applicability_assessment": assessment,
                }
            )
            if any(c["caveat_type"] == "conflict" for c in snap["caveats"]) or any(
                s["relation"] == "contradicts" for s in snap["sources"]
            ):
                conflicts.append(
                    {
                        "path": str(version),
                        "code": "conflict",
                        "disposition": "assume_with_disclosure",
                        "message_safe": "Применимый вывод содержит противоречащие данные; покажите обе стороны и снизьте уверенность.",
                    }
                )
        return {
            "items": items,
            "conflicts": conflicts,
            "gaps": []
            if items
            else [
                {
                    "path": "conclusions",
                    "code": "missing",
                    "disposition": "leave_unknown",
                    "message_safe": "Применимой проверенной опоры недостаточно: insufficient.",
                }
            ],
        }

    def get_recommendation_explanation(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self._profile()
        rec = (
            self.session.execute(
                text(
                    "SELECT r.* FROM trainer.recommendations r LEFT JOIN trainer.program_versions v USING(program_version_id) "
                    "LEFT JOIN trainer.programs p ON p.program_id=v.program_id LEFT JOIN trainer.workout_plan_revisions w USING(plan_revision_id) "
                    "LEFT JOIN trainer.workout_plans wp ON wp.plan_id=w.plan_id WHERE r.recommendation_id=:id AND (p.profile_id=:profile OR wp.profile_id=:profile)"
                ),
                {"id": payload["recommendation_id"], "profile": profile},
            )
            .mappings()
            .one_or_none()
        )
        if rec is None:
            raise ContextRepositoryError("not_found", "recommendation not found", 404)
        explanations = {
            str(r[0]): str(r[1])
            for r in self.session.execute(
                text(
                    "SELECT detail_level,content FROM trainer.recommendation_explanations WHERE recommendation_id=:id"
                ),
                {"id": rec["recommendation_id"]},
            ).all()
        }
        if not explanations.get("short"):
            raise ContextRepositoryError(
                "inconsistent_domain_state", "saved explanation missing", 500
            )
        facts = [
            self._ref(r["object_type"], r["object_id"], r["revision_id"])
            for r in self.session.execute(
                text(
                    "SELECT object_type,object_id,revision_id FROM trainer.recommendation_fact_links WHERE recommendation_id=:id ORDER BY object_type,object_id,revision_id"
                ),
                {"id": rec["recommendation_id"]},
            ).mappings()
        ]
        calcs = list(
            self.session.execute(
                text(
                    "SELECT derived_value_id FROM trainer.recommendation_calculation_links WHERE recommendation_id=:id ORDER BY derived_value_id"
                ),
                {"id": rec["recommendation_id"]},
            ).scalars()
        )
        basis, stale = [], []
        fact_tables = {
            "profile": ("profiles", "profile_id", "current_revision_id"),
            "goal": ("goals", "goal_id", "current_revision_id"),
            "exercise": ("exercises", "exercise_id", "current_revision_id"),
            "training_place": ("training_places", "place_id", "current_revision_id"),
            "equipment_item": ("equipment_items", "equipment_id", "current_revision_id"),
            "program_version": ("programs", "program_id", "current_version_id"),
        }
        for ref in facts:
            spec = fact_tables.get(str(ref["object_type"]))
            if spec is None:
                stale.append(ref)
                continue
            table, identity, revision = spec
            current = self.session.execute(
                text(
                    f"SELECT {revision} FROM trainer.{table} WHERE {identity}=:id AND archived_at IS NULL"
                ),
                {"id": ref["object_id"]},
            ).scalar_one_or_none()
            if current is None or str(current) != ref["revision_id"]:
                stale.append(ref)
        for calculation_id in calcs:
            state = self.session.execute(
                text("SELECT state FROM trainer.derived_values WHERE derived_value_id=:id"),
                {"id": calculation_id},
            ).scalar_one_or_none()
            if state != "current":
                stale.append(self._ref("derived_value", calculation_id, None))
        for link in self.session.execute(
            text(
                "SELECT * FROM trainer.recommendation_conclusion_links WHERE recommendation_id=:id ORDER BY conclusion_version_id,role"
            ),
            {"id": rec["recommendation_id"]},
        ).mappings():
            snap = self._knowledge_snapshot(link["conclusion_version_id"], datetime.now(UTC))
            if snap is None:
                raise ContextRepositoryError(
                    "inconsistent_domain_state", "saved scientific basis missing", 500
                )
            version = snap["version"]
            verification = self._verification_snapshot(link["status_revision_id"])
            ref = self._ref(
                "conclusion_version", link["conclusion_version_id"], link["conclusion_version_id"]
            )
            if (
                snap["effective_status"] != "verified"
                or not snap["is_current"]
                or str(snap["status"]["status_revision_id"]) != str(link["status_revision_id"])
            ):
                stale.append(ref)
            basis.append(
                {
                    "conclusion_version_ref": ref,
                    **{
                        k: version[k]
                        for k in (
                            "statement",
                            "population_conditions",
                            "confidence",
                            "review_due_on",
                        )
                    },
                    "status_at_decision": link["status_at_decision"],
                    "current_status": snap["effective_status"],
                    "applicability_assessment": link["applicability_assessment"],
                    "caveats": snap["caveats"],
                    "sources": [
                        {
                            "source_ref": self._ref(
                                "scientific_source", UUID(s["source_id"]), UUID(s["source_id"])
                            ),
                            "relation": s["relation"],
                            "cited_locator": s["cited_locator"],
                            "metadata_snapshot": {
                                **s["source_metadata_snapshot"],
                                "owner_verification": verification,
                            },
                            "current_availability": s["current_availability"],
                            "last_checked_at": s["last_checked_at"],
                        }
                        for s in snap["sources"]
                    ],
                }
            )
        return {
            "decision_text": rec["decision_text"],
            "confidence": rec["confidence"],
            "explanation": explanations.get(payload["detail_level"], explanations["short"]),
            "fact_refs": facts,
            "derived_value_ids": [str(v) for v in calcs],
            "conclusion_version_ids": [b["conclusion_version_ref"]["object_id"] for b in basis],
            "conclusion_basis": basis,
            "assumptions": rec["assumptions"],
            "alternatives": rec["alternatives"],
            "revisit_condition": rec["revisit_condition"],
            "stale_markers": stale,
        }


class EvidenceDatabaseExecutor(ContextDatabaseExecutor):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def record_failed_attempt(self, action: str, kwargs: dict[str, Any], error: Exception) -> None:
        method = {
            "create_evidence_draft": "CMD-14",
            "create_verification_draft": "CMD-15",
        }.get(action)
        if method is None:
            return super().record_failed_attempt(action, kwargs, error)
        correlation_id, key = kwargs.get("correlation_id"), kwargs.get("key")
        if not isinstance(correlation_id, UUID) or not isinstance(key, str):
            return
        code = str(getattr(error, "code", "internal_error"))[:100]
        payload = kwargs.get("payload") or {}
        request_digest = payload.get("request_digest") or digest_payload(payload)
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    "INSERT INTO trainer.idempotent_operations(operation_id,scope,idempotency_key,command_kind,request_digest,state,error_code,received_at,updated_at,created_at) "
                    "VALUES (:id,'local-owner:'||:method,:key,:method,:digest,'failed',:error,:now,:now,:now) ON CONFLICT(scope,idempotency_key) DO NOTHING"
                ),
                {
                    "id": uuid4(),
                    "method": method,
                    "key": key,
                    "digest": request_digest,
                    "error": code,
                    "now": now,
                },
            )
            ContextRepository(session).record_failed_request(
                method=method,
                key=key,
                correlation_id=correlation_id,
                causation_id=kwargs.get("causation_id"),
                error_code=code,
            )
            session.commit()

    def __call__(self, *, action: str, **kwargs: Any) -> dict[str, Any]:
        owner_verifier = kwargs.pop("owner_verifier", False)
        with self._session_factory() as session:
            try:
                result: dict[str, Any] = getattr(
                    EvidenceRepository(session, owner_verifier=owner_verifier), action
                )(**kwargs)
                session.commit()
                return result
            except Exception as error:
                session.rollback()
                self.record_failed_attempt(action, kwargs, error)
                raise


class EvidenceAwareProgressExecutor(ProgressAwareExecutor):
    def _calculation_action(self, action: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        # Capability belongs to evidence only; calculation repositories never receive it.
        return super()._calculation_action(
            action, {k: v for k, v in kwargs.items() if k != "owner_verifier"}
        )
