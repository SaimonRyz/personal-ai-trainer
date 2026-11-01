from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from trainer.application.context import ContextService
from trainer.config.settings import Settings
from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import ContextRepository
from trainer.transport.http.app import create_app
from trainer.transport.http.contracts import (
    ConfirmRequest,
    ExerciseCatalogDraftRequest,
    OnboardingDraftRequest,
    PlanDraftRequest,
    PlanLifecycleRequest,
    PreviewShownRequest,
    ProgramTemplateSetInput,
)

TEST_TOKEN = "stage-c-fixture-credential-32-chars"


def client() -> TestClient:
    settings = Settings(api_token=SecretStr(TEST_TOKEN))
    return TestClient(create_app(settings), raise_server_exceptions=False)


def required_headers(*, authorization: bool = True) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
    }
    if authorization:
        headers["Authorization"] = f"Bearer {TEST_TOKEN}"
    return headers


def onboarding_request(*, effective_at: datetime) -> dict[str, Any]:
    place_id, equipment_id = uuid4(), uuid4()
    raw: dict[str, Any] = {
        "context": {
            "conversation_id": "contract-onboarding",
            "message_id": "contract-onboarding-draft",
            "effective_at": effective_at.isoformat(),
            "timezone": "Asia/Novokuznetsk",
            "locale": None,
        },
        "profile": {
            "birth_date": None,
            "sex": None,
            "timezone": "Asia/Novokuznetsk",
            "started_on": None,
            "desired_sessions_per_week": 3,
            "usual_place_id": str(place_id),
            "comment": None,
        },
        "goals": [
            {
                "goal_id": None,
                "parent_goal_id": None,
                "name": "Contract goal",
                "description": None,
                "goal_type": "performance",
                "start_value": "1",
                "target_value": "2",
                "unit_code": "repetition",
                "set_on": "2026-08-24",
                "target_date": "2026-12-31",
                "priority": 10,
                "status": "active",
                "comment": None,
            }
        ],
        "places": [
            {
                "place_id": str(place_id),
                "name": "Contract place",
                "place_type": "home_gym",
                "status": "active",
                "comment": None,
            }
        ],
        "equipment": [
            {
                "equipment_id": str(equipment_id),
                "equipment_type": "barbell",
                "name": "Contract barbell",
                "maker": None,
                "brand": None,
                "model": None,
                "quantity": None,
                "quantity_unit_code": None,
                "condition": None,
                "characteristics": {
                    "mass_per_item_kg": None,
                    "bar_sleeve_diameter_mm": None,
                    "plate_bore_diameter_mm": None,
                },
                "comment": None,
            }
        ],
    }
    normalized = OnboardingDraftRequest.model_validate(
        {**raw, "request_digest": "0" * 64}
    ).model_dump(mode="json")
    normalized.pop("request_digest")
    normalized.pop("draft_id")
    normalized.pop("expected_revision_id")
    return {**raw, "request_digest": digest_payload(normalized)}


def test_onboarding_http_preserves_unknowns_and_rejects_place_availability() -> None:
    effective_at = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    def executor(**kwargs: Any) -> dict[str, Any]:
        if kwargs["action"] == "record_failed_request":
            return {"recorded": True}
        calls.append(kwargs)
        return {
            "draft_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "revision_no": 1,
            "content_digest": "a" * 64,
            "dependencies": [],
            "gaps": [],
            "preview_candidate": {
                "summary": "Contract onboarding",
                "confirmation_kind": "normal",
                "complete": False,
            },
        }

    test_client = TestClient(
        create_app(
            Settings(api_token=SecretStr(TEST_TOKEN)),
            context_service=ContextService(executor),
        ),
        raise_server_exceptions=False,
    )
    accepted = test_client.post(
        "/api/v1/commands/onboarding-drafts",
        headers={**required_headers(), "Idempotency-Key": "window-accepted"},
        json=onboarding_request(effective_at=effective_at),
    )
    assert accepted.status_code == 200
    assert calls[0]["payload"]["equipment"][0]["condition"] is None
    assert "place_equipment_availability" not in calls[0]["payload"]
    old_request = onboarding_request(effective_at=effective_at)
    old_request["place_equipment_availability"] = []
    rejected = test_client.post(
        "/api/v1/commands/onboarding-drafts",
        headers={**required_headers(), "Idempotency-Key": "window-rejected"},
        json=old_request,
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "validation_error"
    assert len(calls) == 1


def test_onboarding_http_accepts_only_explicit_exact_familiar_exercise_refs() -> None:
    effective_at = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    exercise_id, exercise_revision_id = uuid4(), uuid4()
    body = onboarding_request(effective_at=effective_at)
    body["familiar_exercises"] = [
        {
            "exercise_ref": {
                "object_type": "exercise",
                "object_id": str(exercise_id),
                "revision_id": str(exercise_revision_id),
            }
        }
    ]
    normalized = OnboardingDraftRequest.model_validate(
        {**body, "request_digest": "0" * 64}
    ).model_dump(mode="json")
    normalized.pop("request_digest")
    normalized.pop("draft_id")
    normalized.pop("expected_revision_id")
    body["request_digest"] = digest_payload(normalized)
    calls: list[dict[str, Any]] = []

    def executor(**kwargs: Any) -> dict[str, Any]:
        if kwargs["action"] == "record_failed_request":
            return {"recorded": True}
        calls.append(kwargs)
        return {
            "draft_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "revision_no": 1,
            "content_digest": "a" * 64,
            "dependencies": [],
            "gaps": [],
            "preview_candidate": {
                "summary": "Ready",
                "confirmation_kind": "normal",
                "complete": True,
            },
        }

    test_client = TestClient(
        create_app(
            Settings(api_token=SecretStr(TEST_TOKEN)),
            context_service=ContextService(executor),
        ),
        raise_server_exceptions=False,
    )
    response = test_client.post(
        "/api/v1/commands/onboarding-drafts",
        headers={**required_headers(), "Idempotency-Key": "familiar-exercise"},
        json=body,
    )

    assert response.status_code == 200
    assert calls[0]["payload"]["familiar_exercises"] == body["familiar_exercises"]

    invalid = dict(body)
    invalid["familiar_exercises"] = [
        {
            "exercise_ref": {
                "object_type": "goal",
                "object_id": str(exercise_id),
                "revision_id": str(exercise_revision_id),
            }
        }
    ]
    rejected = test_client.post(
        "/api/v1/commands/onboarding-drafts",
        headers={**required_headers(), "Idempotency-Key": "familiar-exercise-invalid"},
        json=invalid,
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "validation_error"
    assert len(calls) == 1


def test_api_v1_is_fail_closed_without_bearer_credential() -> None:
    response = client().post(
        "/api/v1/not-a-route", json={}, headers=required_headers(authorization=False)
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert TEST_TOKEN not in response.text
    UUID(response.json()["error"]["correlation_id"])
    assert response.json()["meta"] == {"api_version": "v1"}


def test_valid_credential_reaches_closed_route_catalog_without_generic_fallback() -> None:
    correlation_id = uuid4()
    response = client().post(
        "/api/v1/not-a-route",
        json={"sql": "select 1"},
        headers={**required_headers(), "X-Correlation-ID": str(correlation_id)},
    )
    assert response.status_code == 404
    assert response.headers["X-Correlation-ID"] == str(correlation_id)
    assert response.json()["error"]["code"] == "not_found"
    assert set(response.json()) == {"error", "meta"}
    assert set(response.json()["error"]) == {
        "code",
        "message_safe",
        "retryable",
        "operation_state",
        "correlation_id",
        "operation_id",
        "idempotency_key",
        "field_errors",
        "changed_dependencies",
        "required_next_action",
        "retry_after_seconds",
    }


def test_invalid_correlation_and_oversized_request_fail_safely() -> None:
    invalid = client().post(
        "/api/v1/not-a-route",
        headers={**required_headers(), "X-Correlation-ID": "not-a-uuid"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "validation_error"

    oversized = client().post(
        "/api/v1/not-a-route",
        headers={**required_headers(), "Content-Length": "9999999"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"


def test_api_v1_requires_media_and_both_trace_headers() -> None:
    base = {"Authorization": f"Bearer {TEST_TOKEN}"}
    assert client().post("/api/v1/not-a-route", headers=base).status_code == 406
    wrong_type = {
        **required_headers(),
        "Content-Type": "text/plain",
    }
    assert client().post("/api/v1/not-a-route", headers=wrong_type).status_code == 415
    missing_correlation = required_headers()
    missing_correlation.pop("X-Correlation-ID")
    assert client().post("/api/v1/not-a-route", headers=missing_correlation).status_code == 400
    missing_causation = required_headers()
    missing_causation.pop("X-Causation-ID")
    assert client().post("/api/v1/not-a-route", headers=missing_causation).status_code == 400


def test_router_validation_and_method_errors_use_normative_envelope() -> None:
    app = create_app(Settings(api_token=SecretStr(TEST_TOKEN)))

    @app.post("/api/v1/stage-c/{item_id}")
    def fixture(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    test_client = TestClient(app, raise_server_exceptions=False)
    validation = test_client.post(
        "/api/v1/stage-c/not-an-integer",
        headers={**required_headers(), "Idempotency-Key": "validation"},
        json={},
    )
    method = test_client.put(
        "/api/v1/stage-c/1",
        headers={**required_headers(), "Idempotency-Key": "method"},
        json={},
    )
    assert validation.status_code == 400
    assert validation.json()["error"]["code"] == "validation_error"
    assert method.status_code == 405
    assert method.json()["error"]["code"] == "validation_error"


def test_causation_header_is_validated_and_echoed_when_safe() -> None:
    correlation_id = uuid4()
    causation_id = uuid4()
    accepted = client().get(
        "/openapi.json",
        headers={
            "X-Correlation-ID": str(correlation_id),
            "X-Causation-ID": str(causation_id),
        },
    )
    assert accepted.status_code == 200
    assert accepted.headers["X-Correlation-ID"] == str(correlation_id)
    assert accepted.headers["X-Causation-ID"] == str(causation_id)

    rejected = client().get("/openapi.json", headers={"X-Causation-ID": "not-a-uuid"})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "validation_error"


def test_settings_reject_non_loopback_host_and_weak_token() -> None:
    try:
        Settings(api_token=SecretStr("weak"))
    except ValidationError as error:
        fields = {tuple(item["loc"]) for item in error.errors()}
        assert ("api_token",) in fields
    else:
        raise AssertionError("unsafe settings must fail validation")

    try:
        Settings(api_token=SecretStr(TEST_TOKEN), api_host="0.0.0.0")
    except ValidationError as error:
        assert "container network namespace" in str(error)
    else:
        raise AssertionError("bind-all requires explicit container scope")


def test_openapi_has_only_approved_domain_routes() -> None:
    response = client().get("/openapi.json")
    assert response.status_code == 200
    assert set(response.json()["paths"]) == {
        "/api/v1/queries/profile-context",
        "/api/v1/commands/onboarding-drafts",
        "/api/v1/commands/goal-drafts",
        "/api/v1/commands/inventory-change-drafts",
        "/api/v1/commands/measurement-session-drafts",
        "/api/v1/queries/measurements",
        "/api/v1/commands/previews",
        "/api/v1/commands/previews/{preview_id}/shown",
        "/api/v1/commands/previews/{preview_id}/confirm",
        "/api/v1/queries/exercises/search",
        "/api/v1/commands/exercise-catalog-drafts",
        "/api/v1/queries/programs/get",
        "/api/v1/commands/program-drafts",
        "/api/v1/commands/program-publication-drafts",
        "/api/v1/commands/drafts/{draft_id}/revisions",
        "/api/v1/queries/planning-context",
        "/api/v1/queries/plans/get",
        "/api/v1/commands/plan-drafts",
        "/api/v1/commands/plan-lifecycle-drafts",
        "/api/v1/commands/workout-report-drafts",
        "/api/v1/commands/correction-drafts",
        "/api/v1/queries/workouts/history",
        "/api/v1/queries/record-candidates",
        "/api/v1/commands/record-decision-drafts",
        "/api/v1/queries/records",
        "/api/v1/queries/progress",
        "/api/v1/commands/calculations",
        "/api/v1/commands/historical-record-drafts",
        "/api/v1/queries/operations/status",
        "/api/v1/commands/evidence-drafts",
        "/api/v1/commands/conclusion-verification-drafts",
        "/api/v1/queries/conclusions/eligible",
        "/api/v1/queries/recommendations/explanation",
    }
    assert all("sql" not in path and "crud" not in path for path in response.json()["paths"])
    schema = response.json()
    onboarding = schema["components"]["schemas"]["OnboardingDraftRequest"]
    assert onboarding["additionalProperties"] is False
    assert set(onboarding["required"]) == {
        "context",
        "profile",
        "goals",
        "places",
        "equipment",
        "request_digest",
    }
    familiar_ref = schema["components"]["schemas"]["FamiliarExerciseRef"]
    assert familiar_ref["additionalProperties"] is False
    assert set(familiar_ref["properties"]) == {"object_type", "object_id", "revision_id"}
    assert set(familiar_ref["required"]) == {"object_type", "object_id", "revision_id"}
    equipment = schema["components"]["schemas"]["ContextEquipmentInput"]
    assert "quantity" in equipment["properties"]
    assert "quantity" in equipment["required"]
    preview_response = schema["paths"]["/api/v1/commands/previews"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert "DataEnvelope_PreviewResult_" in preview_response["$ref"]
    confirm_response = schema["paths"]["/api/v1/commands/previews/{preview_id}/confirm"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert "DataEnvelope_CommitResult_" in confirm_response["$ref"]


def test_workout_history_rejects_malformed_cursor_with_http_400() -> None:
    def executor(**kwargs: Any) -> dict[str, Any]:
        if kwargs["action"] == "record_failed_request":
            return {"recorded": True}
        if kwargs["action"] == "get_workout_history":
            return ContextRepository.__new__(ContextRepository).get_workout_history(
                payload=kwargs["payload"]
            )
        raise AssertionError(f"unexpected action: {kwargs['action']}")

    test_client = TestClient(
        create_app(
            Settings(api_token=SecretStr(TEST_TOKEN)),
            context_service=ContextService(executor),
        ),
        raise_server_exceptions=False,
    )
    response = test_client.post(
        "/api/v1/queries/workouts/history",
        headers=required_headers(),
        json={
            "context": {"purpose": "review workout history"},
            "date_from": "2026-09-01",
            "date_to": "2026-09-30",
            "limit": 20,
            "cursor": "WyIyMDI2LTA5LTAxIiw3LDFd",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_d_g02_openapi_is_exact_for_all_requests_responses_and_errors() -> None:
    schema = client().get("/openapi.json").json()
    components = schema["components"]["schemas"]
    expected_shapes = {
        "ProfileContextRequest": ({"context", "purpose"}, {"context", "purpose"}),
        "OnboardingDraftRequest": (
            {
                "context",
                "profile",
                "goals",
                "places",
                "equipment",
                "familiar_exercises",
                "draft_id",
                "expected_revision_id",
                "request_digest",
            },
            {
                "context",
                "profile",
                "goals",
                "places",
                "equipment",
                "request_digest",
            },
        ),
        "GoalDraftRequest": (
            {"context", "intent", "goal", "expected_revision_id", "reason", "request_digest"},
            {"context", "intent", "goal", "request_digest"},
        ),
        "PreviewRequest": (
            {"context", "revision_id", "detail_level", "entry_name", "entry_key", "request_digest"},
            {"context", "revision_id", "detail_level", "request_digest"},
        ),
        "PreviewShownRequest": (
            {
                "context",
                "revision_id",
                "preview_digest",
                "shown_message_id",
                "displayed_text_digest",
                "cross_dialog",
                "request_digest",
            },
            {
                "context",
                "revision_id",
                "preview_digest",
                "shown_message_id",
                "displayed_text_digest",
                "cross_dialog",
                "request_digest",
            },
        ),
        "ConfirmRequest": (
            {
                "context",
                "revision_id",
                "preview_digest",
                "confirmation_kind",
                "confirmation_message_id",
                "explicit_intent",
                "confirmation_mode",
                "referenced_preview_id",
                "reshown_message_id",
                "request_digest",
            },
            {
                "context",
                "revision_id",
                "preview_digest",
                "confirmation_kind",
                "confirmation_message_id",
                "explicit_intent",
                "confirmation_mode",
                "referenced_preview_id",
                "reshown_message_id",
                "request_digest",
            },
        ),
        "ProfileContextResponse": (
            {
                "profile_ref",
                "profile",
                "goals",
                "places",
                "equipment",
                "familiar_exercises",
                "effective_at",
                "gaps",
            },
            {
                "profile_ref",
                "profile",
                "goals",
                "places",
                "equipment",
                "familiar_exercises",
                "effective_at",
                "gaps",
            },
        ),
        "DraftResult": (
            {
                "draft_id",
                "revision_id",
                "revision_no",
                "content_digest",
                "dependencies",
                "gaps",
                "preview_candidate",
            },
            {
                "draft_id",
                "revision_id",
                "revision_no",
                "content_digest",
                "dependencies",
                "gaps",
                "preview_candidate",
            },
        ),
        "PreviewResult": (
            {
                "preview_id",
                "revision_id",
                "conversation_id",
                "digest",
                "confirmation_kind",
                "complete",
                "canonical_payload",
                "canonical_text",
                "changed_objects",
                "dependencies",
                "gaps",
                "shown_message_id",
                "shown_at",
                "expires_at",
            },
            {
                "preview_id",
                "revision_id",
                "conversation_id",
                "digest",
                "confirmation_kind",
                "complete",
                "canonical_payload",
                "canonical_text",
                "changed_objects",
                "dependencies",
                "gaps",
                "expires_at",
            },
        ),
        "CommitResult": (
            {
                "commit_result_id",
                "confirmation_id",
                "operation_id",
                "status",
                "domain_changes",
                "created_revisions",
                "invalidated_calculations",
                "committed_at",
            },
            {
                "commit_result_id",
                "confirmation_id",
                "operation_id",
                "status",
                "domain_changes",
                "created_revisions",
                "invalidated_calculations",
                "committed_at",
            },
        ),
        "ErrorDetail": (
            {
                "code",
                "message_safe",
                "retryable",
                "operation_state",
                "correlation_id",
                "operation_id",
                "idempotency_key",
                "field_errors",
                "changed_dependencies",
                "required_next_action",
                "retry_after_seconds",
            },
            {
                "code",
                "message_safe",
                "retryable",
                "operation_state",
                "correlation_id",
                "field_errors",
                "changed_dependencies",
            },
        ),
    }
    for name, (properties, required) in expected_shapes.items():
        assert components[name]["additionalProperties"] is False
        assert set(components[name]["properties"]) == properties
        assert set(components[name].get("required", [])) == required

    expected_statuses = {
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "410",
        "422",
        "429",
        "500",
        "503",
    }
    for route in schema["paths"].values():
        responses = route["post"]["responses"]
        assert set(responses) == expected_statuses
        for status in expected_statuses - {"200"}:
            assert responses[status]["content"]["application/json"]["schema"]["$ref"].endswith(
                "/ErrorEnvelope"
            )


def test_program_pause_seconds_is_exact_numeric_8_3() -> None:
    valid = ProgramTemplateSetInput.model_validate(
        {
            "position": 1,
            "logical_set_no": 1,
            "side": "both",
            "set_type_code": "working",
            "pause_seconds": "99999.999",
            "metrics": [
                {"metric_kind": "repetitions", "exact_value": "1", "unit_code": "repetition"}
            ],
        }
    )
    assert str(valid.pause_seconds) == "99999.999"
    for invalid in ("0.0001", "100000", "-0.001"):
        with pytest.raises(ValidationError):
            ProgramTemplateSetInput.model_validate(
                {
                    "position": 1,
                    "logical_set_no": 1,
                    "side": "both",
                    "set_type_code": "working",
                    "pause_seconds": invalid,
                    "metrics": [
                        {
                            "metric_kind": "repetitions",
                            "exact_value": "1",
                            "unit_code": "repetition",
                        }
                    ],
                }
            )


def test_confirmation_contract_has_no_client_controlled_chronology() -> None:
    common = {
        "context": {
            "conversation_id": "contract-chronology",
            "message_id": "m1",
            "effective_at": "2026-09-18T00:00:00+00:00",
            "timezone": "UTC",
            "locale": None,
        },
        "revision_id": str(uuid4()),
        "preview_digest": "0" * 64,
        "request_digest": "0" * 64,
    }
    shown = PreviewShownRequest.model_validate(
        {
            **common,
            "shown_message_id": "m1",
            "displayed_text_digest": "0" * 64,
            "cross_dialog": False,
        }
    )
    assert "shown_sequence" not in shown.model_dump()
    confirmed = ConfirmRequest.model_validate(
        {
            **common,
            "confirmation_kind": "normal",
            "confirmation_message_id": "m1",
            "explicit_intent": "yes",
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        }
    )
    assert "confirmation_sequence" not in confirmed.model_dump()


@pytest.mark.parametrize("lifecycle", [False, True], ids=["cmd-05", "cmd-23-reschedule"])
def test_plan_requests_reject_client_equipment_snapshots(lifecycle: bool) -> None:
    plan = {
        "planned_date": "2026-09-25",
        "planned_time": "18:00:00",
        "program_id": None,
        "program_version_id": None,
        "day_template_id": None,
        "plan_type": "training",
        "name": "Closed request snapshot test",
        "purpose": None,
        "comment": None,
        "temporary_conditions": {
            "place": None,
            "equipment_additions": [],
            "equipment_removals": [],
        },
        "blocks": [
            {
                "position": 1,
                "block_type_code": "straight",
                "name": None,
                "declared_rounds": None,
                "rest_seconds": None,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": str(uuid4()),
                        "circuit_position": None,
                        "equipment_snapshot": {"client": "must be rejected"},
                        "comment": None,
                        "sets": [
                            {
                                "position": 1,
                                "round_no": None,
                                "logical_set_no": 1,
                                "side": "both",
                                "set_type_code": "working",
                                "metrics": [
                                    {
                                        "metric_kind": "repetitions",
                                        "exact_value": "5",
                                        "unit_code": "repetition",
                                    }
                                ],
                                "loads": [
                                    {
                                        "position": 1,
                                        "load_kind": "external",
                                        "equipment_id": None,
                                        "equipment_snapshot": {"client": "must be rejected"},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    context = {
        "conversation_id": "contract-plan-snapshot",
        "message_id": "plan-snapshot",
        "effective_at": "2026-09-23T00:00:00+00:00",
        "timezone": "Asia/Novokuznetsk",
        "locale": "ru",
    }
    body: dict[str, Any] = {
        "context": context,
        "request_digest": "0" * 64,
    }
    endpoint = "/api/v1/commands/plan-drafts"
    if lifecycle:
        endpoint = "/api/v1/commands/plan-lifecycle-drafts"
        body.update(
            {
                "intent": "reschedule",
                "source_ref": {
                    "object_type": "workout_plan",
                    "object_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                },
                "expected_current_revision_id": str(uuid4()),
                "reason": None,
                "replacement_plan": plan,
                "replacement_recommendations": [],
            }
        )
        model = PlanLifecycleRequest
    else:
        body.update({"plan": plan, "recommendations": []})
        model = PlanDraftRequest

    response = client().post(
        endpoint,
        headers={**required_headers(), "Idempotency-Key": "closed-plan-snapshots"},
        json=body,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    with pytest.raises(ValidationError) as validation_error:
        model.model_validate(body)
    rejected = [
        issue for issue in validation_error.value.errors() if issue["type"] == "extra_forbidden"
    ]
    assert len(rejected) == 2
    assert all(issue["loc"][-1] == "equipment_snapshot" for issue in rejected)


def test_exercise_load_measurement_error_identifies_field_and_set_loads() -> None:
    raw: dict[str, Any] = {
        "context": {
            "conversation_id": "contract-exercise",
            "message_id": "contract-exercise-load",
            "effective_at": datetime(2026, 10, 31, tzinfo=UTC).isoformat(),
            "timezone": "Asia/Novokuznetsk",
        },
        "intent": "create",
        "exercise": {
            "canonical_name": "Synthetic bench press",
            "category_code": "strength",
            "unilateral": False,
            "status_code": "active",
            "aliases": [],
            "muscle_roles": [
                {"kind": "create_by_name", "name": "Synthetic chest", "status_code": "active", "role": "primary"}
            ],
            "measurements": [
                {"metric_kind": "repetitions", "requiredness": "required", "unit_family": "count"},
                {"metric_kind": "load", "requiredness": "required", "unit_family": "mass"},
            ],
            "equipment_requirements": [],
        },
    }
    normalized = ExerciseCatalogDraftRequest.model_validate(
        {**raw, "request_digest": "0" * 64}
    ).model_dump(mode="json")
    normalized.pop("request_digest")
    raw["request_digest"] = digest_payload(normalized)
    app = create_app(
        Settings(api_token=SecretStr(TEST_TOKEN)),
        context_service=ContextService(lambda **_: {}),
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/commands/exercise-catalog-drafts",
        headers={**required_headers(), "Idempotency-Key": "synthetic-exercise-load"},
        json=raw,
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["field_errors"][0]["path"] == "exercise.measurements[1].metric_kind"
    assert "planned or performed set" in error["field_errors"][0]["message_safe"]
