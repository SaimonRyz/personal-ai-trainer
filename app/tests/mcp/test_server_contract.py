from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from trainer_mcp import server
from trainer_mcp.contracts import (
    ContextEquipmentInput,
    ExerciseCatalogInput,
    ExerciseRevisionRef,
    FamiliarExerciseSelection,
    GoalInput,
    PlaceInput,
    PlanTreeInput,
    ProfileInput,
    ProgramIdentityInput,
    ProgramRecommendationInput,
    ProgramVersionDraftInput,
    TemporaryPlanConditionsInput,
)

from trainer.application.context import ContextService
from trainer.transport.http.contracts import (
    ExerciseCatalogDraftRequest,
    GoalDraftRequest,
    OnboardingDraftRequest,
    PlanDraftRequest,
    ProgramDraftRequest,
    ProgramPublicationDraftRequest,
)
from trainer.transport.http.evidence_contracts import (
    EligibleConclusionsRequest,
    RecommendationExplanationRequest,
)


def test_active_tool_catalog_is_closed_and_contains_no_fixture_tools() -> None:
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "acknowledge_preview_shown",
        "confirm_preview",
        "calculate_metric",
        "create_exercise_draft",
        "create_familiar_exercise_batch_draft",
        "create_evidence_draft",
        "create_goal_draft",
        "create_inventory_change_draft",
        "create_historical_record_draft",
        "create_onboarding_draft",
        "create_plan_draft",
        "create_plan_lifecycle_draft",
        "create_record_decision_draft",
        "create_measurement_session_draft",
        "create_workout_correction_draft",
        "create_program_draft",
        "create_program_publication_draft",
        "create_workout_report_draft",
        "create_workout_import_batch_draft",
        "get_body_measurements",
        "get_operation_status",
        "get_current_program",
        "get_eligible_conclusions",
        "get_exact_program",
        "get_exercise_exact",
        "get_plan",
        "get_planning_context",
        "get_profile_context",
        "get_progress",
        "get_record_candidates",
        "get_records",
        "get_recommendation_explanation",
        "get_workout_history",
        "render_preview",
        "revise_program_draft",
        "revise_workout_report_draft",
        "search_exercises",
        "update_source_availability_draft",
    }
    assert all(tool.inputSchema.get("additionalProperties") is False for tool in tools)


def test_server_instructions_explain_nested_queries_and_identifier_types() -> None:
    instructions = server.mcp.instructions

    assert "place filters under the declared nested argument such as `query`" in instructions
    assert "`conversation_id` and `message_id` are strings" in instructions
    assert "`correlation_id` and `causation_id` are UUIDs" in instructions
    assert "Copy domain object IDs only from tool results" in instructions


def test_fastmcp_rejects_undeclared_tool_arguments() -> None:
    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        asyncio.run(
            server.mcp.call_tool(
                "get_profile_context",
                {
                    "conversation_id": "conversation-1",
                    "message_id": "message-1",
                    "timezone": "Asia/Novokuznetsk",
                    "purpose": "plan preparation",
                    "correlation_id": str(uuid4()),
                    "causation_id": str(uuid4()),
                    "arbitrary_path": "/etc/passwd",
                },
            )
        )


def test_api_call_rejects_non_loopback_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAINER_API_URL", "http://example.invalid:8765")
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    with pytest.raises(RuntimeError, match="127.0.0.1"):
        server.api_call("/api/v1/queries/profile-context", {}, str(uuid4()), str(uuid4()))


def test_api_call_rejects_unlisted_local_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    for path in (
        "/api/v1/commands/drafts/anything/cancel",
        "/api/v1/commands/conclusion-verification-drafts",
        "/api/v1/commands/unknown-drafts",
    ):
        with pytest.raises(RuntimeError, match="not allowlisted"):
            server.api_call(path, {}, str(uuid4()), str(uuid4()))


def test_api_call_requires_credential_without_disclosing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("TRAINER_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TRAINER_API_TOKEN") as error:
        server.api_call("/api/v1/queries/profile-context", {}, str(uuid4()), str(uuid4()))
    assert "Bearer" not in str(error.value)


def test_api_call_disables_proxy_and_preserves_success_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    seen: dict[str, Any] = {}
    original_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"value": "canonical"},
                "meta": {
                    "api_version": "v1",
                    "correlation_id": "00000000-0000-4000-8000-000000000001",
                    "operation_id": "00000000-0000-4000-8000-000000000002",
                },
            },
        )

    def client_factory(**kwargs: Any) -> httpx.Client:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(server.httpx, "Client", client_factory)
    result = server.api_call(
        "/api/v1/queries/profile-context",
        {"mode": "exact"},
        str(uuid4()),
        str(uuid4()),
    )
    assert result["meta"]["correlation_id"] == "00000000-0000-4000-8000-000000000001"
    assert result["meta"]["operation_id"] == "00000000-0000-4000-8000-000000000002"
    assert result["data"] == {"value": "canonical"}
    assert seen["url"] == "http://127.0.0.1:8765/api/v1/queries/profile-context"
    assert seen["authorization"] == "Bearer synthetic-token"
    assert seen["body"] == {"mode": "exact"}


def test_api_call_returns_api_error_envelope_without_rewriting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    error = {
        "error": {"code": "stale_preview", "message_safe": "Preview is stale"},
        "meta": {"api_version": "v1"},
    }
    original_client = httpx.Client

    def client_factory(**kwargs: Any) -> httpx.Client:
        return original_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(409, json=error)), **kwargs
        )

    monkeypatch.setattr(server.httpx, "Client", client_factory)
    assert (
        server.api_call("/api/v1/commands/previews", {}, str(uuid4()), str(uuid4()), "key-1")
        == error
    )


def test_api_call_rejects_non_json_without_echoing_response_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    original_client = httpx.Client

    def client_factory(**kwargs: Any) -> httpx.Client:
        return original_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(502, text="secret-like upstream body")
            ),
            **kwargs,
        )

    monkeypatch.setattr(server.httpx, "Client", client_factory)
    with pytest.raises(RuntimeError, match="invalid JSON") as error:
        server.api_call("/api/v1/queries/profile-context", {}, str(uuid4()), str(uuid4()))
    assert "secret-like upstream body" not in str(error.value)


def test_api_call_refuses_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINER_API_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("TRAINER_API_TOKEN", "synthetic-token")
    original_client = httpx.Client

    def client_factory(**kwargs: Any) -> httpx.Client:
        return original_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    307,
                    headers={"Location": "https://example.invalid/collect"},
                    json={"error": {"code": "redirect"}},
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(server.httpx, "Client", client_factory)
    with pytest.raises(RuntimeError, match="Redirects"):
        server.api_call("/api/v1/queries/profile-context", {}, str(uuid4()), str(uuid4()))


def test_goal_draft_builds_complete_body_and_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: str,
        causation_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captured.update(
            path=path,
            body=body,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )
        return {"data": {"draft_id": "synthetic"}}

    monkeypatch.setattr(server, "api_call", fake_api_call)
    correlation_id = uuid4()
    causation_id = uuid4()
    result = server.create_goal_draft(
        conversation_id="conversation-1",
        message_id="message-1",
        timezone="Asia/Novokuznetsk",
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key="goal-draft-1",
        name="10 подтягиваний",
        goal_type="repetitions",
        set_on=date(2026, 10, 10),
        target_date=date(2027, 1, 1),
        priority=1,
        status="active",
        target_value="10",
        unit_code="rep",
    )
    body = captured["body"]
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert captured["path"] == "/api/v1/commands/goal-drafts"
    assert digest == expected
    assert body["goal"]["start_value"] is None
    assert body["goal"]["target_value"] == "10"
    assert body["goal"]["target_date"] == "2027-01-01"
    assert GoalDraftRequest.model_validate({**body, "request_digest": digest})
    ContextService.verify_digest({**body, "request_digest": digest}, digest)
    assert result == {"data": {"draft_id": "synthetic"}}


def _capture_api_call(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_api_call(
        path: str,
        body: dict[str, Any],
        correlation_id: str,
        causation_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        captured.update(path=path, body=body, idempotency_key=idempotency_key)
        return {"data": {"draft_id": "synthetic"}}

    monkeypatch.setattr(server, "api_call", fake_api_call)
    return captured


def _recommendation() -> dict[str, Any]:
    return {
        "recommendation_kind": "exercise_selection",
        "decision_text": "Use the recorded exercise",
        "confidence": "insufficient",
        "revisit_condition": "Revisit when evidence is available",
        "fact_links": [],
        "calculation_links": [],
        "conclusion_links": [],
        "assumptions": {"limitation": "No verified conclusion"},
        "alternatives": {},
        "short_explanation": "No verified scientific conclusion is linked.",
        "detailed_explanation": None,
    }


def _assert_request_digest_and_validate(body: dict[str, Any], model: Any) -> None:
    digest = body.pop("request_digest")
    expected = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == expected
    request = {**body, "request_digest": digest}
    model.model_validate(request)
    ContextService.verify_digest(request, digest)


def test_onboarding_draft_wire_body_matches_active_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    place_id = uuid4()
    equipment_id = uuid4()
    exercise_id = uuid4()
    exercise_revision_id = uuid4()
    server.create_onboarding_draft(
        conversation_id="conversation-1",
        message_id="message-1",
        timezone="Asia/Novokuznetsk",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="onboarding-draft-1",
        profile=ProfileInput(timezone="Asia/Novokuznetsk"),
        goals=[
            GoalInput(
                name="10 подтягиваний",
                goal_type="repetitions",
                set_on=date(2026, 10, 10),
                target_date=date(2027, 1, 1),
                priority=1,
                status="active",
            )
        ],
        places=[
            PlaceInput(
                place_id=place_id,
                name="Домашний зал",
                place_type="home",
                status="active",
            )
        ],
        equipment=[
            ContextEquipmentInput.model_validate(
                {
                    "equipment_id": equipment_id,
                    "equipment_type": "barbell",
                    "name": "Гриф",
                    "maker": None,
                    "brand": None,
                    "model": None,
                    "quantity": None,
                    "quantity_unit_code": None,
                    "condition": "usable",
                    "characteristics": {
                        "mass_per_item_kg": "7",
                        "bar_sleeve_diameter_mm": None,
                        "plate_bore_diameter_mm": None,
                    },
                    "comment": None,
                }
            )
        ],
        familiar_exercises=[
            FamiliarExerciseSelection(
                exercise_ref=ExerciseRevisionRef(
                    object_type="exercise",
                    object_id=exercise_id,
                    revision_id=exercise_revision_id,
                )
            )
        ],
    )
    assert captured["path"] == "/api/v1/commands/onboarding-drafts"
    _assert_request_digest_and_validate(captured["body"], OnboardingDraftRequest)
    assert captured["body"]["places"][0]["place_id"] == str(place_id)
    assert captured["body"]["equipment"][0] == {
        "equipment_id": str(equipment_id),
        "equipment_type": "barbell",
        "name": "Гриф",
        "maker": None,
        "brand": None,
        "model": None,
        "quantity": None,
        "quantity_unit_code": None,
        "condition": "usable",
        "characteristics": {
            "mass_per_item_kg": "7",
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": None,
        },
        "comment": None,
    }
    assert captured["body"]["familiar_exercises"] == [
        {
            "exercise_ref": {
                "object_type": "exercise",
                "object_id": str(exercise_id),
                "revision_id": str(exercise_revision_id),
            }
        }
    ]


def test_exercise_draft_wire_body_matches_active_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    server.create_exercise_draft(
        conversation_id="conversation-1",
        message_id="message-1",
        timezone="Asia/Novokuznetsk",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="exercise-draft-1",
        exercise=ExerciseCatalogInput.model_validate(
            {
                "canonical_name": "Приседание",
                "description": "Synthetic exercise aggregate",
                "category_code": "strength",
                "unilateral": False,
                "status_code": "active",
                "aliases": [
                    {"alias": "Присед", "locale": "ru-RU", "context": "разговорный вариант"}
                ],
                "muscle_roles": [
                    {
                        "kind": "create_by_name",
                        "name": "Квадрицепсы",
                        "status_code": "active",
                        "role": "primary",
                    },
                    {
                        "kind": "existing",
                        "muscle_group_id": uuid4(),
                        "role": "secondary",
                    },
                ],
                "measurements": [
                    {
                        "metric_kind": "repetitions",
                        "requiredness": "required",
                        "unit_family": "count",
                    }
                ],
                "equipment_requirements": [
                    {
                        "equipment_type": "barbell",
                        "optionality": "alternative",
                        "alternative_group": "external_load",
                    },
                    {
                        "equipment_type": "dumbbell",
                        "optionality": "alternative",
                        "alternative_group": "external_load",
                    },
                ],
            }
        ),
    )
    assert captured["path"] == "/api/v1/commands/exercise-catalog-drafts"
    _assert_request_digest_and_validate(captured["body"], ExerciseCatalogDraftRequest)
    assert captured["body"]["exercise"]["aliases"][0]["alias"] == "Присед"
    assert captured["body"]["exercise"]["muscle_roles"][0] == {
        "kind": "create_by_name",
        "name": "Квадрицепсы",
        "status_code": "active",
        "role": "primary",
    }
    assert captured["body"]["exercise"]["muscle_roles"][1]["kind"] == "existing"
    assert captured["body"]["exercise"]["equipment_requirements"] == [
        {
            "equipment_type": "barbell",
            "optionality": "alternative",
            "alternative_group": "external_load",
        },
        {
            "equipment_type": "dumbbell",
            "optionality": "alternative",
            "alternative_group": "external_load",
        },
    ]


def test_program_draft_wire_body_matches_active_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    exercise_id = uuid4()
    exercise_revision_id = uuid4()
    fact_id = uuid4()
    fact_revision_id = uuid4()
    calculation_id = uuid4()
    conclusion_version_id = uuid4()
    recommendation = _recommendation()
    recommendation["confidence"] = "moderate"
    recommendation["fact_links"] = [
        {
            "ref": {
                "object_type": "training_goal",
                "object_id": str(fact_id),
                "revision_id": str(fact_revision_id),
            },
            "role": "constraint",
            "reason": "Synthetic exact goal revision constrains the recommendation",
            "snapshot_digest": "a" * 64,
        }
    ]
    recommendation["calculation_links"] = [
        {"derived_value_id": str(calculation_id), "role": "supporting"}
    ]
    recommendation["conclusion_links"] = [
        {"conclusion_version_id": str(conclusion_version_id), "role": "supports"}
    ]
    server.create_program_draft(
        conversation_id="conversation-1",
        message_id="message-1",
        timezone="Asia/Novokuznetsk",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="program-draft-1",
        goal_revision_id=uuid4(),
        program=ProgramIdentityInput.model_validate(
            {
                "name": "Силовая программа",
                "description": None,
                "starts_on": date(2026, 10, 10),
                "ends_on": date(2026, 12, 31),
                "status_code": "active",
                "comment": None,
            }
        ),
        version=ProgramVersionDraftInput.model_validate(
            {
                "version_label": "v1",
                "rationale": "Synthetic contract test",
                "period_start": date(2026, 10, 10),
                "period_end": date(2026, 12, 31),
                "templates": [
                    {
                        "position": 1,
                        "day_type_code": "training",
                        "purpose": "Силовая тренировка",
                        "blocks": [
                            {
                                "position": 1,
                                "block_type_code": "straight",
                                "exercises": [
                                    {
                                        "position": 1,
                                        "exercise_ref": {
                                            "object_type": "exercise",
                                            "object_id": exercise_id,
                                            "revision_id": exercise_revision_id,
                                        },
                                        "sets": [
                                            {
                                                "position": 1,
                                                "logical_set_no": 1,
                                                "set_type_code": "working",
                                                "metrics": [
                                                    {
                                                        "metric_kind": "repetitions",
                                                        "exact_value": "8",
                                                        "min_value": None,
                                                        "max_value": None,
                                                        "unit_code": "rep",
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "recommendations": [recommendation],
            }
        ),
    )
    assert captured["path"] == "/api/v1/commands/program-drafts"
    _assert_request_digest_and_validate(captured["body"], ProgramDraftRequest)
    assert captured["body"]["version"]["templates"][0]["blocks"][0]["exercises"][0][
        "exercise_ref"
    ] == {
        "object_type": "exercise",
        "object_id": str(exercise_id),
        "revision_id": str(exercise_revision_id),
    }
    actual_recommendation = captured["body"]["version"]["recommendations"][0]
    assert actual_recommendation["fact_links"][0]["ref"] == {
        "object_type": "training_goal",
        "object_id": str(fact_id),
        "revision_id": str(fact_revision_id),
    }
    assert actual_recommendation["calculation_links"] == [
        {"derived_value_id": str(calculation_id), "role": "supporting"}
    ]
    assert actual_recommendation["conclusion_links"] == [
        {"conclusion_version_id": str(conclusion_version_id), "role": "supports"}
    ]


def test_new_program_version_uses_exact_program_and_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    program_id = uuid4()
    current_version_id = uuid4()
    goal_revision_id = uuid4()
    exercise_id = uuid4()
    exercise_revision_id = uuid4()

    server.create_program_draft(
        conversation_id="conversation-1",
        message_id="new-version-message",
        timezone="Asia/Novokuznetsk",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="program-new-version-1",
        intent="new_version",
        program=None,
        program_id=program_id,
        expected_current_version_id=current_version_id,
        goal_revision_id=goal_revision_id,
        version=ProgramVersionDraftInput.model_validate(
            {
                "version_label": "v2",
                "rationale": "Synthetic new-version contract test",
                "period_start": date(2026, 10, 10),
                "period_end": date(2026, 12, 31),
                "templates": [
                    {
                        "position": 1,
                        "day_type_code": "training",
                        "purpose": "Силовая тренировка",
                        "blocks": [
                            {
                                "position": 1,
                                "block_type_code": "straight",
                                "exercises": [
                                    {
                                        "position": 1,
                                        "exercise_ref": {
                                            "object_type": "exercise",
                                            "object_id": exercise_id,
                                            "revision_id": exercise_revision_id,
                                        },
                                        "sets": [
                                            {
                                                "position": 1,
                                                "logical_set_no": 1,
                                                "set_type_code": "working",
                                                "metrics": [
                                                    {
                                                        "metric_kind": "repetitions",
                                                        "exact_value": "8",
                                                        "min_value": None,
                                                        "max_value": None,
                                                        "unit_code": "rep",
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "recommendations": [],
            }
        ),
    )

    assert captured["path"] == "/api/v1/commands/program-drafts"
    body = captured["body"]
    _assert_request_digest_and_validate(body, ProgramDraftRequest)
    normalized = {key: value for key, value in body.items() if key != "request_digest"}
    ContextService.validate_program(normalized)
    assert body["intent"] == "new_version"
    assert body["program_id"] == str(program_id)
    assert body["expected_current_version_id"] == str(current_version_id)
    assert body["goal_revision_id"] == str(goal_revision_id)
    assert body["program"] is None
    exercise_ref = body["version"]["templates"][0]["blocks"][0]["exercises"][0]["exercise_ref"]
    assert exercise_ref == {
        "object_type": "exercise",
        "object_id": str(exercise_id),
        "revision_id": str(exercise_revision_id),
    }


def test_program_publication_is_a_separate_exact_revision_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    source_draft_id = uuid4()
    source_revision_id = uuid4()
    expected_current_version_id = uuid4()

    result = server.create_program_publication_draft(
        conversation_id="conversation-1",
        message_id="publication-message",
        timezone="Asia/Novokuznetsk",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="program-publication-draft-1",
        source_draft_id=source_draft_id,
        source_revision_id=source_revision_id,
        publication_reason="Публикация проверенной версии программы",
        expected_current_version_id=expected_current_version_id,
    )

    assert captured["path"] == "/api/v1/commands/program-publication-drafts"
    assert captured["idempotency_key"] == "program-publication-draft-1"
    assert captured["body"]["source_draft_id"] == str(source_draft_id)
    assert captured["body"]["source_revision_id"] == str(source_revision_id)
    assert captured["body"]["expected_current_version_id"] == str(expected_current_version_id)
    _assert_request_digest_and_validate(captured["body"], ProgramPublicationDraftRequest)
    assert result == {"data": {"draft_id": "synthetic"}}


def test_plan_draft_wire_body_matches_active_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    goal_id = uuid4()
    goal_revision_id = uuid4()
    calculation_id = uuid4()
    conclusion_version_id = uuid4()
    exercise_ids = [uuid4(), uuid4()]
    recommendation = _recommendation()
    recommendation["confidence"] = "moderate"
    recommendation["fact_links"] = [
        {
            "ref": {
                "object_type": "training_goal",
                "object_id": str(goal_id),
                "revision_id": str(goal_revision_id),
            },
            "role": "constraint",
            "reason": "Synthetic dated goal constrains this training plan",
            "snapshot_digest": "b" * 64,
        }
    ]
    recommendation["calculation_links"] = [
        {"derived_value_id": str(calculation_id), "role": "supporting"}
    ]
    recommendation["conclusion_links"] = [
        {"conclusion_version_id": str(conclusion_version_id), "role": "supports"}
    ]
    server.create_plan_draft(
        conversation_id="conversation-1",
        message_id="message-1",
        timezone="Asia/Novokuznetsk",
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="plan-draft-1",
        plan=PlanTreeInput.model_validate(
            {
                "planned_date": date(2026, 10, 10),
                "planned_time": "18:00:00",
                "plan_type": "training",
                "name": "Силовая тренировка",
                "purpose": "Synthetic plan with per-set loads",
                "temporary_conditions": {
                    "place": {
                        "kind": "ad_hoc",
                        "name": "Temporary apartment workout",
                        "place_type": "home",
                    },
                    "equipment_additions": ["resistance_band"],
                    "equipment_removals": ["barbell"],
                },
                "blocks": [
                    {
                        "position": 1,
                        "block_type_code": "straight",
                        "exercises": [
                            {
                                "position": 1,
                                "exercise_id": exercise_ids[0],
                                "sets": [
                                    {
                                        "position": 1,
                                        "logical_set_no": 1,
                                        "set_type_code": "working",
                                        "metrics": [
                                            {
                                                "metric_kind": "repetitions",
                                                "exact_value": "8",
                                                "min_value": None,
                                                "max_value": None,
                                                "unit_code": "rep",
                                            }
                                        ],
                                        "loads": [
                                            {
                                                "position": 1,
                                                "load_kind": "external",
                                                "equipment_id": None,
                                                "item_count": None,
                                                "mass": "20",
                                                "unit_code": "kilogram",
                                                "mass_meaning": "total_external_mass",
                                                "direction": None,
                                                "raw_ambiguous_text": None,
                                            }
                                        ],
                                    },
                                    {
                                        "position": 2,
                                        "logical_set_no": 2,
                                        "set_type_code": "working",
                                        "metrics": [
                                            {
                                                "metric_kind": "repetitions",
                                                "exact_value": "6",
                                                "min_value": None,
                                                "max_value": None,
                                                "unit_code": "rep",
                                            }
                                        ],
                                        "loads": [
                                            {
                                                "position": 1,
                                                "load_kind": "external",
                                                "equipment_id": None,
                                                "item_count": None,
                                                "mass": "25",
                                                "unit_code": "kilogram",
                                                "mass_meaning": "total_external_mass",
                                                "direction": None,
                                                "raw_ambiguous_text": None,
                                            }
                                        ],
                                    },
                                ],
                            },
                            {
                                "position": 2,
                                "exercise_id": exercise_ids[1],
                                "sets": [
                                    {
                                        "position": 1,
                                        "logical_set_no": 1,
                                        "set_type_code": "working",
                                        "metrics": [
                                            {
                                                "metric_kind": "repetitions",
                                                "exact_value": "10",
                                                "min_value": None,
                                                "max_value": None,
                                                "unit_code": "rep",
                                            }
                                        ],
                                        "loads": [],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ),
        recommendations=[ProgramRecommendationInput.model_validate(recommendation)],
    )
    assert captured["path"] == "/api/v1/commands/plan-drafts"
    _assert_request_digest_and_validate(captured["body"], PlanDraftRequest)
    actual_recommendation = captured["body"]["recommendations"][0]
    assert actual_recommendation["fact_links"][0]["ref"] == {
        "object_type": "training_goal",
        "object_id": str(goal_id),
        "revision_id": str(goal_revision_id),
    }
    assert actual_recommendation["calculation_links"] == [
        {"derived_value_id": str(calculation_id), "role": "supporting"}
    ]
    assert actual_recommendation["conclusion_links"] == [
        {"conclusion_version_id": str(conclusion_version_id), "role": "supports"}
    ]
    exercises = captured["body"]["plan"]["blocks"][0]["exercises"]
    assert [item["exercise_id"] for item in exercises] == [
        str(exercise_ids[0]),
        str(exercise_ids[1]),
    ]
    assert [item["loads"][0]["mass"] for item in exercises[0]["sets"]] == [
        "20",
        "25",
    ]
    assert captured["body"]["plan"]["temporary_conditions"] == {
        "place": {
            "kind": "ad_hoc",
            "name": "Temporary apartment workout",
            "place_type": "home",
        },
        "equipment_additions": ["resistance_band"],
        "equipment_removals": ["barbell"],
    }


def test_planning_context_forwards_only_explicit_temporary_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    overlay = TemporaryPlanConditionsInput.model_validate(
        {
            "place": {
                "kind": "ad_hoc",
                "name": "Temporary apartment workout",
                "place_type": "home",
            },
            "equipment_additions": ["resistance_band"],
            "equipment_removals": ["barbell"],
        }
    )

    result = server.get_planning_context(
        conversation_id="conversation-1",
        message_id="temporary-plan-context",
        timezone="Asia/Novokuznetsk",
        target_date=date(2026, 10, 11),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        temporary_conditions=overlay,
    )

    assert captured["path"] == "/api/v1/queries/planning-context"
    assert captured["body"]["context"]["conversation_id"] == "conversation-1"
    assert captured["body"]["context"]["message_id"] == "temporary-plan-context"
    assert captured["body"]["context"]["timezone"] == "Asia/Novokuznetsk"
    assert captured["body"]["target_date"] == "2026-10-11"
    assert captured["body"]["program_id"] is None
    assert captured["body"]["temporary_conditions"] == {
        "place": {
            "kind": "ad_hoc",
            "name": "Temporary apartment workout",
            "place_type": "home",
        },
        "equipment_additions": ["resistance_band"],
        "equipment_removals": ["barbell"],
    }
    assert result == {"data": {"draft_id": "synthetic"}}

    with pytest.raises(ValidationError, match="cannot overlap"):
        TemporaryPlanConditionsInput.model_validate(
            {
                "place": None,
                "equipment_additions": ["barbell"],
                "equipment_removals": ["barbell"],
            }
        )


def test_evidence_read_tools_forward_exact_topic_applicability_and_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    applicability = {
        "population": "healthy adult",
        "training_goal": "strength",
        "training_experience": "confirmed profile only",
    }
    result = server.get_eligible_conclusions(
        conversation_id="conversation-1",
        message_id="eligible-evidence",
        timezone="Asia/Novokuznetsk",
        topic="weekly resistance training frequency",
        applicability_snapshot=applicability,
        decision_date=date(2026, 10, 11),
        correlation_id=uuid4(),
        causation_id=uuid4(),
    )

    assert captured["path"] == "/api/v1/queries/conclusions/eligible"
    request = EligibleConclusionsRequest.model_validate(captured["body"])
    assert request.topic == "weekly resistance training frequency"
    assert request.applicability_snapshot == applicability
    assert request.decision_date == date(2026, 10, 11)
    assert result == {"data": {"draft_id": "synthetic"}}


def test_recommendation_explanation_requires_exact_saved_recommendation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_api_call(monkeypatch)
    recommendation_id = uuid4()
    result = server.get_recommendation_explanation(
        conversation_id="conversation-1",
        message_id="saved-recommendation-explanation",
        timezone="Asia/Novokuznetsk",
        recommendation_id=recommendation_id,
        detail_level="detailed",
        correlation_id=uuid4(),
        causation_id=uuid4(),
    )

    assert captured["path"] == "/api/v1/queries/recommendations/explanation"
    request = RecommendationExplanationRequest.model_validate(captured["body"])
    assert request.recommendation_id == recommendation_id
    assert request.detail_level == "detailed"
    assert result == {"data": {"draft_id": "synthetic"}}


def test_confirmation_requires_explicit_intent() -> None:
    schema = next(
        tool.inputSchema
        for tool in asyncio.run(server.mcp.list_tools())
        if tool.name == "confirm_preview"
    )
    assert "explicit_intent" in schema["required"]
    assert "owner_verification" not in str(schema)


def test_mcp_refuses_owner_verification_confirmation_kind() -> None:
    with pytest.raises(ToolError):
        asyncio.run(
            server.mcp.call_tool(
                "confirm_preview",
                {
                    "preview_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                    "preview_digest": "a" * 64,
                    "conversation_id": "conversation-1",
                    "confirmation_message_id": "message-1",
                    "correlation_id": str(uuid4()),
                    "causation_id": str(uuid4()),
                    "idempotency_key": "confirm-1",
                    "timezone": "Asia/Novokuznetsk",
                    "explicit_intent": "Подтверждаю этот точный preview",
                    "confirmation_mode": "same_dialog",
                    "confirmation_kind": "owner_verification",
                },
            )
        )
