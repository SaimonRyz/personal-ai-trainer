"""One confirmation for a resolved list of familiar exercises."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from trainer.domain.context import digest_payload
from trainer.domain.exercise import normalize_name
from trainer.infrastructure.db.context_repository import ContextRepositoryError


class FamiliarBatchRepository:
    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self.session = repository.session

    def create(
        self, *, payload: dict[str, Any], key: str,
        correlation_id: UUID, causation_id: UUID | None = None,
    ) -> dict[str, Any]:
        repo = self.repository
        operation_id, replay = repo._reserve("CMD-25", key, payload["request_digest"])
        if replay is not None:
            repo._audit_replay(operation_id, "CMD-25", correlation_id, causation_id, key, replay)
            return replay
        profile_id = UUID(str(payload["profile_id"]))
        expected = UUID(str(payload["expected_profile_revision_id"]))
        current = self.session.execute(text("""
            SELECT current_revision_id FROM trainer.profiles
            WHERE profile_id=:id AND archived_at IS NULL
        """), {"id": profile_id}).scalar_one_or_none()
        if current != expected:
            raise ContextRepositoryError("revision_conflict", "profile revision changed", 409)
        prior = {
            str(row["exercise_id"]): str(row["exercise_revision_id"])
            for row in self.session.execute(text("""
                SELECT exercise_id,exercise_revision_id FROM trainer.profile_familiar_exercises
                WHERE profile_revision_id=:revision
            """), {"revision": expected}).mappings()
        }
        now = datetime.now(UTC)
        entries: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        seen_exercises: set[str] = set()
        seen_names: set[str] = set()
        created_groups: dict[str, dict[str, Any]] = {}
        dependencies: list[tuple[str, UUID, UUID, str]] = [
            ("profile", profile_id, expected, "profile must remain current")
        ]
        child_ids: list[str] = []
        for item in payload["items"]:
            source = item["source_name"].strip()
            source_key = normalize_name(source)
            if not source_key or source_key in seen_sources:
                raise ContextRepositoryError("validation_error", "duplicate or blank source exercise", 422)
            seen_sources.add(source_key)
            if item.get("exercise_ref") is not None:
                ref = item["exercise_ref"]
                exercise_id = UUID(str(ref["object_id"]))
                revision_id = UUID(str(ref["revision_id"]))
                row = self.session.execute(text("""
                    SELECT e.current_revision_id,e.archived_at,e.logically_deleted_at,
                           s.planning_eligible,r.canonical_name
                    FROM trainer.exercises e JOIN trainer.exercise_revisions r
                      ON r.exercise_id=e.exercise_id AND r.exercise_revision_id=:revision
                    JOIN trainer.exercise_status_catalog s ON s.code=r.status_code
                    WHERE e.exercise_id=:exercise
                """), {"exercise": exercise_id, "revision": revision_id}).mappings().one_or_none()
                if (row is None or row["current_revision_id"] != revision_id
                        or row["archived_at"] is not None or row["logically_deleted_at"] is not None
                        or not row["planning_eligible"]):
                    raise ContextRepositoryError("validation_error", "selected exercise is not current and eligible", 422)
                if str(exercise_id) in seen_exercises:
                    raise ContextRepositoryError("validation_error", "exercise selected twice", 422)
                seen_exercises.add(str(exercise_id))
                entries.append({"source_name": source, "action": "existing", "name": row["canonical_name"],
                                "exercise_ref": ref,
                                "previously_familiar": str(exercise_id) in prior,
                                "already_familiar": prior.get(str(exercise_id)) == str(revision_id)})
                dependencies.append(("exercise_revision", exercise_id, revision_id,
                                     "selected familiar exercise must remain current and planning-eligible"))
                continue
            child_revision = UUID(str(item["new_draft_revision_id"]))
            child = self.session.execute(text("""
                SELECT d.draft_id,d.state,d.expires_at,dr.typed_payload
                FROM trainer.draft_revisions dr JOIN trainer.drafts d ON d.draft_id=dr.draft_id
                WHERE dr.draft_revision_id=:revision AND d.latest_revision_id=dr.draft_revision_id
                  AND d.intent_type='exercise_catalog'
            """), {"revision": child_revision}).mappings().one_or_none()
            if child is None or child["state"] != "open" or child["expires_at"] <= now:
                raise ContextRepositoryError("validation_error", "new exercise draft is not open", 422)
            repo._check_dependencies(child_revision)
            typed = dict(child["typed_payload"])
            if typed.get("intent") != "create" or typed.get("context", {}).get("conversation_id") != payload["context"]["conversation_id"]:
                raise ContextRepositoryError("validation_error", "new exercise draft belongs to another action", 422)
            exercise = typed["exercise"]
            normalized = exercise["normalized_name"]
            exercise_id = str(exercise["exercise_id"])
            if normalized in seen_names or exercise_id in seen_exercises:
                raise ContextRepositoryError("validation_error", "duplicate exercise in batch", 422)
            seen_names.add(normalized)
            seen_exercises.add(exercise_id)
            eligible = self.session.execute(text("""
                SELECT planning_eligible FROM trainer.exercise_status_catalog
                WHERE code=:status AND archived_at IS NULL
            """), {"status": exercise["status_code"]}).scalar_one_or_none()
            if not eligible:
                raise ContextRepositoryError("validation_error", "new exercise is not planning-eligible", 422)
            # Share one new group across cards that name the same group.
            kept_groups: list[dict[str, Any]] = []
            kept_changes = [typed["change_plan"][0]]
            for group, change in zip(typed["created_muscle_groups"], typed["change_plan"][1:], strict=True):
                earlier = created_groups.get(group["normalized_name"])
                if earlier is None:
                    created_groups[group["normalized_name"]] = group
                    kept_groups.append(group)
                    kept_changes.append(change)
                else:
                    for role in typed["resolved_muscle_roles"]:
                        if role["muscle_group"]["muscle_group_id"] == group["muscle_group_id"]:
                            role["muscle_group"] = earlier
            typed["created_muscle_groups"] = kept_groups
            typed["change_plan"] = kept_changes
            entries.append({"source_name": source, "action": "create", "name": exercise["canonical_name"],
                            "exercise_draft": typed, "child_draft_id": str(child["draft_id"])})
            child_ids.append(str(child["draft_id"]))
            for dep in self.session.execute(text("""
                SELECT dependency_type,dependency_object_id,dependency_revision_id,reason
                FROM trainer.draft_dependencies WHERE draft_revision_id=:revision
            """), {"revision": child_revision}).mappings():
                dependencies.append((dep["dependency_type"], dep["dependency_object_id"],
                                     dep["dependency_revision_id"], dep["reason"]))
        unresolved = [name.strip() for name in payload.get("unresolved_names", [])]
        unresolved_keys = [normalize_name(name) for name in unresolved]
        if (any(not key or key in seen_sources for key in unresolved_keys)
                or len(set(unresolved_keys)) != len(unresolved_keys)):
            raise ContextRepositoryError("validation_error", "unresolved exercise list overlaps resolved items", 422)
        if all(entry.get("already_familiar") for entry in entries):
            raise ContextRepositoryError("validation_error", "batch makes no change", 422)
        profile_revision = uuid4()
        change_plan = [change for entry in entries if entry["action"] == "create"
                       for change in entry["exercise_draft"]["change_plan"]]
        change_plan.append({"object_type": "profile", "object_id": str(profile_id),
                            "revision_id": str(profile_revision)})
        body = {"intent": "familiar_batch", "profile_id": str(profile_id),
                "expected_profile_revision_id": str(expected), "profile_revision_id": str(profile_revision),
                "entries": entries, "unresolved_names": unresolved,
                "change_plan": change_plan, "child_draft_ids": child_ids,
                "familiar_exercises": [
                    {"exercise_ref": entry["exercise_ref"]} for entry in entries
                    if entry["action"] == "existing"
                ]}
        draft_id, revision_id = uuid4(), uuid4()
        digest = digest_payload(body)
        self.session.execute(text("""
            INSERT INTO trainer.drafts(draft_id,intent_type,owner_profile_id,state,latest_revision_id,expires_at,created_at)
            VALUES (:draft,'familiar_exercise_batch',:profile,'open',:revision,:expires,:now)
        """), {"draft": draft_id, "profile": profile_id, "revision": revision_id,
               "expires": now + timedelta(hours=24), "now": now})
        self.session.execute(text("""
            INSERT INTO trainer.draft_revisions(draft_revision_id,draft_id,revision_no,typed_payload,
                content_digest,trust_state,confirmation_kind,created_at)
            VALUES (:revision,:draft,1,CAST(:payload AS jsonb),:digest,'draft','normal',:now)
        """), {"revision": revision_id, "draft": draft_id, "payload": json.dumps(body),
               "digest": digest, "now": now})
        unique_dependencies = {(kind, obj, revision): reason for kind, obj, revision, reason in dependencies}
        recorded = [repo._add_dependency(revision_id, kind, obj, revision, reason, now)
                    for (kind, obj, revision), reason in unique_dependencies.items()]
        response = {"draft_id": str(draft_id), "revision_id": str(revision_id),
                    "revision_no": 1, "content_digest": digest, "dependencies": recorded,
                    "gaps": [], "preview_candidate": {"summary": "Пакет знакомых упражнений готов",
                    "confirmation_kind": "normal", "complete": True}}
        repo._finish(operation_id, response)
        repo._audit(operation_id, "CMD-25", correlation_id, causation_id, key,
                    [{"object_type": "draft", "object_id": str(draft_id), "revision_id": str(revision_id)}])
        return response

    @staticmethod
    def render(typed: dict[str, Any]) -> str:
        from trainer.infrastructure.db.context_repository import ContextRepository

        entries = typed["entries"]
        additions = sum(not item.get("previously_familiar", False) for item in entries)
        refreshed = sum(item.get("previously_familiar", False) and not item.get("already_familiar", False)
                        for item in entries)
        created = sum(item["action"] == "create" for item in entries)
        lines = ["Добавить знакомые упражнения?",
                 f"Новые отметки знакомости: {additions}. Новые карточки: {created}."]
        if refreshed:
            lines.append(f"Отметки, привязка которых обновится до текущей карточки: {refreshed}.")
        for item in entries:
            action = "будет создано и отмечено знакомым" if item["action"] == "create" else (
                "уже отмечено знакомым" if item["already_familiar"] else
                "знакомо; ссылка обновится до текущей карточки" if item["previously_familiar"] else
                "найдено; будет отмечено знакомым"
            )
            lines.append(f"• {item['source_name']} → {item['name']}: {action}.")
            if item["action"] == "create":
                card = item["exercise_draft"]["exercise"]
                metrics = ", ".join(ContextRepository._preview_value_label(
                    "metric_kind", measurement["metric_kind"]
                ) for measurement in card["measurements"])
                equipment = ", ".join(ContextRepository._preview_value_label(
                    "equipment_type", requirement["equipment_type"]
                ) for requirement in card["equipment_requirements"]) or "не требуется"
                lines.append(f"  Новая карточка: результат — {metrics}; оборудование — {equipment}.")
        groups = {group["name"] for item in entries if item["action"] == "create"
                  for group in item["exercise_draft"]["created_muscle_groups"]}
        if groups:
            lines.append("Будут созданы группы мышц: " + ", ".join(sorted(groups)) + ".")
        if typed.get("unresolved_names"):
            lines.append("Не будут записаны до уточнения: " + ", ".join(typed["unresolved_names"]) + ".")
        lines.append("До подтверждения каталог и профиль не изменены.")
        return "\n".join(lines)

    def commit(
        self, typed: dict[str, Any], confirmation: UUID, result: UUID,
        changes: list[dict[str, Any]], now: datetime,
    ) -> None:
        repo = self.repository
        for entry in typed["entries"]:
            if entry["action"] == "create":
                child = entry["exercise_draft"]
                repo._commit_exercise(child, confirmation, result, child["change_plan"], now)
        profile_id = UUID(typed["profile_id"])
        old_revision = UUID(typed["expected_profile_revision_id"])
        new_revision = UUID(typed["profile_revision_id"])
        current = self.session.execute(text("""
            SELECT current_revision_id FROM trainer.profiles
            WHERE profile_id=:profile AND archived_at IS NULL FOR UPDATE
        """), {"profile": profile_id}).scalar_one_or_none()
        if current != old_revision:
            raise ContextRepositoryError("stale_preview", "profile changed before confirmation", 409)
        self.session.execute(text("""
            INSERT INTO trainer.profile_revisions(profile_revision_id,profile_id,revision_no,
                supersedes_revision_id,birth_date,sex,timezone,started_on,desired_sessions_per_week,
                usual_place_id,comment,source_kind,confirmation_id,confirmed_at,created_at)
            SELECT :new,profile_id,revision_no+1,profile_revision_id,birth_date,sex,timezone,
                   started_on,desired_sessions_per_week,usual_place_id,comment,'user',:confirmation,:now,:now
            FROM trainer.profile_revisions WHERE profile_revision_id=:old AND profile_id=:profile
        """), {"new": new_revision, "old": old_revision, "profile": profile_id,
               "confirmation": confirmation, "now": now})
        selected: dict[str, str] = {}
        for entry in typed["entries"]:
            if entry["action"] == "existing":
                ref = entry["exercise_ref"]
                selected[str(ref["object_id"])] = str(ref["revision_id"])
            else:
                change = entry["exercise_draft"]["change_plan"][0]
                selected[change["object_id"]] = change["revision_id"]
        self.session.execute(text("""
            INSERT INTO trainer.profile_familiar_exercises(profile_revision_id,exercise_id,
                exercise_revision_id,created_at)
            SELECT :new,exercise_id,exercise_revision_id,:now
            FROM trainer.profile_familiar_exercises WHERE profile_revision_id=:old
              AND NOT (exercise_id = ANY(:selected_ids))
        """), {"new": new_revision, "old": old_revision, "now": now,
               "selected_ids": [UUID(item) for item in selected]})
        for exercise_id, exercise_revision in selected.items():
            self.session.execute(text("""
                INSERT INTO trainer.profile_familiar_exercises(profile_revision_id,exercise_id,
                    exercise_revision_id,created_at)
                VALUES (:profile_revision,:exercise,:exercise_revision,:now)
            """), {"profile_revision": new_revision, "exercise": UUID(exercise_id),
                   "exercise_revision": UUID(exercise_revision), "now": now})
        self.session.execute(text("""
            UPDATE trainer.profiles SET current_revision_id=:new WHERE profile_id=:profile
        """), {"new": new_revision, "profile": profile_id})
        for child_id in typed["child_draft_ids"]:
            self.session.execute(text("""
                UPDATE trainer.drafts SET state='cancelled',cancelled_at=:now
                WHERE draft_id=:draft AND state='open'
            """), {"draft": UUID(child_id), "now": now})
        repo._insert_change(changes[-1], result, "correct",
                            {"profile_revision_id": str(old_revision)},
                            {"profile_revision_id": str(new_revision), "familiar_exercises": list(selected)},
                            "added familiar exercises", now)
