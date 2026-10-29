"""Non-runtime E-G06 transaction-order and keyset-cursor regression tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, Lock
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from trainer.domain.measurements import FACTORS, KINDS
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError
from trainer.infrastructure.db.measurements import MeasurementRepository


def _item(day: str, clock: str | None, session_id: str) -> dict[str, Any]:
    return {"measured_on": day, "measured_time": clock, "session_id": session_id}


def _filters() -> dict[str, Any]:
    return {
        "mode": "sessions",
        "view": "current",
        "measurement_kind": None,
        "side": None,
        "date_from": None,
        "date_to": None,
        "limit": 2,
        "cursor": None,
    }


def test_keyset_cursor_survives_correction_of_last_seen_session() -> None:
    ids = [str(uuid4()) for _ in range(4)]
    items = [
        _item("2026-10-10", "12:00:00", ids[0]),
        _item("2026-10-09", "11:00:00", ids[1]),
        _item("2026-10-08", None, ids[2]),
        _item("2026-10-07", None, ids[3]),
    ]
    first = MeasurementRepository._page(items, _filters(), "items")
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = MeasurementRepository._page(
        [items[0], *items[2:]], {**_filters(), "cursor": first["next_cursor"]}, "items"
    )
    assert [item["session_id"] for item in second["items"]] == ids[2:]
    with pytest.raises(ContextRepositoryError, match="invalid measurement cursor"):
        MeasurementRepository._page(
            items, {**_filters(), "view": "history", "cursor": first["next_cursor"]}, "items"
        )


def test_query_sets_repeatable_read_before_any_profile_or_bmi_read() -> None:
    order: list[str] = []

    class Session:
        def execute(self, statement: object) -> None:
            order.append(str(statement))

    repository = MeasurementRepository.__new__(MeasurementRepository)
    repository.session = Session()  # type: ignore[assignment]
    repository._profile = lambda: order.append("profile") or {"profile_id": uuid4()}  # type: ignore[method-assign]
    repository._bmi = lambda _: order.append("bmi") or {"value": "1"}  # type: ignore[method-assign]
    assert repository.query(payload={"mode": "bmi"}) == {"value": "1"}
    assert order == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY", "profile", "bmi"]


def test_concurrent_creates_recheck_duplicates_under_same_profile_lock() -> None:
    lock = Lock()
    first_locked = Event()
    allow_first = Event()
    present: list[str] = []
    typed = {
        "profile_id": "synthetic",
        "measurement_change": "create",
        "measurement_root_id": "root",
        "after": {"values": []},
        "before": None,
        "change_plan": [{"revision_id": "synthetic-session"}],
        "series_impacts": [],
        "duplicates": [],
        "catalog_digest": "same",
    }

    def run(first: bool) -> str:
        repository = MeasurementRepository.__new__(MeasurementRepository)

        def acquire(_: str) -> None:
            lock.acquire()
            if first:
                first_locked.set()
                allow_first.wait(timeout=2)

        repository._lock_profile = acquire  # type: ignore[method-assign]
        repository._catalog_digest = lambda **_: "same"  # type: ignore[method-assign]
        repository._duplicates = lambda *_: list(present)  # type: ignore[method-assign]
        try:
            repository.check_fresh(typed)
            if first:
                present.append("confirmed-session")
            return "confirmed"
        except ContextRepositoryError as error:
            return error.code
        finally:
            lock.release()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, True)
        assert first_locked.wait(timeout=2)
        second = pool.submit(run, False)
        allow_first.set()
        assert first.result() == "confirmed"
        assert second.result() == "stale_preview"


def test_create_draft_root_matches_first_revision_and_preview_uses_same_method() -> None:
    context = Mock()
    context._reserve.return_value = (uuid4(), None)
    context._add_dependency.return_value = {}
    repository = MeasurementRepository(context)
    repository._profile = lambda: {"profile_id": uuid4(), "current_revision_id": uuid4()}
    repository._lock_profile = Mock()
    repository._catalog_digest = Mock(return_value="catalog")
    repository._duplicates = Mock(return_value=[])
    repository._series_members = Mock(return_value=[])
    repository.create_draft(
        payload={
            "intent": "create",
            "request_digest": "a" * 64,
            "session": {
                "measured_on": "2026-10-08",
                "measured_time": None,
                "conditions": None,
                "comment": None,
                "values": [
                    {
                        "measurement_kind": "waist_circumference",
                        "original_value": "90",
                        "original_unit": "centimeter",
                        "side": None,
                        "method": {"mode": "new", "description": "Synthetic waist"},
                        "comment": None,
                    }
                ],
            },
        },
        key="synthetic-create",
        correlation_id=uuid4(),
    )
    typed = json.loads(context.session.execute.call_args_list[1].args[1]["payload"])
    assert typed["measurement_root_id"] == typed["change_plan"][0]["revision_id"]
    assert typed["series_impacts"][0]["method_id"] == typed["after"]["values"][0]["method_id"]
    canonical = ContextRepository._render_preview(typed, detail_level="full")
    assert "отдельный ряд даже при совпадении" in canonical
    assert "90 см" in canonical


@pytest.mark.parametrize("legacy", [False, True])
def test_correction_does_not_claim_loss_of_unknown_or_legacy_series_point(legacy: bool) -> None:
    repository = MeasurementRepository.__new__(MeasurementRepository)
    repository._series_members = Mock(return_value=[])
    old = {
        "measurement_kind": "waist_circumference",
        "side": None,
        "method_id": None,
        "measurement_value_id": "old-value",
        "normalization_status": "legacy_unverified" if legacy else "verified_v1",
    }
    before = {"session_id": "old-session", "values": [old]}
    after = {"values": []}
    impact = repository._series_impacts("profile", before, after, "new-session")[0]
    assert impact["loses_current_point"] is None
    assert impact["prospective_current_points"] == []


@pytest.mark.parametrize(
    "change,code",
    [
        ("catalog", "stale_dependency"),
        ("current", "stale_preview"),
        ("source", "stale_preview"),
        ("series", "stale_preview"),
    ],
)
def test_confirm_rechecks_each_measurement_dependency(change: str, code: str) -> None:
    repository = MeasurementRepository.__new__(MeasurementRepository)
    repository._lock_profile = Mock()
    repository._catalog_digest = Mock(return_value="changed" if change == "catalog" else "same")
    repository._current = Mock(
        side_effect=lambda root, *_args, **_kwargs: (
            "changed"
            if (change == "current" and root == "root")
            or (change == "source" and root == "source-root")
            else "expected"
        )
    )
    repository._duplicates = Mock(return_value=[])
    repository._series_impacts = Mock(return_value=["changed"] if change == "series" else [])
    typed = {
        "profile_id": "profile",
        "catalog_digest": "same",
        "measurement_change": "correct",
        "measurement_root_id": "root",
        "expected_current_session_id": "expected",
        "before": {},
        "after": {
            "values": [
                {
                    "method_source": {
                        "source_root_id": "source-root",
                        "source_session_id": "expected",
                    }
                }
            ]
        },
        "duplicates": [],
        "series_impacts": [],
        "change_plan": [{"revision_id": "new"}],
    }
    with pytest.raises(ContextRepositoryError) as error:
        repository.check_fresh(typed)
    assert error.value.code == code


@pytest.mark.parametrize(
    "mode,phrase",
    [
        ("unknown", "не войдёт в сравнимый ряд"),
        ("new", "отдельный ряд даже при совпадении"),
        ("reuse", "Явно выбран прежний способ"),
    ],
)
def test_preview_explains_modes_conversion_correction_and_duplicate(mode: str, phrase: str) -> None:
    after = {
        "values": [
            {
                "measurement_kind": "waist_circumference",
                "side": None,
                "original_value": "900",
                "original_unit": "millimeter",
                "normalized_value": "90.000000",
                "normalized_unit": "centimeter",
                "method": {"mode": mode},
            }
        ]
    }
    text = "\n".join(
        MeasurementRepository._preview_explanation({"session_id": "old"}, after, ["dup"])
    )
    assert phrase in text and "900 мм" in text and "90.000000 см" in text
    assert "Возможный дубль сеансов: dup" in text and "автоматического объединения нет" in text
    assert "весь текущий сеанс" in text


def test_conversion_rule_changes_catalog_dependency_digest(monkeypatch: Any) -> None:
    units = [
        {"code": code, "description": code, "seed_version": 1, "archived_at": None}
        for code in sorted(FACTORS)
    ]
    kinds = [
        {"code": code, "description": code, "seed_version": 1, "archived_at": None}
        for code in sorted(KINDS)
    ]
    repository = MeasurementRepository.__new__(MeasurementRepository)
    repository.session = Mock()
    repository.session.execute.side_effect = [
        Mock(mappings=lambda: units),
        Mock(mappings=lambda: kinds),
        Mock(mappings=lambda: units),
        Mock(mappings=lambda: kinds),
    ]
    before = repository._catalog_digest()
    monkeypatch.setitem(FACTORS, "inch", Decimal("2.55"))
    assert repository._catalog_digest() != before
