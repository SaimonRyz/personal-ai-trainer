from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.infrastructure.db.context_repository import ContextRepositoryError
from trainer.infrastructure.db.workout_history_snapshots import (
    WorkoutHistorySnapshot,
    WorkoutHistorySnapshotRegistry,
    WorkoutHistorySnapshotRepository,
    WorkoutRevisionRef,
    _history_filter_digest,
)
from trainer.transport.http.contracts import (
    RequestContext,
    WorkoutHistoryRequest,
    WorkoutHistoryResponse,
)


def _context() -> dict[str, str]:
    return {
        "conversation_id": "synthetic-conversation",
        "message_id": "synthetic-message",
        "effective_at": datetime.now(UTC).isoformat(),
        "timezone": "Asia/Novokuznetsk",
    }


def test_qry05_preserves_range_mode_and_accepts_all_confirmed_mode() -> None:
    date_range = WorkoutHistoryRequest(
        context=RequestContext.model_validate(_context()),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 12, 31),
    )
    complete = WorkoutHistoryRequest(
        context=RequestContext.model_validate(_context()), mode="all_confirmed", limit=100
    )

    assert date_range.mode == "range"
    assert complete.mode == "all_confirmed"
    assert complete.date_from is None and complete.date_to is None


@pytest.mark.parametrize(
    "payload",
    [
        {"context": {"purpose": "history"}},
        {
            "context": {"purpose": "history"},
            "date_from": "2026-01-01",
            "mode": "all_confirmed",
        },
    ],
)
def test_qry05_rejects_incomplete_range_or_mixed_modes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WorkoutHistoryRequest.model_validate(payload)


def test_snapshot_response_requires_complete_metadata() -> None:
    with pytest.raises(ValidationError, match="snapshot metadata must be complete"):
        WorkoutHistoryResponse.model_validate(
            {"items": [], "next_cursor": None, "snapshot_id": str(uuid4())}
        )

    empty_snapshot = WorkoutHistoryResponse.model_validate(
        {
            "items": [],
            "next_cursor": None,
            "snapshot_id": str(uuid4()),
            "snapshot_confirmed_at": datetime.now(UTC).isoformat(),
            "total_count": 0,
        }
    )
    assert empty_snapshot.total_count == 0


def test_snapshot_cursor_is_filter_bound_and_rejects_tampering() -> None:
    payload = {"mode": "all_confirmed", "exercise_id": None, "lifecycle": ["completed"]}
    snapshot = WorkoutHistorySnapshot(
        snapshot_id=str(uuid4()),
        profile_id=str(uuid4()),
        filter_digest=_history_filter_digest(payload),
        snapshot_confirmed_at=datetime.now(UTC),
        entries=(),
        expires_at=9999999999,
    )
    cursor = WorkoutHistorySnapshotRepository._encode_cursor(snapshot, 10)

    assert WorkoutHistorySnapshotRepository._decode_cursor(cursor, snapshot.filter_digest) == (
        snapshot.snapshot_id,
        10,
    )
    with pytest.raises(ContextRepositoryError, match="cursor is invalid"):
        WorkoutHistorySnapshotRepository._decode_cursor(cursor, "0" * 64)


def test_all_confirmed_cursor_keeps_snapshot_when_page_limit_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filters: dict[str, Any] = {"mode": "all_confirmed", "exercise_id": None, "lifecycle": []}
    profile_id = str(uuid4())
    registry = WorkoutHistorySnapshotRegistry()
    snapshot = registry.create(
        profile_id=profile_id,
        filter_digest=_history_filter_digest(filters),
        snapshot_confirmed_at=datetime.now(UTC),
        entries=[
            WorkoutRevisionRef(
                session_id=str(uuid4()),
                revision_id=str(uuid4()),
                revision_no=index,
                local_start_date=f"2026-10-{index:02d}",
                current=True,
            )
            for index in range(1, 6)
        ],
    )
    repository = WorkoutHistorySnapshotRepository.__new__(WorkoutHistorySnapshotRepository)
    repository._registry = registry
    monkeypatch.setattr(repository, "_active_profile_id", lambda: profile_id)
    monkeypatch.setattr(repository, "_new_snapshot", lambda _: snapshot)
    monkeypatch.setattr(
        repository,
        "_hydrate",
        lambda _snapshot, refs: [{"revision_no": ref.revision_no} for ref in refs],
    )

    first = repository.query({**filters, "limit": 2})
    second = repository.query({**filters, "limit": 1, "cursor": first["next_cursor"]})
    third = repository.query({**filters, "limit": 3, "cursor": second["next_cursor"]})

    assert [item["revision_no"] for page in (first, second, third) for item in page["items"]] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert [page["snapshot_id"] for page in (first, second, third)] == [snapshot.snapshot_id] * 3
    assert [page["snapshot_confirmed_at"] for page in (first, second, third)] == [
        snapshot.snapshot_confirmed_at.isoformat()
    ] * 3
    assert [page["total_count"] for page in (first, second, third)] == [5, 5, 5]
    assert third["next_cursor"] is None


def test_snapshot_registry_expires_and_bounds_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "trainer.infrastructure.db.workout_history_snapshots.time.monotonic", lambda: now[0]
    )
    registry = WorkoutHistorySnapshotRegistry(ttl_seconds=10, max_snapshots=1)
    profile_id = str(uuid4())
    first = registry.create(
        profile_id=profile_id,
        filter_digest="a" * 64,
        snapshot_confirmed_at=datetime.now(UTC),
        entries=[],
    )
    assert registry.get(first.snapshot_id) is not None
    second = registry.create(
        profile_id=profile_id,
        filter_digest="a" * 64,
        snapshot_confirmed_at=datetime.now(UTC),
        entries=[],
    )
    assert registry.get(first.snapshot_id) is None
    assert registry.get(second.snapshot_id) is not None
    now[0] += 11
    assert registry.get(second.snapshot_id) is None


def test_expired_snapshot_cursor_fails_closed_with_http_410() -> None:
    payload: dict[str, Any] = {
        "mode": "all_confirmed",
        "exercise_id": None,
        "lifecycle": [],
        "cursor": "",
        "limit": 1,
    }
    snapshot = WorkoutHistorySnapshot(
        snapshot_id=str(uuid4()),
        profile_id=str(uuid4()),
        filter_digest=_history_filter_digest(payload),
        snapshot_confirmed_at=datetime.now(UTC),
        entries=(),
        expires_at=9999999999,
    )
    payload["cursor"] = WorkoutHistorySnapshotRepository._encode_cursor(snapshot, 1)
    repository = WorkoutHistorySnapshotRepository.__new__(WorkoutHistorySnapshotRepository)
    repository._registry = WorkoutHistorySnapshotRegistry()

    with pytest.raises(ContextRepositoryError, match="snapshot expired") as error:
        repository.query(payload)
    assert error.value.code == "history_snapshot_expired"
    assert error.value.status_code == 410
