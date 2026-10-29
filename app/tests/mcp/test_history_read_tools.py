from __future__ import annotations

from datetime import date
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from trainer_mcp import server
from trainer_mcp.contracts import (
    MeasurementBmiInput,
    MeasurementSeriesInput,
    MeasurementSessionsInput,
    ProgressQueryInput,
    RecordCandidatesInput,
    RecordsInput,
    WorkoutHistoryInput,
)

from trainer.transport.http.contracts import (
    RecordCandidatesRequest,
    RecordQueryRequest,
    WorkoutHistoryRequest,
)
from trainer.transport.http.measurement_contracts import MeasurementQueryRequest
from trainer.transport.http.progress_contracts import ProgressQueryRequest


def _invoke(name: str, **kwargs: Any) -> dict[str, Any]:
    tool = next(item for item in server.mcp._tool_manager.list_tools() if item.name == name)
    return cast(dict[str, Any], tool.fn(**kwargs))


def _base_args(query: Any) -> dict[str, Any]:
    return {
        "conversation_id": "synthetic-conversation",
        "message_id": "synthetic-message",
        "timezone": "Asia/Novokuznetsk",
        "query": query,
        "correlation_id": uuid4(),
        "causation_id": uuid4(),
    }


def _capture(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    captured: dict[str, Any] = {}
    envelope = {"data": {"items": []}, "meta": {"api_version": "v1"}}

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: Any,
        causation_id: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captured.update(
            path=path,
            body=body,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )
        return envelope

    monkeypatch.setattr(server, "api_call", fake_api_call)
    return captured, envelope


def _validate_request(captured: dict[str, Any], model: Any) -> None:
    model.model_validate(captured["body"])


def test_workout_history_tool_matches_qry05_and_preserves_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, envelope = _capture(monkeypatch)
    next_cursor = "synthetic-next-page-token"
    envelope["data"]["next_cursor"] = next_cursor
    result = _invoke(
        "get_workout_history",
        **_base_args(
            WorkoutHistoryInput(
                date_from=date(2026, 10, 1),
                date_to=date(2026, 10, 10),
                lifecycle=["completed", "interrupted"],
                limit=25,
                cursor="synthetic-request-cursor",
            )
        ),
    )
    assert captured["path"] == "/api/v1/queries/workouts/history"
    _validate_request(captured, WorkoutHistoryRequest)
    assert captured["body"]["cursor"] == "synthetic-request-cursor"
    assert result["data"]["next_cursor"] == next_cursor
    assert result is envelope


def test_workout_history_tool_passes_all_confirmed_mode_without_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, envelope = _capture(monkeypatch)
    result = _invoke(
        "get_workout_history",
        **_base_args(WorkoutHistoryInput(mode="all_confirmed", limit=100)),
    )
    assert captured["path"] == "/api/v1/queries/workouts/history"
    _validate_request(captured, WorkoutHistoryRequest)
    assert captured["body"]["mode"] == "all_confirmed"
    assert captured["body"]["date_from"] is None
    assert captured["body"]["date_to"] is None
    assert result is envelope


def test_all_confirmed_continuation_forwards_exact_snapshot_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, envelope = _capture(monkeypatch)
    cursor = "opaque-synthetic-snapshot-cursor"
    snapshot_id = "10000000-0000-4000-8000-000000000005"
    envelope["data"].update(
        next_cursor=cursor,
        snapshot_id=snapshot_id,
        total_count=2,
    )

    result = _invoke(
        "get_workout_history",
        **_base_args(
            WorkoutHistoryInput(
                mode="all_confirmed",
                limit=100,
                cursor=cursor,
            )
        ),
    )

    assert captured["path"] == "/api/v1/queries/workouts/history"
    _validate_request(captured, WorkoutHistoryRequest)
    assert captured["body"]["mode"] == "all_confirmed"
    assert captured["body"]["cursor"] == cursor
    assert result["data"]["snapshot_id"] == snapshot_id
    assert result["data"]["next_cursor"] == cursor
    assert result is envelope


def test_workout_history_input_rejects_missing_range_dates_and_mixed_all_history_dates() -> None:
    with pytest.raises(ValidationError, match="date_from and date_to are required"):
        WorkoutHistoryInput()
    with pytest.raises(ValidationError, match="date filters are not supported"):
        WorkoutHistoryInput(
            mode="all_confirmed",
            date_from=date(2026, 10, 1),
        )


def test_progress_tool_matches_qry06_without_persisting_read_time_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, envelope = _capture(monkeypatch)
    result = _invoke(
        "get_progress",
        **_base_args(
            ProgressQueryInput(
                metric_kind="external_working_set_volume",
                date_from=date(2026, 10, 1),
                date_to=date(2026, 10, 10),
                include_history=False,
            )
        ),
    )
    assert captured["path"] == "/api/v1/queries/progress"
    ProgressQueryRequest.model_validate(captured["body"])
    assert result is envelope
    with pytest.raises(ValidationError, match="no comparison condition"):
        ProgressQueryInput(
            metric_kind="external_working_set_volume",
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 10),
            condition_id=uuid4(),
        )


def test_record_read_tools_match_qry07_and_qry12(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, envelope = _capture(monkeypatch)
    candidate_result = _invoke(
        "get_record_candidates",
        **_base_args(RecordCandidatesInput(status="pending", limit=10)),
    )
    assert captured["path"] == "/api/v1/queries/record-candidates"
    _validate_request(captured, RecordCandidatesRequest)
    assert candidate_result is envelope

    records_result = _invoke(
        "get_records",
        **_base_args(RecordsInput(view="history", limit=12)),
    )
    assert captured["path"] == "/api/v1/queries/records"
    _validate_request(captured, RecordQueryRequest)
    assert records_result is envelope


@pytest.mark.parametrize(
    "query",
    [
        MeasurementSessionsInput(
            mode="sessions",
            view="current",
            date_from=None,
            date_to=None,
            measurement_kind="body_mass",
            side=None,
        ),
        MeasurementSeriesInput(
            mode="series",
            measurement_kind="waist_circumference",
            side=None,
            method_source_value_id=uuid4(),
            date_from=date(2026, 1, 1),
            date_to=date(2026, 10, 10),
        ),
        MeasurementBmiInput(mode="bmi"),
    ],
)
def test_measurement_tool_matches_qry13_union(
    monkeypatch: pytest.MonkeyPatch,
    query: Any,
) -> None:
    captured, envelope = _capture(monkeypatch)
    result = _invoke("get_body_measurements", **_base_args(query))
    assert captured["path"] == "/api/v1/queries/measurements"
    TypeAdapter(MeasurementQueryRequest).validate_python(captured["body"])
    assert result is envelope


def test_measurement_inputs_reject_incompatible_side_and_method() -> None:
    with pytest.raises(ValidationError, match="side requires paired"):
        MeasurementSessionsInput(
            mode="sessions",
            view="current",
            date_from=None,
            date_to=None,
            measurement_kind="height",
            side="left",
        )
    with pytest.raises(ValidationError, match="circumference requires exact method source"):
        MeasurementSeriesInput(
            mode="series",
            measurement_kind="waist_circumference",
            side=None,
            method_source_value_id=None,
            date_from=None,
            date_to=None,
        )
