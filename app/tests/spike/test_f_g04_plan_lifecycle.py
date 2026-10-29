from __future__ import annotations

from uuid import uuid4

import pytest
from mcp.client.streamable_http import streamable_http_client
from trainer_mcp.write_tools import PlanLifecycleInput

from mcp import ClientSession


def lifecycle_arguments(intent: str) -> dict[str, object]:
    revision_id = str(uuid4())
    transition: dict[str, object] = {
        "intent": intent,
        "source_ref": {
            "object_type": "workout_plan",
            "object_id": str(uuid4()),
            "revision_id": revision_id,
        },
        "expected_current_revision_id": revision_id,
        "reason": "Synthetic lifecycle transport test",
    }
    if intent == "reschedule":
        transition["replacement_plan"] = {
            "planned_date": "2026-10-20",
            "plan_type": "training",
            "name": "Synthetic replacement plan",
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
        transition["replacement_recommendations"] = [
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
    return transition


@pytest.mark.spike
@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["cancel", "miss", "reschedule"])
async def test_cmd23_lifecycle_uses_exact_mcp_to_api_route_without_domain_effect(
    f_g04_lifecycle_mcp_url: str,
    intent: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    idempotency_key = f"synthetic-cmd23-{uuid4().hex}"
    transition = lifecycle_arguments(intent)
    normalized_transition = PlanLifecycleInput.model_validate(transition).model_dump(mode="json")
    canonical_text = "SYNTHETIC PLAN LIFECYCLE PREVIEW\nNo domain effect."

    async with (
        streamable_http_client(f_g04_lifecycle_mcp_url) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        draft = await session.call_tool(
            "create_plan_lifecycle_draft",
            arguments={
                "conversation_id": "synthetic-f-g04-lifecycle",
                "message_id": f"synthetic-cmd23-{intent}",
                "timezone": "Asia/Novokuznetsk",
                "transition": transition,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": idempotency_key,
            },
        )
        assert draft.isError is False
        assert draft.structuredContent is not None
        draft_data = draft.structuredContent["data"]
        assert draft_data["synthetic"] is True
        assert draft_data["effect"] == "none"

        preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": "synthetic-f-g04-lifecycle",
                "message_id": f"synthetic-cmd23-preview-{intent}",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": draft_data["revision_id"],
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"{idempotency_key}-preview",
            },
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        preview_data = preview.structuredContent["data"]
        assert preview_data["revision_id"] == draft_data["revision_id"]
        assert preview_data["complete"] is True
        assert preview_data["canonical_text"] == canonical_text

        shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": preview_data["revision_id"],
                "preview_digest": preview_data["digest"],
                "canonical_text": canonical_text,
                "conversation_id": "synthetic-f-g04-lifecycle",
                "shown_message_id": f"synthetic-cmd23-shown-{intent}",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"{idempotency_key}-shown",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert shown.isError is False

        confirmed = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": preview_data["revision_id"],
                "preview_digest": preview_data["digest"],
                "conversation_id": "synthetic-f-g04-lifecycle",
                "confirmation_message_id": f"synthetic-cmd23-confirm-{intent}",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"{idempotency_key}-confirm",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Confirm this synthetic preview only",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
            },
        )
        assert confirmed.isError is False
        assert confirmed.structuredContent is not None
        commit_result = confirmed.structuredContent["data"]

    envelope = draft.structuredContent
    assert envelope is not None
    data = envelope["data"]
    assert data["synthetic"] is True
    assert data["effect"] == "none"
    assert data["intent"] == intent
    assert data["source_ref"] == transition["source_ref"]
    assert data["expected_current_revision_id"] == transition["expected_current_revision_id"]
    assert data["idempotency_key"] == idempotency_key
    assert envelope["meta"]["correlation_id"] == correlation_id
    assert envelope["meta"]["causation_id"] == causation_id
    if intent == "reschedule":
        assert data["replacement_plan"] == normalized_transition["replacement_plan"]
        assert (
            data["replacement_recommendations"]
            == normalized_transition["replacement_recommendations"]
        )
    else:
        assert data["replacement_plan"] is None
        assert data["replacement_recommendations"] is None
    assert commit_result["status"] == "committed"
    assert commit_result["domain_changes"] == []
    assert commit_result["created_revisions"] == []
