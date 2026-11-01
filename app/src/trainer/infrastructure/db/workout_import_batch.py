"""One exact, atomic confirmation for a named part of historical workouts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import ContextRepositoryError


class WorkoutImportBatchRepository:
    def __init__(self, repository: Any) -> None:
        self.repo = repository
        self.session = repository.session

    def _child(self, revision_id: str, profile_id: UUID, conversation_id: str) -> dict[str, Any]:
        row = self.session.execute(text("""
            SELECT d.draft_id,d.latest_revision_id,d.owner_profile_id,d.state,d.expires_at,
                   dr.typed_payload,dr.content_digest
            FROM trainer.draft_revisions dr JOIN trainer.drafts d ON d.draft_id=dr.draft_id
            WHERE dr.draft_revision_id=:revision AND d.intent_type='workout_report'
            FOR UPDATE OF d
        """), {"revision": UUID(revision_id)}).mappings().one_or_none()
        if (row is None or row["state"] != "open" or row["expires_at"] <= datetime.now(UTC)
                or str(row["latest_revision_id"]) != revision_id
                or row["owner_profile_id"] != profile_id
                or row["typed_payload"].get("context", {}).get("conversation_id") != conversation_id):
            raise ContextRepositoryError("stale_preview", "report draft is no longer current", 409)
        return dict(row)

    def validate_children(self, typed: dict[str, Any]) -> None:
        self._lock_dates(UUID(typed["profile_id"]), typed["entries"])
        for entry in typed["entries"]:
            if entry["decision"] not in {"create", "correct"}:
                continue
            row = self._child(entry["report_draft_revision_id"], UUID(typed["profile_id"]),
                              typed["conversation_id"])
            if row["content_digest"] != entry["report_draft_digest"]:
                raise ContextRepositoryError("stale_preview", "report draft content changed", 409)
            report = entry["report_draft"]["report"]
            if report.get("name") is not None and report["name"] != entry["name"]:
                raise ContextRepositoryError("stale_preview", "batch name differs from report name", 409)
            self._check_fact_sources(report, {source["field_path"] for source in entry["fact_sources"]})
            if entry["decision"] == "correct":
                self._check_correction_records(entry["report_draft"])
            current = self._same_date_sessions(UUID(typed["profile_id"]), entry["local_start_date"])
            if current != entry["same_date_sessions"]:
                raise ContextRepositoryError("stale_preview", "workouts on this date changed", 409)

    def _check_correction_records(self, child: dict[str, Any]) -> None:
        source = child["correction"]["source_revision_id"]
        session = child["session_id"]
        checks = (
            ("pending_candidates_to_invalidate", """
                SELECT candidate_id FROM trainer.record_candidates
                WHERE session_revision_id=:source AND status='pending' ORDER BY candidate_id
            """),
            ("source_records_to_revalidate", """
                SELECT record_id FROM trainer.actual_records
                WHERE session_id=:session ORDER BY record_id
            """),
            ("accepted_candidates_to_reconcile", """
                SELECT c.candidate_id FROM trainer.record_candidates c
                JOIN trainer.actual_records r ON r.candidate_id=c.candidate_id
                WHERE r.session_id=:session AND c.status='accepted' ORDER BY c.candidate_id
            """),
        )
        for key, query in checks:
            current = [str(value) for value in self.session.execute(
                text(query), {"source": UUID(source), "session": UUID(session)}).scalars()]
            if current != child["record_effects"].get(key, []):
                raise ContextRepositoryError("stale_preview", "record correction effects changed", 409)
        from trainer.infrastructure.db.workouts import WorkoutRepository

        fresh = WorkoutRepository(self.repo)._plan_record_correction(
            session_id=UUID(session), old_revision=UUID(source),
            before_tree=child["correction"]["before"],
            after_tree={"local_start_date": child["report"]["local_start_date"],
                        "blocks": child["report"]["blocks"]},
            report=child["report"], record_effects=child["record_effects"],
        )
        if fresh != child["record_effects"].get("preview_plan"):
            raise ContextRepositoryError("stale_preview", "record correction plan changed", 409)

    def _lock_dates(self, profile_id: UUID, entries: list[dict[str, Any]]) -> None:
        for local_date in sorted({item["local_start_date"] for item in entries
                                  if item["decision"] in {"create", "correct"}}):
            self.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                 {"key": f"workout-import:{profile_id}:{local_date}"})

    def _same_date_sessions(self, profile_id: UUID, local_date: str) -> list[dict[str, str]]:
        return [{"session_id": str(row["session_id"]),
                 "revision_id": str(row["current_revision_id"])} for row in self.session.execute(text("""
            SELECT s.session_id,s.current_revision_id FROM trainer.workout_sessions s
            JOIN trainer.workout_session_revisions r ON r.session_revision_id=s.current_revision_id
            WHERE s.profile_id=:profile AND r.local_start_date=:date
              AND s.archived_at IS NULL AND s.logically_deleted_at IS NULL
            ORDER BY s.session_id
        """), {"profile": profile_id, "date": local_date}).mappings()]

    def create(self, *, payload: dict[str, Any], key: str,
               correlation_id: UUID, causation_id: UUID | None = None) -> dict[str, Any]:
        repo = self.repo
        operation, replay = repo._reserve("CMD-26", key, payload["request_digest"])
        if replay is not None:
            repo._audit_replay(operation, "CMD-26", correlation_id, causation_id, key, replay)
            return replay
        profile_id = UUID(str(payload["profile_id"]))
        expected = UUID(str(payload["expected_profile_revision_id"]))
        current = self.session.execute(text("""
            SELECT current_revision_id FROM trainer.profiles
            WHERE profile_id=:profile AND archived_at IS NULL FOR UPDATE
        """), {"profile": profile_id}).scalar_one_or_none()
        if current != expected:
            raise ContextRepositoryError("revision_conflict", "profile revision changed", 409)
        previous = None
        if payload.get("draft_id"):
            previous = self.session.execute(text("""
                SELECT d.draft_id,d.latest_revision_id,d.owner_profile_id,d.state,d.expires_at,
                       dr.revision_no,dr.typed_payload
                FROM trainer.drafts d JOIN trainer.draft_revisions dr
                  ON dr.draft_revision_id=d.latest_revision_id
                WHERE d.draft_id=:draft AND d.intent_type='workout_import_batch'
                FOR UPDATE OF d
            """), {"draft": UUID(str(payload["draft_id"]))}).mappings().one_or_none()
            if (previous is None or previous["state"] != "open"
                    or previous["expires_at"] <= datetime.now(UTC)
                    or str(previous["latest_revision_id"]) != str(payload["expected_revision_id"])
                    or previous["owner_profile_id"] != profile_id
                    or previous["typed_payload"]["part_label"] != payload["part_label"]):
                raise ContextRepositoryError("stale_preview", "batch revision is no longer current", 409)
        entries = []
        self._lock_dates(profile_id, payload["entries"])
        seen_sessions: set[str] = set()
        seen_sources: set[str] = set()
        dependencies: dict[tuple[str, str, str], tuple[str, UUID, UUID, str]] = {
            ("profile", str(profile_id), str(expected)):
                ("profile", profile_id, expected, "profile must remain current")
        }
        change_plan: list[dict[str, Any]] = []
        downstream_effects: list[dict[str, Any]] = []
        for item in payload["entries"]:
            entry = {field: item.get(field) for field in (
                "entry_key", "name", "local_start_date", "decision", "reason", "fact_sources"
            )}
            if item["decision"] not in {"create", "correct"}:
                entries.append(entry)
                continue
            child_revision = str(item["report_draft_revision_id"])
            child = self._child(child_revision, profile_id, payload["context"]["conversation_id"])
            repo._check_dependencies(UUID(child_revision))
            typed = dict(child["typed_payload"])
            if typed.get("intent") != "workout_report" or any(
                gap.get("disposition") != "leave_unknown" for gap in typed.get("gaps", [])
            ):
                raise ContextRepositoryError("validation_error", "report has unresolved required facts", 422)
            report = typed["report"]
            if report["local_start_date"] != item["local_start_date"]:
                raise ContextRepositoryError("validation_error", "batch date differs from report date", 422)
            if report.get("name") is not None and report["name"] != item["name"]:
                raise ContextRepositoryError(
                    "validation_error", "batch name differs from report name", 422,
                    field_errors=[{"path": "entries.name", "code": "report_name_mismatch",
                                   "message_safe": f"Use the exact report name: {report['name']}."}],
                )
            if (item["decision"] == "correct") != bool(typed.get("correction")):
                raise ContextRepositoryError("validation_error", "batch decision differs from report draft", 422)
            session_id = str(typed["session_id"])
            if session_id in seen_sessions:
                raise ContextRepositoryError("validation_error", "workout selected twice", 422)
            seen_sessions.add(session_id)
            source_paths = [source["field_path"] for source in item["fact_sources"]]
            if any(not path.startswith("/report") for path in source_paths):
                raise ContextRepositoryError("validation_error", "fact source path must target report", 422)
            if "/report/local_start_date" not in source_paths:
                raise ContextRepositoryError("validation_error", "report date needs a source", 422)
            self._check_fact_sources(report, set(source_paths))
            for block in report["blocks"]:
                for exercise in block["exercises"]:
                    scope = f"/report/blocks/{block['position']}/exercises/{exercise['position']}"
                    sets = exercise["sets"] or [None]
                    for actual_set in sets:
                        target = scope if actual_set is None else scope + f"/sets/{actual_set['position']}"
                        if not any(path == target or path.startswith(target + "/")
                                   for path in source_paths):
                            raise ContextRepositoryError("validation_error", "each exercise or set needs a source", 422)
            existing = self._same_date_sessions(profile_id, report["local_start_date"])
            if item["decision"] == "create":
                if existing and not item.get("reason"):
                    raise ContextRepositoryError("validation_error", "same-date workout needs explicit separate reason", 422)
                if report["local_start_date"] in seen_sources and not item.get("reason"):
                    raise ContextRepositoryError("validation_error", "same-date batch entries need separate reason", 422)
                seen_sources.add(report["local_start_date"])
            else:
                target_revision = self.session.execute(text("""
                    SELECT current_revision_id FROM trainer.workout_sessions
                    WHERE session_id=:session AND profile_id=:profile
                      AND archived_at IS NULL AND logically_deleted_at IS NULL
                """), {"session": UUID(session_id), "profile": profile_id}).scalar_one_or_none()
                if target_revision is None or str(target_revision) != typed["correction"]["source_revision_id"]:
                    raise ContextRepositoryError("stale_preview", "correction target is no longer current", 409)
            entry.update({"report_draft_revision_id": child_revision,
                          "report_draft_id": str(child["draft_id"]),
                          "report_draft_digest": child["content_digest"],
                          "report_draft": typed,
                          "same_date_existing_count": len(existing),
                          "same_date_sessions": existing,
                          "unknown_fact_count": len(typed.get("gaps", [])),
                          "report_name_missing": report.get("name") is None,
                          "exercise_summaries": self._exercise_summaries(report)})
            entries.append(entry)
            change_plan.extend(typed["change_plan"])
            downstream_effects.extend(typed.get("downstream_effects", []))
            for dep in typed.get("dependencies", []):
                dep_key = (dep["dependency_type"], dep["object_id"], dep["revision_id"])
                dependencies[dep_key] = (dep["dependency_type"], UUID(dep["object_id"]),
                                         UUID(dep["revision_id"]), dep["reason"])
        body = {"intent": "workout_import_batch", "profile_id": str(profile_id),
                "expected_profile_revision_id": str(expected),
                "conversation_id": payload["context"]["conversation_id"],
                "part_label": payload["part_label"], "entries": entries,
                "change_plan": change_plan, "downstream_effects": downstream_effects,
                "gaps": []}
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 1_048_576:
            raise ContextRepositoryError("validation_error", "import part is too large; split it into named parts", 422)
        now = datetime.now(UTC)
        draft_id = UUID(str(previous["draft_id"])) if previous else uuid4()
        revision_id = uuid4()
        revision_no = previous["revision_no"] + 1 if previous else 1
        content_digest = digest_payload(body)
        if previous is None:
            self.session.execute(text("""
                INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,
                    latest_revision_id,expires_at,created_at)
                VALUES (:draft,'workout_import_batch',:profile,'open',:revision,:expires,:now)
            """), {"draft": draft_id, "profile": profile_id, "revision": revision_id,
                   "expires": now + timedelta(hours=24), "now": now})
        else:
            self.session.execute(text("""
                UPDATE trainer.draft_revisions SET trust_state='superseded'
                WHERE draft_revision_id=:old AND trust_state='shown'
            """), {"old": previous["latest_revision_id"]})
        self.session.execute(text("""
            INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,
                typed_payload,content_digest,trust_state,confirmation_kind,created_at)
            VALUES (:revision,:draft,:number,CAST(:payload AS jsonb),:digest,'draft','normal',:now)
        """), {"revision": revision_id, "draft": draft_id, "number": revision_no,
               "payload": json.dumps(body), "digest": content_digest, "now": now})
        if previous:
            self.session.execute(text("""
                UPDATE trainer.drafts SET latest_revision_id=:revision WHERE draft_id=:draft
            """), {"revision": revision_id, "draft": draft_id})
        recorded = [repo._add_dependency(revision_id, kind, obj, rev, reason, now)
                    for kind, obj, rev, reason in dependencies.values()]
        response = {"draft_id": str(draft_id), "revision_id": str(revision_id),
                    "revision_no": revision_no, "content_digest": content_digest,
                    "dependencies": recorded, "gaps": [],
                    "preview_candidate": {"summary": "Часть истории готова к предпросмотру",
                                          "confirmation_kind": "normal", "complete": True}}
        repo._finish(operation, response)
        repo._audit(operation, "CMD-26", correlation_id, causation_id, key,
                    [{"object_type": "draft", "object_id": str(draft_id),
                      "revision_id": str(revision_id)}])
        return response

    @staticmethod
    def _check_fact_sources(report: dict[str, Any], paths: set[str]) -> None:
        """Require a source for each user-reported value, not generated IDs or snapshots."""
        required: set[str] = set()
        for field in ("local_start_date", "name", "started_at", "ended_at",
                      "duration_seconds", "lifecycle", "effort", "comment"):
            if report.get(field) is not None:
                required.add(f"/report/{field}")
        for metric in report.get("workout_metrics", []):
            if metric.get("source_kind") == "user":
                required.add(f"/report/workout_metrics/{metric['metric_kind']}/value")
        for block in report["blocks"]:
            block_scope = f"/report/blocks/{block['position']}"
            for field in ("block_type_code", "declared_rounds", "rest_seconds", "comment"):
                if block.get(field) is not None:
                    required.add(f"{block_scope}/{field}")
            for exercise in block["exercises"]:
                scope = f"{block_scope}/exercises/{exercise['position']}"
                for field in ("result", "reason"):
                    if exercise.get(field) is not None:
                        required.add(f"{scope}/{field}")
                for actual_set in exercise["sets"]:
                    target = scope + f"/sets/{actual_set['position']}"
                    for field in ("round_no", "side", "set_type_code", "status",
                                  "full_reps", "partial_reps", "failed_attempts", "reported_rpe",
                                  "reported_rir", "failure_reached", "rom_state", "technique_state",
                                  "tempo", "pause_seconds", "rest_after_seconds", "comment"):
                        if actual_set.get(field) is not None:
                            required.add(f"{target}/{field}")
                    for metric in actual_set["metrics"]:
                        if metric.get("source_kind") == "user":
                            required.add(f"{target}/metrics/{metric['metric_kind']}/value")
                    for load in actual_set["loads"]:
                        for field in ("load_kind", "mass", "unit_code", "mass_meaning",
                                      "item_count", "direction", "raw_ambiguous_text"):
                            if load.get(field) is not None:
                                required.add(f"{target}/loads/{load['position']}/{field}")
        missing = sorted(required - paths)
        if missing:
            raise ContextRepositoryError(
                "validation_error", f"reported fact needs a source: {missing[0]}", 422,
                field_errors=[{"path": "entries.fact_sources", "code": "missing_fact_source",
                               "message_safe": f"Add source for {missing[0]}."}],
            )

    def _exercise_summaries(self, report: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for block in report["blocks"]:
            for exercise in block["exercises"]:
                name = self.session.execute(text("""
                    SELECT canonical_name FROM trainer.exercise_revisions
                    WHERE exercise_revision_id=:revision AND exercise_id=:exercise
                """), {"revision": UUID(str(exercise["exercise_revision_id"])),
                       "exercise": UUID(str(exercise["exercise_id"]))}).scalar_one_or_none()
                if name is None:
                    raise ContextRepositoryError("stale_dependency", "exercise revision is unavailable", 409)
                sets = []
                for actual_set in exercise["sets"]:
                    pieces = []
                    repetitions = actual_set.get("full_reps")
                    if repetitions is None:
                        repetitions = next((metric["value"] for metric in actual_set["metrics"]
                                            if metric["metric_kind"] == "repetitions"), None)
                    if repetitions is not None:
                        pieces.append(f"{repetitions} повт.")
                    for metric in actual_set["metrics"]:
                        if metric["metric_kind"] in {"duration", "distance"}:
                            unit = {"second": "с", "meter": "м"}.get(
                                metric["unit_code"], metric["unit_code"]
                            )
                            pieces.append(f"{metric['value']} {unit}")
                    loads = [f"{load['mass']} { {'kilogram': 'кг', 'kg': 'кг', 'pound': 'фунтов'}.get(load['unit_code'], load['unit_code'])}"
                             for load in actual_set["loads"]
                             if load.get("mass") is not None and load.get("load_kind") == "external"]
                    pieces.extend(loads)
                    if pieces:
                        sets.append(" × ".join(pieces))
                if sets:
                    summary = "; ".join(sets[:3])
                    if len(sets) > 3:
                        summary += f"; ещё подходов: {len(sets) - 3}"
                    names.append(f"{name} ({summary})")
                else:
                    names.append(name)
        return names

    @staticmethod
    def render(typed: dict[str, Any]) -> str:
        entries = typed["entries"]
        creates = [item for item in entries if item["decision"] == "create"]
        corrections = [item for item in entries if item["decision"] == "correct"]
        excluded = [item for item in entries if item["decision"] in {"exclude", "unresolved"}]
        lines = [f"Импортировать часть истории «{typed['part_label']}»?",
                 f"Создать тренировок: {len(creates)}. Исправить существующих: {len(corrections)}. "
                 f"Не записывать: {len(excluded)}. Пока ничего не сохранено."]
        for index, item in enumerate(entries, 1):
            date = item.get("local_start_date") or "дата не установлена"
            action = {"create": "создать", "correct": "исправить", "exclude": "исключить",
                      "unresolved": "не включать до уточнения"}[item["decision"]]
            line = f"{index}. {date} — {item['name']}: {action}"
            if item.get("report_name_missing"):
                line += "; название в отчёте не задано"
            if item.get("exercise_summaries"):
                summaries = item["exercise_summaries"]
                line += "; результаты: " + ", ".join(summaries[:3])
                if len(summaries) > 3:
                    line += f"; ещё упражнений: {len(summaries) - 3}"
            if item.get("reason"):
                line += f"; причина: {item['reason']}"
            if item["decision"] == "create" and item.get("same_date_existing_count"):
                line += f"; на эту дату уже есть {item['same_date_existing_count']} тренировок"
            if item.get("unknown_fact_count"):
                line += f"; неуказанных фактов: {item['unknown_fact_count']}"
            lines.append(line + ".")
        lines.append("Подробности каждой включённой тренировки можно открыть до подтверждения.")
        lines.append("Подтверждение запишет весь указанный пакет одной операцией.")
        return "\n".join(lines)

    def commit(self, typed: dict[str, Any], confirmation: UUID, result: UUID,
               changes: list[dict[str, Any]], now: datetime) -> list[UUID]:
        from trainer.infrastructure.db.workouts import WorkoutRepository

        self.validate_children(typed)
        invalidated: list[UUID] = []
        for entry in typed["entries"]:
            if entry["decision"] not in {"create", "correct"}:
                continue
            child = entry["report_draft"]
            child_changes = [dict(item) for item in child["change_plan"]]
            invalidated.extend(WorkoutRepository(self.repo).commit(
                child, confirmation, result, child_changes, now,
                create_record_candidates=False,
            ))
            for change in child_changes:
                if change not in changes:
                    changes.append(change)
            self.session.execute(text("""
                UPDATE trainer.drafts SET state='cancelled',cancelled_at=:now
                WHERE draft_id=:draft AND state='open'
            """), {"draft": UUID(entry["report_draft_id"]), "now": now})
        return invalidated
