# ruff: noqa: E501
"""Short-lived process-local snapshots for complete QRY-05 history reads."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError
from trainer.infrastructure.db.workouts import WorkoutRepository


@dataclass(frozen=True)
class WorkoutRevisionRef:
    session_id: str
    revision_id: str
    revision_no: int
    local_start_date: str
    current: bool


@dataclass(frozen=True)
class WorkoutHistorySnapshot:
    snapshot_id: str
    profile_id: str
    filter_digest: str
    snapshot_confirmed_at: datetime
    entries: tuple[WorkoutRevisionRef, ...]
    expires_at: float


class WorkoutHistorySnapshotRegistry:
    """Bounded cache; API restart/expiry deliberately invalidates unfinished reads."""

    def __init__(self, *, ttl_seconds: int = 900, max_snapshots: int = 8) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_snapshots = max_snapshots
        self._lock = threading.RLock()
        self._snapshots: OrderedDict[str, WorkoutHistorySnapshot] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired = [key for key, value in self._snapshots.items() if value.expires_at <= now]
        for key in expired:
            self._snapshots.pop(key, None)

    def create(
        self,
        *,
        profile_id: str,
        filter_digest: str,
        snapshot_confirmed_at: datetime,
        entries: list[WorkoutRevisionRef],
    ) -> WorkoutHistorySnapshot:
        now = time.monotonic()
        snapshot = WorkoutHistorySnapshot(
            snapshot_id=str(uuid4()),
            profile_id=profile_id,
            filter_digest=filter_digest,
            snapshot_confirmed_at=snapshot_confirmed_at,
            entries=tuple(entries),
            expires_at=now + self._ttl_seconds,
        )
        with self._lock:
            self._prune(now)
            while len(self._snapshots) >= self._max_snapshots:
                self._snapshots.popitem(last=False)
            self._snapshots[snapshot.snapshot_id] = snapshot
        return snapshot

    def get(self, snapshot_id: str) -> WorkoutHistorySnapshot | None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is not None:
                self._snapshots.move_to_end(snapshot_id)
            return snapshot


SNAPSHOT_REGISTRY = WorkoutHistorySnapshotRegistry()


def _history_filter_digest(payload: dict[str, Any]) -> str:
    filters = {
        "mode": "all_confirmed",
        "exercise_id": str(payload["exercise_id"]) if payload.get("exercise_id") else None,
        "lifecycle": sorted(payload.get("lifecycle", [])),
    }
    canonical = json.dumps(filters, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkoutHistorySnapshotRepository(ContextRepository):
    """Reads pinned immutable revision references and hydrates only the requested page."""

    def __init__(self, session: Session, registry: WorkoutHistorySnapshotRegistry) -> None:
        super().__init__(session)
        self._registry = registry

    @staticmethod
    def _decode_cursor(cursor_value: str, expected_filter_digest: str) -> tuple[str, int]:
        from base64 import b64decode

        try:
            raw = b64decode(
                cursor_value + "=" * (-len(cursor_value) % 4), altchars=b"-_", validate=True
            )
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"v", "snapshot_id", "offset", "filter_digest"}
                or value["v"] != 1
                or not isinstance(value["snapshot_id"], str)
                or type(value["offset"]) is not int
                or value["offset"] < 1
                or value["filter_digest"] != expected_filter_digest
            ):
                raise ValueError("invalid cursor fields")
            UUID(value["snapshot_id"])
            return value["snapshot_id"], value["offset"]
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ContextRepositoryError(
                "validation_error", "history cursor is invalid", 400
            ) from error

    @staticmethod
    def _encode_cursor(snapshot: WorkoutHistorySnapshot, offset: int) -> str:
        from base64 import urlsafe_b64encode

        raw = json.dumps(
            {
                "v": 1,
                "snapshot_id": snapshot.snapshot_id,
                "offset": offset,
                "filter_digest": snapshot.filter_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    def _active_profile_id(self) -> str:
        rows = list(
            self.session.execute(
                text("SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL")
            ).scalars()
        )
        if len(rows) != 1:
            raise ContextRepositoryError("not_found", "single active owner profile required", 404)
        return str(rows[0])

    def _new_snapshot(self, payload: dict[str, Any]) -> WorkoutHistorySnapshot:
        # One MVCC snapshot captures the selected revisions and each session's current pointer.
        self.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        snapshot_time = self.session.execute(text("SELECT transaction_timestamp()")).scalar_one()
        profile_id = self._active_profile_id()
        filters = [
            "s.archived_at IS NULL",
            "s.logically_deleted_at IS NULL",
            "s.profile_id=:profile_id",
        ]
        params: dict[str, Any] = {"profile_id": profile_id}
        if payload.get("lifecycle"):
            filters.append("r.lifecycle = ANY(CAST(:lifecycles AS text[]))")
            params["lifecycles"] = payload["lifecycle"]
        if payload.get("exercise_id"):
            filters.append(
                """EXISTS (
                    SELECT 1 FROM trainer.actual_blocks b
                    JOIN trainer.exercise_executions e USING(actual_block_id)
                    WHERE b.session_revision_id=r.session_revision_id AND e.exercise_id=:exercise
                )"""
            )
            params["exercise"] = payload["exercise_id"]
        rows = (
            self.session.execute(
                text(f"""
                SELECT s.session_id,s.current_revision_id,r.session_revision_id,r.revision_no,
                       r.local_start_date
                FROM trainer.workout_sessions s
                JOIN trainer.workout_session_revisions r ON r.session_id=s.session_id
                JOIN trainer.confirmations c ON c.confirmation_id=r.confirmation_id
                JOIN trainer.draft_revisions dr ON dr.draft_revision_id=c.draft_revision_id
                JOIN trainer.drafts d ON d.draft_id=dr.draft_id AND d.owner_profile_id=s.profile_id
                WHERE {" AND ".join(filters)}
                ORDER BY r.local_start_date DESC,s.session_id DESC,r.revision_no DESC
            """),
                params,
            )
            .mappings()
            .all()
        )
        entries = [
            WorkoutRevisionRef(
                session_id=str(row["session_id"]),
                revision_id=str(row["session_revision_id"]),
                revision_no=int(row["revision_no"]),
                local_start_date=row["local_start_date"].isoformat(),
                current=row["session_revision_id"] == row["current_revision_id"],
            )
            for row in rows
        ]
        return self._registry.create(
            profile_id=profile_id,
            filter_digest=_history_filter_digest(payload),
            snapshot_confirmed_at=snapshot_time,
            entries=entries,
        )

    def _hydrate(
        self, snapshot: WorkoutHistorySnapshot, refs: tuple[WorkoutRevisionRef, ...]
    ) -> list[dict[str, Any]]:
        if not refs:
            return []
        rows = (
            self.session.execute(
                text("""
                SELECT s.session_id,s.profile_id,s.current_revision_id,r.session_revision_id,r.revision_no,
                       r.supersedes_revision_id,r.plan_revision_id,r.local_start_date,r.lifecycle,
                       r.confirmation_id,c.confirmed_at,dc.reason,dr.content_digest,
                       dc.after_snapshot,p.plan_id,pr.content_digest AS plan_content_digest
                FROM unnest(CAST(:session_ids AS uuid[]),CAST(:revision_ids AS uuid[]))
                    WITH ORDINALITY AS wanted(session_id,revision_id,ordinality)
                JOIN trainer.workout_sessions s ON s.session_id=wanted.session_id
                JOIN trainer.workout_session_revisions r
                  ON r.session_id=s.session_id AND r.session_revision_id=wanted.revision_id
                JOIN trainer.confirmations c ON c.confirmation_id=r.confirmation_id
                JOIN trainer.draft_revisions dr ON dr.draft_revision_id=c.draft_revision_id
                JOIN trainer.drafts d ON d.draft_id=dr.draft_id AND d.owner_profile_id=s.profile_id
                LEFT JOIN trainer.domain_changes dc ON dc.object_type='workout_session'
                  AND dc.object_id=s.session_id AND dc.new_revision_id=r.session_revision_id
                LEFT JOIN trainer.workout_plans p ON p.plan_id=s.plan_id
                LEFT JOIN trainer.workout_plan_revisions pr ON pr.plan_revision_id=r.plan_revision_id
                WHERE s.profile_id=:profile_id
                ORDER BY wanted.ordinality
            """),
                {
                    "session_ids": [ref.session_id for ref in refs],
                    "revision_ids": [ref.revision_id for ref in refs],
                    "profile_id": snapshot.profile_id,
                },
            )
            .mappings()
            .all()
        )
        if len(rows) != len(refs):
            raise ContextRepositoryError(
                "inconsistent_domain_state",
                "workout history snapshot reference is unavailable",
                500,
            )
        repo = WorkoutRepository(self)
        items: list[dict[str, Any]] = []
        for ref, row in zip(refs, rows, strict=True):
            revision_id = UUID(ref.revision_id)
            session_id = UUID(ref.session_id)
            tree = self._json_row(repo._load_saved_tree(session_id, revision_id))
            if row["after_snapshot"] is None or self._json_row(row["after_snapshot"]) != tree:
                raise ContextRepositoryError(
                    "inconsistent_domain_state",
                    "workout history snapshot differs from its confirmed tree",
                    500,
                )
            items.append(
                {
                    "session_ref": self._ref("workout_session", session_id, revision_id),
                    "current": ref.current,
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
                    "provenance": self._workout_revision_provenance(revision_id),
                }
            )
        return items

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        filter_digest = _history_filter_digest(payload)
        cursor_value = payload.get("cursor")
        if cursor_value:
            snapshot_id, offset = self._decode_cursor(cursor_value, filter_digest)
            snapshot = self._registry.get(snapshot_id)
            if snapshot is None:
                raise ContextRepositoryError(
                    "history_snapshot_expired",
                    "workout history snapshot expired; restart the read",
                    410,
                )
            if snapshot.filter_digest != filter_digest:
                raise ContextRepositoryError(
                    "validation_error", "history cursor filters differ", 400
                )
            if snapshot.profile_id != self._active_profile_id():
                raise ContextRepositoryError("not_found", "workout history not found", 404)
            if offset >= len(snapshot.entries):
                raise ContextRepositoryError(
                    "validation_error", "history cursor position is invalid", 400
                )
        else:
            snapshot = self._new_snapshot(payload)
            offset = 0

        end = min(offset + payload["limit"], len(snapshot.entries))
        page_refs = snapshot.entries[offset:end]
        items = self._hydrate(snapshot, page_refs)
        next_cursor = self._encode_cursor(snapshot, end) if end < len(snapshot.entries) else None
        return {
            "items": items,
            "next_cursor": next_cursor,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_confirmed_at": snapshot.snapshot_confirmed_at.isoformat(),
            "total_count": len(snapshot.entries),
        }
