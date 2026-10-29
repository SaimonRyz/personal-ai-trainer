from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from trainer_mcp import server
from trainer_mcp.write_tools import PlanLifecycleInput

from trainer.application.context import ContextService
from trainer.transport.http.contracts import PlanLifecycleRequest


def payload(intent: str) -> dict[str, Any]:
    revision = str(uuid4())
    result: dict[str, Any] = {
        "intent": intent,
        "source_ref": {
            "object_type": "workout_plan",
            "object_id": str(uuid4()),
            "revision_id": revision,
        },
        "expected_current_revision_id": revision,
        "reason": "Synthetic plan transition",
    }
    if intent == "reschedule":
        result["replacement_plan"] = {
            "planned_date": "2026-10-20",
            "plan_type": "training",
            "name": "Synthetic plan",
            "temporary_conditions": {
                "place": None,
                "equipment_additions": [],
                "equipment_removals": [],
            },
            "blocks": [
                {
                    "position": 1,
                    "block_type_code": "straight",
                    "exercises": [
                        {
                            "position": 1,
                            "exercise_id": str(uuid4()),
                            "sets": [
                                {
                                    "position": 1,
                                    "logical_set_no": 1,
                                    "set_type_code": "working",
                                    "metrics": [
                                        {
                                            "metric_kind": "repetitions",
                                            "exact_value": "8",
                                            "unit_code": "rep",
                                        }
                                    ],
                                    "loads": [],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result["replacement_recommendations"] = [
            {
                "recommendation_kind": "exercise_selection",
                "decision_text": "Synthetic replacement",
                "confidence": "insufficient",
                "revisit_condition": "When evidence is available",
                "fact_links": [],
                "calculation_links": [],
                "conclusion_links": [],
                "assumptions": {},
                "alternatives": {},
                "short_explanation": "No verified evidence linked",
                "detailed_explanation": None,
            }
        ]
    return result


@pytest.mark.parametrize("intent", ["cancel", "miss", "reschedule"])
def test_plan_lifecycle_matches_api_and_keeps_reschedule_in_one_request(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
) -> None:
    captured: list[dict[str, Any]] = []
    envelope = {"data": {"draft_id": "synthetic"}, "meta": {"api_version": "v1"}}

    def capture(path: str, body: Any, correlation: Any, causation: Any, key: Any) -> Any:
        captured.append({"path": path, "body": body, "key": key})
        return envelope

    monkeypatch.setattr(server, "api_call", capture)
    transition = PlanLifecycleInput.model_validate(payload(intent))
    tool = next(
        t for t in server.mcp._tool_manager.list_tools() if t.name == "create_plan_lifecycle_draft"
    )
    result = tool.fn(
        conversation_id="synthetic",
        message_id="synthetic",
        timezone="UTC",
        transition=transition,
        correlation_id=uuid4(),
        causation_id=uuid4(),
        idempotency_key="synthetic-plan-transition",
    )
    assert len(captured) == 1
    request = captured[0]
    assert request["path"] == "/api/v1/commands/plan-lifecycle-drafts"
    assert request["key"] == "synthetic-plan-transition"
    body = request["body"]
    PlanLifecycleRequest.model_validate(body)
    ContextService.verify_digest(body, body["request_digest"])
    assert body["source_ref"] == transition.source_ref.model_dump(mode="json")
    assert body["expected_current_revision_id"] == str(transition.expected_current_revision_id)
    assert result is envelope
    if intent == "reschedule":
        assert transition.replacement_plan is not None
        assert body["replacement_plan"] == transition.replacement_plan.model_dump(mode="json")
        assert body["replacement_recommendations"][0]["confidence"] == "insufficient"
    else:
        assert body["replacement_plan"] is None
        assert body["replacement_recommendations"] is None


@pytest.mark.parametrize("field", ["replacement_plan", "replacement_recommendations"])
def test_reschedule_requires_both_replacement_parts(field: str) -> None:
    value = payload("reschedule")
    del value[field]
    with pytest.raises(ValidationError):
        PlanLifecycleInput.model_validate(value)


@pytest.mark.parametrize("intent", ["cancel", "miss"])
def test_other_transitions_reject_replacement(intent: str) -> None:
    value = payload("reschedule")
    value["intent"] = intent
    with pytest.raises(ValidationError):
        PlanLifecycleInput.model_validate(value)


def test_plan_transition_rejects_mismatched_or_missing_source_revision() -> None:
    value = payload("cancel")
    value["expected_current_revision_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="source revision"):
        PlanLifecycleInput.model_validate(value)
    value = payload("cancel")
    value["source_ref"]["revision_id"] = None
    with pytest.raises(ValidationError):
        PlanLifecycleInput.model_validate(value)


@pytest.mark.parametrize("intent", ["archive", "delete", "complete"])
def test_plan_transition_rejects_unapproved_actions(intent: str) -> None:
    with pytest.raises(ValidationError):
        PlanLifecycleInput.model_validate(payload(intent))
