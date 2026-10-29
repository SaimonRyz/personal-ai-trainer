from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from g04_app import app as g04_fixture_app
from g04_app import empty_query_response
from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession
from trainer.transport.http.contracts import (
    RecordCandidatesResponse,
    RecordResultsResponse,
    WorkoutHistoryResponse,
)
from trainer.transport.http.measurement_contracts import (
    BMIResponse,
    SeriesResponse,
    SessionsResponse,
)
from trainer.transport.http.progress_contracts import ProgressQueryResponse

PROGRESS_QUERY = {
    "metric_kind": "external_working_set_volume",
    "date_from": "2026-10-01",
    "date_to": "2026-10-10",
    "exercise_id": "20000000-0000-4000-8000-000000000051",
    "condition_id": None,
    "include_history": False,
    "limit": 50,
    "cursor": None,
}
PROGRESS_RESPONSE = empty_query_response("/api/v1/queries/progress", PROGRESS_QUERY)
CANDIDATES_QUERY = {
    "exercise_id": None,
    "metric_kind": None,
    "status": "pending",
    "limit": 10,
    "cursor": None,
}
CANDIDATES_RESPONSE = empty_query_response("/api/v1/queries/record-candidates", CANDIDATES_QUERY)
RECORDS_QUERY = {
    "exercise_id": None,
    "metric_kind": None,
    "condition_id": None,
    "view": "history",
    "limit": 12,
    "cursor": None,
}
RECORDS_RESPONSE = empty_query_response("/api/v1/queries/records", RECORDS_QUERY)
MEASUREMENT_SESSIONS_QUERY = {
    "mode": "sessions",
    "view": "current",
    "date_from": None,
    "date_to": None,
    "measurement_kind": "body_mass",
    "side": None,
    "limit": 50,
    "cursor": None,
}
MEASUREMENT_SESSIONS_RESPONSE = empty_query_response(
    "/api/v1/queries/measurements", MEASUREMENT_SESSIONS_QUERY
)

QUERY_CASES = [
    pytest.param(
        "get_workout_history",
        {
            "mode": "range",
            "date_from": "2026-10-01",
            "date_to": "2026-10-10",
            "exercise_id": None,
            "lifecycle": ["completed", "interrupted"],
            "limit": 25,
            "cursor": None,
        },
        {
            "items": [
                {
                    "session_ref": {
                        "object_type": "workout_session",
                        "object_id": "20000000-0000-4000-8000-000000000030",
                        "revision_id": "20000000-0000-4000-8000-000000000031",
                    },
                    "current": True,
                    "supersedes_revision_ref": None,
                    "local_start_date": "2026-10-10",
                    "lifecycle": "completed",
                    "confirmed_at": "2026-10-10T09:30:00+07:00",
                    "confirmation_ref": {
                        "object_type": "confirmation",
                        "object_id": "20000000-0000-4000-8000-000000000032",
                        "revision_id": None,
                    },
                    "reason": None,
                    "plan_ref": {
                        "object_type": "plan",
                        "object_id": "20000000-0000-4000-8000-000000000033",
                        "revision_id": "20000000-0000-4000-8000-000000000034",
                    },
                    "plan_content_digest": "a" * 64,
                    "workout": {
                        "local_start_date": "2026-10-10",
                        "lifecycle": "completed",
                        "comment": "synthetic history item",
                    },
                    "provenance": [
                        {
                            "kind": "exercise_revision",
                            "ref": {
                                "object_type": "exercise",
                                "object_id": "20000000-0000-4000-8000-000000000039",
                                "revision_id": "20000000-0000-4000-8000-000000000046",
                            },
                            "planned_exercise_id": "20000000-0000-4000-8000-000000000040",
                        },
                        {
                            "path": "/comment",
                            "kind": "user_reported",
                            "source_revision_id": "20000000-0000-4000-8000-000000000031",
                        },
                    ],
                }
            ],
            "next_cursor": None,
        },
        id="qry05-history",
    ),
    pytest.param(
        "get_progress",
        PROGRESS_QUERY,
        PROGRESS_RESPONSE,
        id="qry06-progress",
    ),
    pytest.param(
        "get_record_candidates",
        CANDIDATES_QUERY,
        CANDIDATES_RESPONSE,
        id="qry07-candidates",
    ),
    pytest.param(
        "get_records",
        RECORDS_QUERY,
        RECORDS_RESPONSE,
        id="qry12-records",
    ),
    pytest.param(
        "get_body_measurements",
        MEASUREMENT_SESSIONS_QUERY,
        MEASUREMENT_SESSIONS_RESPONSE,
        id="qry13-sessions",
    ),
    pytest.param(
        "get_body_measurements",
        {
            "mode": "series",
            "measurement_kind": "waist_circumference",
            "side": None,
            "method_source_value_id": "20000000-0000-4000-8000-000000000020",
            "date_from": "2026-01-01",
            "date_to": "2026-10-10",
            "limit": 50,
            "cursor": None,
        },
        {
            "points": [
                {
                    "session_id": "20000000-0000-4000-8000-000000000022",
                    "measurement_value_id": "20000000-0000-4000-8000-000000000020",
                    "measured_on": "2026-10-10",
                    "measured_time": None,
                    "original_value": "84.2",
                    "original_unit": "centimeter",
                    "normalized_value": "84.2",
                    "normalized_unit": "centimeter",
                    "conversion_version": "body_units_v1",
                    "side": None,
                    "method_description": "synthetic tape at navel",
                    "method_id": "20000000-0000-4000-8000-000000000021",
                }
            ],
            "next_cursor": None,
        },
        id="qry13-series",
    ),
    pytest.param(
        "get_body_measurements",
        {"mode": "bmi"},
        None,
        id="qry13-bmi-insufficient-data",
    ),
]


@pytest.mark.unit
def test_qry05_synthetic_fixture_contains_complete_nested_workout_tree() -> None:
    with TestClient(g04_fixture_app) as client:
        response = client.post(
            "/api/v1/queries/workouts/history",
            headers={
                "authorization": "Bearer spike-local-token",
                "accept": "application/json",
                "content-type": "application/json",
                "x-correlation-id": str(uuid4()),
                "x-causation-id": str(uuid4()),
            },
            json={
                "context": {
                    "conversation_id": "synthetic-g04-fixture",
                    "message_id": "synthetic-qry05",
                    "timezone": "Asia/Novokuznetsk",
                },
                "date_from": "2026-10-01",
                "date_to": "2026-10-10",
                "limit": 25,
            },
        )

    assert response.status_code == 200, response.text
    history = WorkoutHistoryResponse.model_validate(response.json()["data"]["response"])
    assert len(history.items) == 1
    workout = history.items[0].workout
    assert workout["duration_seconds"] == 3600
    assert workout["session_type"] == "training"
    assert workout["report_gaps"] == []
    assert len(workout["blocks"]) == 1
    block = workout["blocks"][0]
    assert block["block_type"] == "superset"
    assert len(block["exercises"]) == 1
    exercise = block["exercises"][0]
    assert exercise["result"] == "performed"
    assert len(exercise["sets"]) == 1
    actual_set = exercise["sets"][0]
    assert actual_set["full_reps"] == 8
    assert actual_set["metrics"][0]["metric_kind"] == "repetitions"
    assert actual_set["loads"][0]["mass"] == "20.000000"
    assert actual_set["loads"][0]["equipment_snapshot"] == {"name": "Synthetic barbell"}


@pytest.mark.unit
def test_qry05_synthetic_fixture_marks_all_confirmed_snapshot_complete() -> None:
    with TestClient(g04_fixture_app) as client:
        response = client.post(
            "/api/v1/queries/workouts/history",
            headers={
                "authorization": "Bearer spike-local-token",
                "accept": "application/json",
                "content-type": "application/json",
                "x-correlation-id": str(uuid4()),
                "x-causation-id": str(uuid4()),
            },
            json={
                "context": {
                    "conversation_id": "synthetic-g04-fixture",
                    "message_id": "synthetic-qry05-all-confirmed",
                    "timezone": "Asia/Novokuznetsk",
                },
                "mode": "all_confirmed",
                "limit": 100,
            },
        )

    assert response.status_code == 200, response.text
    history = WorkoutHistoryResponse.model_validate(response.json()["data"]["response"])
    assert str(history.snapshot_id) == "20000000-0000-4000-8000-000000000099"
    assert history.snapshot_confirmed_at is not None
    assert history.total_count == len(history.items) == 1


@pytest.mark.unit
def test_qry05_synthetic_fixture_supports_complete_two_page_snapshot() -> None:
    base_request = {
        "context": {
            "conversation_id": "synthetic-g04-fixture",
            "message_id": "synthetic-qry05-two-pages",
            "timezone": "Asia/Novokuznetsk",
        },
        "mode": "all_confirmed",
        "limit": 1,
    }
    headers = {
        "authorization": "Bearer spike-local-token",
        "accept": "application/json",
        "content-type": "application/json",
        "x-correlation-id": str(uuid4()),
        "x-causation-id": str(uuid4()),
    }
    with TestClient(g04_fixture_app) as client:
        first_response = client.post(
            "/api/v1/queries/workouts/history", headers=headers, json=base_request
        )
        first_page = WorkoutHistoryResponse.model_validate(
            first_response.json()["data"]["response"]
        )
        second_request = {**base_request, "cursor": first_page.next_cursor}
        second_response = client.post(
            "/api/v1/queries/workouts/history", headers=headers, json=second_request
        )
        second_page = WorkoutHistoryResponse.model_validate(
            second_response.json()["data"]["response"]
        )

    assert first_response.status_code == second_response.status_code == 200
    assert first_page.next_cursor == "synthetic-all-confirmed-page-2"
    assert second_page.next_cursor is None
    assert first_page.snapshot_id == second_page.snapshot_id
    assert first_page.snapshot_confirmed_at == second_page.snapshot_confirmed_at
    assert first_page.total_count == second_page.total_count == 2
    refs = {
        (item.session_ref.object_id, item.session_ref.revision_id)
        for item in [*first_page.items, *second_page.items]
    }
    assert len(refs) == 2


@pytest.mark.spike
@pytest.mark.asyncio
async def test_qry05_mcp_streamable_http_continues_complete_snapshot(
    g04_mcp_url: str,
) -> None:
    async with (
        streamable_http_client(g04_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        page_results = []
        query: dict[str, object] = {
            "mode": "all_confirmed",
            "date_from": None,
            "date_to": None,
            "exercise_id": None,
            "lifecycle": [],
            "limit": 1,
            "cursor": None,
        }
        for page_no in range(2):
            result = await session.call_tool(
                "get_workout_history",
                arguments={
                    "conversation_id": "synthetic-g04-multipage",
                    "message_id": f"synthetic-qry05-page-{page_no + 1}",
                    "timezone": "Asia/Novokuznetsk",
                    "correlation_id": str(uuid4()),
                    "causation_id": str(uuid4()),
                    "query": query,
                },
            )
            assert result.isError is False
            assert result.structuredContent is not None
            data = result.structuredContent["data"]
            assert data["synthetic"] is True
            assert data["filters"] == query
            page_results.append(WorkoutHistoryResponse.model_validate(data["response"]))
            query = {**query, "cursor": page_results[-1].next_cursor}

    first_page, second_page = page_results
    assert first_page.next_cursor == "synthetic-all-confirmed-page-2"
    assert second_page.next_cursor is None
    assert first_page.snapshot_id == second_page.snapshot_id
    assert first_page.snapshot_confirmed_at == second_page.snapshot_confirmed_at
    assert first_page.total_count == second_page.total_count == 2
    refs = {
        (item.session_ref.object_id, item.session_ref.revision_id)
        for item in [*first_page.items, *second_page.items]
    }
    assert len(refs) == first_page.total_count


@pytest.mark.spike
@pytest.mark.asyncio
async def test_g04_candidate_is_not_conflated_with_confirmed_current_record(
    g04_mcp_url: str,
) -> None:
    async with (
        streamable_http_client(g04_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        pending_result = await session.call_tool(
            "get_record_candidates",
            arguments={
                "conversation_id": "synthetic-g04-record-distinction",
                "message_id": "synthetic-pending-candidates",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "query": CANDIDATES_QUERY,
            },
        )
        records_result = await session.call_tool(
            "get_records",
            arguments={
                "conversation_id": "synthetic-g04-record-distinction",
                "message_id": "synthetic-confirmed-records",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "query": RECORDS_QUERY,
            },
        )

    assert pending_result.isError is False
    assert pending_result.structuredContent is not None
    assert records_result.isError is False
    assert records_result.structuredContent is not None
    pending_data = pending_result.structuredContent["data"]
    records_data = records_result.structuredContent["data"]
    assert pending_data["synthetic"] is records_data["synthetic"] is True
    pending_response = RecordCandidatesResponse.model_validate(pending_data["response"])
    records_response = RecordResultsResponse.model_validate(records_data["response"])
    assert len(pending_response.items) == len(records_response.items) == 1
    candidate = pending_response.items[0]
    record = records_response.items[0]
    assert candidate.status == "pending"
    assert candidate.eligible_now is True
    assert candidate.current_record_ref is None
    assert candidate.candidate_value.exact_value == 10
    assert candidate.achieved_on.isoformat() == "2026-10-10"
    assert record.is_current is True
    assert record.validity == "valid"
    assert record.value.exact_value == 9
    assert record.achieved_on.isoformat() == "2026-10-03"
    assert record.source_candidate_ref is not None
    assert record.source_candidate_ref.object_id != candidate.candidate_id
    assert record.source_session_ref is not None
    assert record.source_set_ref is not None
    assert record.source_session_ref.object_id != candidate.source_session_ref.object_id
    assert record.source_set_ref.object_id != candidate.source_set_ref.object_id


@pytest.mark.spike
@pytest.mark.asyncio
async def test_qry13_bmi_mcp_preserves_confirmed_sources_from_different_dates(
    g04_mcp_url: str,
) -> None:
    async with (
        streamable_http_client(g04_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "get_body_measurements",
            arguments={
                "conversation_id": "synthetic-g04-bmi-positive",
                "message_id": "synthetic-qry13-bmi-mixed-dates",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "query": {"mode": "bmi"},
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    data = result.structuredContent["data"]
    assert data["synthetic"] is True
    assert data["filters"] == {"mode": "bmi"}
    bmi = BMIResponse.model_validate(data["response"])
    assert bmi.value == "24.691358"
    assert bmi.unit_code == "kg_per_m2"
    assert bmi.method == "bmi_latest_confirmed_height_v1"
    assert bmi.body_mass.normalized_value == "80.0"
    assert bmi.body_mass.measured_on.isoformat() == "2026-10-10"
    assert bmi.height.normalized_value == "1.8"
    assert bmi.height.measured_on.isoformat() == "2025-01-15"
    assert bmi.body_mass.measured_on != bmi.height.measured_on


@pytest.mark.spike
@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "query", "expected_response"), QUERY_CASES)
async def test_g04_read_tool_passes_filters_and_api_envelope_unchanged(
    g04_mcp_url: str,
    tool_name: str,
    query: dict[str, object],
    expected_response: dict[str, object] | None,
) -> None:
    async with (
        streamable_http_client(g04_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            tool_name,
            arguments={
                "conversation_id": "synthetic-g04-read",
                "message_id": f"synthetic-{tool_name}",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "query": query,
            },
        )

    assert result.isError is False
    assert result.structuredContent is not None
    if expected_response is None:
        assert "error" in result.structuredContent
        assert result.structuredContent["error"]["code"] == "insufficient_data"
        assert "data" not in result.structuredContent
        return

    data = result.structuredContent["data"]
    assert data["synthetic"] is True
    assert data["filters"] == query
    if tool_name == "get_workout_history":
        response = data["response"]
        assert response == empty_query_response("/api/v1/queries/workouts/history", query)
        history = WorkoutHistoryResponse.model_validate(response)
        assert response["next_cursor"] is None
        assert len(history.items) == 1
        item = history.items[0]
        assert item.workout["duration_seconds"] == 3600
        assert item.workout["session_type"] == "training"
        blocks = item.workout["blocks"]
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == "superset"
        exercises = blocks[0]["exercises"]
        assert len(exercises) == 1
        sets = exercises[0]["sets"]
        assert len(sets) == 1
        assert sets[0]["full_reps"] == 8
        assert sets[0]["metrics"][0]["value"] == "8.000000"
        assert sets[0]["loads"][0]["mass"] == "20.000000"
        assert sets[0]["loads"][0]["equipment_snapshot"] == {"name": "Synthetic barbell"}
        expected_item = dict(expected_response["items"][0])
        expected_item.pop("workout")
        actual_item = dict(response["items"][0])
        actual_item.pop("workout")
        assert actual_item == expected_item
    elif tool_name == "get_progress":
        response = data["response"]
        assert response == PROGRESS_RESPONSE
        progress = ProgressQueryResponse.model_validate(response)
        assert progress.comparison_status == "two_points"
        assert len(progress.items) == 2
        assert [item.point.achieved_on.isoformat() for item in progress.items] == [
            "2026-10-02",
            "2026-10-09",
        ]
        assert [str(item.point.value.exact_value) for item in progress.items] == ["160", "180"]
        assert all(item.kind == "derived" for item in progress.items)
        assert all(item.point.state == "ephemeral" for item in progress.items)
        assert all(item.point.derived_value_ref is None for item in progress.items)
    elif tool_name == "get_record_candidates":
        response = data["response"]
        assert response == CANDIDATES_RESPONSE
        candidates = RecordCandidatesResponse.model_validate(response)
        assert len(candidates.items) == 1
        candidate = candidates.items[0]
        assert candidate.status == "pending"
        assert candidate.eligible_now is True
        assert candidate.metric_kind == "record_full_reps"
        assert candidate.candidate_value.exact_value == 10
        assert candidate.source_set_ref.revision_id is not None
    elif tool_name == "get_records":
        response = data["response"]
        assert response == RECORDS_RESPONSE
        records = RecordResultsResponse.model_validate(response)
        assert len(records.items) == 1
        record = records.items[0]
        assert record.is_current is True
        assert record.is_historical_import is False
        assert record.validity == "valid"
        assert record.source_session_ref is not None
        assert record.source_set_ref is not None
        assert record.source_candidate_ref is not None
    elif tool_name == "get_body_measurements" and query["mode"] == "sessions":
        response = data["response"]
        assert response == MEASUREMENT_SESSIONS_RESPONSE
        sessions = SessionsResponse.model_validate(response)
        assert len(sessions.items) == 1
        session = sessions.items[0]
        assert session.current is True
        assert session.measured_on.isoformat() == "2026-10-10"
        assert session.values[0].measurement_kind == "body_mass"
        assert session.values[0].normalized_value == "80.0"
        assert session.values[0].normalization_status == "verified_v1"
    elif tool_name == "get_body_measurements" and query["mode"] == "series":
        response = data["response"]
        assert response == expected_response
        series = SeriesResponse.model_validate(response)
        assert len(series.points) == 1
        point = series.points[0]
        assert point.method_id is not None
        assert str(point.measurement_value_id) == query["method_source_value_id"]
        assert point.normalized_value == "84.2"
        assert point.normalized_unit == "centimeter"
    else:
        assert data["response"] == expected_response
