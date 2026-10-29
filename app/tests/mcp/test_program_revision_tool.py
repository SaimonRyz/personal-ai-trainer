from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import TypeAdapter
from trainer_mcp import server

from trainer.application.context import ContextService
from trainer.transport.http.contracts import (
    DraftRevisionRequest,
    ProgramDraftRevisionRequest,
    WorkoutDraftRevisionRequest,
    WorkoutReportDraftRequest,
)


def test_program_revision_uses_exact_route_and_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    draft_id, base_id, goal_id = uuid4(), uuid4(), uuid4()
    seen: list[httpx.Request] = []
    response = {"data": {"revision_id": str(uuid4())}, "meta": {"api_version": "v1"}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == f"/api/v1/commands/drafts/{draft_id}/revisions"
        assert request.headers["Idempotency-Key"] == "synthetic-revision"
        body = json.loads(request.content)
        ProgramDraftRevisionRequest.model_validate(body)
        ContextService.verify_digest(body, body["request_digest"])
        assert body["base_revision_id"] == str(base_id)
        assert body["changes"] == [
            {"op": "replace", "path": "/goal_revision_id", "value": str(goal_id)}
        ]
        return httpx.Response(200, json=response)

    original = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    tool = next(
        t for t in server.mcp._tool_manager.list_tools() if t.name == "revise_program_draft"
    )
    result = asyncio.run(
        tool.run(
            {
                "conversation_id": "synthetic",
                "message_id": "synthetic",
                "timezone": "UTC",
                "draft_id": str(draft_id),
                "base_revision_id": str(base_id),
                "changes": [{"op": "replace", "path": "/goal_revision_id", "value": str(goal_id)}],
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "idempotency_key": "synthetic-revision",
            }
        )
    )
    assert result == response
    assert len(seen) == 1


def test_workout_report_revision_uses_same_route_and_closed_full_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    draft_id, base_id = uuid4(), uuid4()
    response = {"data": {"draft_id": str(draft_id)}, "meta": {"api_version": "v1"}}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == f"/api/v1/commands/drafts/{draft_id}/revisions"
        body = json.loads(request.content)
        WorkoutDraftRevisionRequest.model_validate(body)
        TypeAdapter(DraftRevisionRequest).validate_python(body)
        ContextService.verify_digest(body, body["request_digest"])
        assert body["base_revision_id"] == str(base_id)
        assert len(body["changes"]) == 1
        assert body["changes"][0]["path"] == "/report"
        return httpx.Response(200, json=response)

    original = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    tool = next(
        t for t in server.mcp._tool_manager.list_tools() if t.name == "revise_workout_report_draft"
    )
    result = asyncio.run(
        tool.run(
            {
                "conversation_id": "synthetic",
                "message_id": "synthetic",
                "timezone": "UTC",
                "draft_id": str(draft_id),
                "base_revision_id": str(base_id),
                "change": {
                    "op": "replace",
                    "path": "/report",
                    "value": {
                        "plan_id": None,
                        "plan_revision_id": None,
                        "started_at": None,
                        "ended_at": None,
                        "local_start_date": "2026-10-11",
                        "duration_seconds": None,
                        "session_type": "training",
                        "name": None,
                        "lifecycle": "completed",
                        "effort": None,
                        "comment": None,
                        "workout_metrics": [],
                        "blocks": [],
                        "confirmed_unknown_planned_exercise_ids": [],
                        "confirmed_unknown_planned_set_ids": [],
                        "confirmed_unknown_planned_load_component_ids": [],
                        "confirmed_unknown_metrics": [],
                    },
                },
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "idempotency_key": "synthetic-workout-revision",
            }
        )
    )
    assert result == response
    assert len(seen) == 1


def test_workout_report_create_uses_existing_cmd06_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    response = {"data": {"draft_id": str(uuid4())}, "meta": {"api_version": "v1"}}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/api/v1/commands/workout-report-drafts"
        body = json.loads(request.content)
        WorkoutReportDraftRequest.model_validate(body)
        ContextService.verify_digest(body, body["request_digest"])
        return httpx.Response(200, json=response)

    original = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    tool = next(
        t for t in server.mcp._tool_manager.list_tools() if t.name == "create_workout_report_draft"
    )
    result = asyncio.run(
        tool.run(
            {
                "conversation_id": "synthetic",
                "message_id": "synthetic",
                "timezone": "UTC",
                "report": {
                    "plan_id": None,
                    "plan_revision_id": None,
                    "started_at": None,
                    "ended_at": None,
                    "local_start_date": "2026-10-11",
                    "duration_seconds": None,
                    "session_type": "training",
                    "name": None,
                    "lifecycle": "completed",
                    "effort": None,
                    "comment": None,
                    "workout_metrics": [],
                    "blocks": [],
                    "confirmed_unknown_planned_exercise_ids": [],
                    "confirmed_unknown_planned_set_ids": [],
                    "confirmed_unknown_planned_load_component_ids": [],
                    "confirmed_unknown_metrics": [],
                },
                "correlation_id": str(uuid4()),
                "causation_id": str(uuid4()),
                "idempotency_key": "synthetic-workout-create",
            }
        )
    )
    assert result == response
    assert len(seen) == 1


def test_workout_revision_contract_rejects_multiple_or_nonreport_changes() -> None:
    from pydantic import ValidationError

    base = {
        "context": {
            "conversation_id": "synthetic",
            "message_id": "synthetic",
            "effective_at": "2026-10-11T12:00:00Z",
            "timezone": "UTC",
        },
        "request_digest": "0" * 64,
        "base_revision_id": str(uuid4()),
        "changes": [
            {
                "op": "replace",
                "path": "/report",
                "value": {"session_type": "training"},
            }
        ],
    }
    invalid_change = {"op": "replace", "path": "/intent", "value": base["changes"]}
    duplicated_changes = [
        {
            "op": "replace",
            "path": "/report",
            "value": {"session_type": "training"},
        },
        {
            "op": "replace",
            "path": "/report",
            "value": {"session_type": "training"},
        },
    ]
    for changes in ([invalid_change], duplicated_changes):
        with pytest.raises(ValidationError):
            WorkoutDraftRevisionRequest.model_validate({**base, "changes": changes})


@pytest.mark.parametrize(
    "suffix",
    ["anything/revisions", "../revisions", "00000000-0000-4000-8000-000000000001/revisions/extra"],
)
def test_revision_route_rejects_nonexact_paths(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    with pytest.raises(RuntimeError, match="not allowlisted"):
        server.api_call(f"/api/v1/commands/drafts/{suffix}", {}, uuid4(), uuid4())


@pytest.mark.parametrize("path", ["/report", "/intent", "/program_id", "/version/label"])
def test_program_revision_rejects_nonprogram_paths(path: str) -> None:
    from pydantic import ValidationError
    from trainer_mcp.write_tools import ProgramDraftReplacement

    with pytest.raises(ValidationError):
        ProgramDraftReplacement.model_validate(
            {"op": "replace", "path": path, "value": str(uuid4())}
        )
