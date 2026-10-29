from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from mcp.client.streamable_http import streamable_http_client
from trainer_mcp.contracts import (
    PlanTreeInput,
    ProgramIdentityInput,
    ProgramRecommendationInput,
    ProgramVersionDraftInput,
)

from mcp import ClientSession
from trainer.domain.context import digest_payload
from trainer.transport.http.contracts import (
    DraftResult,
    ExerciseSearchResponse,
    PlanningContextResponse,
    PreviewResult,
    ProfileContextResponse,
    WorkoutHistoryResponse,
)
from trainer.transport.http.evidence_contracts import EligibleConclusionsResponse


async def _reject_program_confirmation_then_prepare_publication(
    session: ClientSession,
    draft: DraftResult,
    preview: PreviewResult,
    correlation_id: str,
    causation_id: str,
) -> PreviewResult:
    conversation_id = "synthetic-f-g02-program"
    technical_draft_confirmation = await session.call_tool(
        "confirm_preview",
        arguments={
            "preview_id": str(preview.preview_id),
            "revision_id": str(preview.revision_id),
            "preview_digest": preview.digest,
            "conversation_id": conversation_id,
            "confirmation_message_id": "synthetic-program-draft-confirmation-rejected",
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "idempotency_key": f"synthetic-program-draft-confirm-{uuid4().hex}",
            "timezone": "Asia/Novokuznetsk",
            "explicit_intent": "Synthetic попытка подтвердить технический черновик программы",
            "confirmation_mode": "same_dialog",
            "confirmation_kind": "normal",
        },
    )
    assert technical_draft_confirmation.isError is False
    assert technical_draft_confirmation.structuredContent is not None
    assert technical_draft_confirmation.structuredContent["error"]["code"] == (
        "confirmation_not_supported"
    )

    publication_result = await session.call_tool(
        "create_program_publication_draft",
        arguments={
            "conversation_id": conversation_id,
            "message_id": "synthetic-program-publication-request",
            "timezone": "Asia/Novokuznetsk",
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "idempotency_key": f"synthetic-program-publication-{uuid4().hex}",
            "source_draft_id": str(draft.draft_id),
            "source_revision_id": str(draft.revision_id),
            "publication_reason": "Synthetic CMD-04-only confirmation test",
        },
    )
    assert publication_result.isError is False
    assert publication_result.structuredContent is not None
    publication_draft = DraftResult.model_validate(publication_result.structuredContent["data"])

    preview_result = await session.call_tool(
        "render_preview",
        arguments={
            "conversation_id": conversation_id,
            "message_id": "synthetic-program-publication-preview",
            "timezone": "Asia/Novokuznetsk",
            "revision_id": str(publication_draft.revision_id),
            "detail_level": "full",
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "idempotency_key": f"synthetic-publication-preview-{uuid4().hex}",
        },
    )
    assert preview_result.isError is False
    assert preview_result.structuredContent is not None
    publication_preview = PreviewResult.model_validate(preview_result.structuredContent["data"])
    assert publication_preview.confirmation_kind == "publication"
    assert publication_preview.complete is True
    assert publication_preview.canonical_payload["source_draft_id"] == str(draft.draft_id)
    assert publication_preview.canonical_payload["source_revision_id"] == str(draft.revision_id)

    publication_shown_id = "synthetic-program-publication-preview-shown"
    publication_shown = await session.call_tool(
        "acknowledge_preview_shown",
        arguments={
            "preview_id": str(publication_preview.preview_id),
            "revision_id": str(publication_preview.revision_id),
            "preview_digest": publication_preview.digest,
            "canonical_text": publication_preview.canonical_text,
            "conversation_id": conversation_id,
            "shown_message_id": publication_shown_id,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "idempotency_key": f"synthetic-publication-shown-{uuid4().hex}",
            "timezone": "Asia/Novokuznetsk",
        },
    )
    assert publication_shown.isError is False
    assert publication_shown.structuredContent is not None
    assert publication_shown.structuredContent["data"]["shown_message_id"] == publication_shown_id

    publication_confirmation = await session.call_tool(
        "confirm_preview",
        arguments={
            "preview_id": str(publication_preview.preview_id),
            "revision_id": str(publication_preview.revision_id),
            "preview_digest": publication_preview.digest,
            "conversation_id": conversation_id,
            "confirmation_message_id": "synthetic-program-publication-confirmed",
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "idempotency_key": f"synthetic-publication-confirm-{uuid4().hex}",
            "timezone": "Asia/Novokuznetsk",
            "explicit_intent": "Подтверждаю публикацию именно показанной синтетической программы",
            "confirmation_mode": "same_dialog",
            "confirmation_kind": "publication",
        },
    )
    assert publication_confirmation.isError is False
    assert publication_confirmation.structuredContent is not None
    publication_commit = publication_confirmation.structuredContent["data"]
    assert publication_commit["status"] == "committed"
    assert publication_commit["domain_changes"] == []
    assert publication_commit["created_revisions"] == []
    return publication_preview


@pytest.mark.spike
@pytest.mark.asyncio
async def test_onboarding_draft_reaches_full_preview_without_saving(
    f_g02_plan_mcp_url: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    onboarding = {
        "profile": {
            "birth_date": None,
            "sex": None,
            "timezone": "Asia/Novokuznetsk",
            "started_on": "2026-10-01",
            "desired_sessions_per_week": 4,
            "usual_place_id": "30000000-0000-4000-8000-000000000224",
            "comment": "Synthetic profile context only.",
        },
        "goals": [
            {
                "goal_id": None,
                "parent_goal_id": None,
                "name": "Synthetic dated strength goal",
                "description": None,
                "goal_type": "strength",
                "start_value": None,
                "target_value": "100",
                "unit_code": "kilogram",
                "set_on": "2026-10-01",
                "target_date": "2027-04-01",
                "priority": 1,
                "status": "active",
                "comment": None,
            }
        ],
        "places": [
            {
                "place_id": "30000000-0000-4000-8000-000000000224",
                "name": "Synthetic home gym",
                "place_type": "home",
                "status": "active",
                "comment": None,
            }
        ],
        "equipment": [
            {
                "equipment_id": "30000000-0000-4000-8000-000000000225",
                "equipment_type": "barbell",
                "name": "Synthetic barbell",
                "maker": None,
                "brand": None,
                "model": None,
                "quantity": 1,
                "quantity_unit_code": "piece",
                "condition": "usable",
                "characteristics": {
                    "mass_per_item_kg": "7",
                    "bar_sleeve_diameter_mm": None,
                    "plate_bore_diameter_mm": None,
                },
                "comment": None,
            }
        ],
    }

    async with (
        streamable_http_client(f_g02_plan_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "create_onboarding_draft",
            arguments={
                **onboarding,
                "conversation_id": "synthetic-f-g02-onboarding",
                "message_id": "synthetic-onboarding-draft",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-onboarding-{uuid4().hex}",
            },
        )
        assert result.isError is False
        assert result.structuredContent is not None
        draft = DraftResult.model_validate(result.structuredContent["data"])
        assert str(draft.revision_id) == "30000000-0000-4000-8000-000000000222"

        preview_result = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": "synthetic-f-g02-onboarding",
                "message_id": "synthetic-onboarding-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": str(draft.revision_id),
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-onboarding-preview-{uuid4().hex}",
            },
        )
        assert preview_result.isError is False
        assert preview_result.structuredContent is not None
        preview_envelope = preview_result.structuredContent

    preview = PreviewResult.model_validate(preview_envelope["data"])
    assert str(preview.preview_id) == "30000000-0000-4000-8000-000000000223"
    assert preview.revision_id == draft.revision_id
    assert preview.complete is True
    assert preview.confirmation_kind == "normal"
    assert preview.canonical_payload == {
        "fixture": "synthetic-only",
        "draft_revision_id": str(draft.revision_id),
        "detail_level": "full",
        "draft_kind": "onboarding",
        "profile": onboarding["profile"],
        "goals": onboarding["goals"],
        "places": onboarding["places"],
        "equipment": onboarding["equipment"],
    }
    assert preview.canonical_text == (
        "СИНТЕТИЧЕСКОЕ ПЕРВОЕ ЗНАКОМСТВО — только тест transport.\n"
        "Профиль, цель с датой, обычное место и инвентарь собраны одним черновиком.\n"
        "Сохранение и подтверждение не выполнялись."
    )
    assert preview.shown_message_id is None
    assert preview_envelope["meta"]["correlation_id"] == correlation_id
    assert preview_envelope["meta"]["causation_id"] == causation_id


@pytest.mark.spike
@pytest.mark.asyncio
async def test_plan_draft_requires_full_preview_shown_before_explicit_confirmation(
    f_g02_plan_mcp_url: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    goal_id = "30000000-0000-4000-8000-000000000001"
    goal_revision_id = "30000000-0000-4000-8000-000000000002"
    exercise_id = "30000000-0000-4000-8000-000000000003"
    recommendation = {
        "recommendation_kind": "plan_structure",
        "decision_text": "Synthetic dated recommendation for transport testing.",
        "confidence": "moderate",
        "revisit_condition": "Re-evaluate after the synthetic session.",
        "fact_links": [
            {
                "ref": {
                    "object_type": "training_goal",
                    "object_id": goal_id,
                    "revision_id": goal_revision_id,
                },
                "role": "constraint",
                "reason": "Synthetic dated goal reference.",
                "snapshot_digest": "c" * 64,
            }
        ],
        "calculation_links": [],
        "conclusion_links": [
            {
                "conclusion_version_id": "30000000-0000-4000-8000-000000000004",
                "role": "supports",
            }
        ],
        "assumptions": {"synthetic": True},
        "alternatives": {},
        "short_explanation": "Synthetic fixture only; not a training recommendation.",
    }
    plan: dict[str, Any] = {
        "planned_date": "2026-10-12",
        "planned_time": "18:00:00",
        "program_id": None,
        "program_version_id": None,
        "day_template_id": None,
        "plan_type": "training",
        "name": "Synthetic dated plan",
        "purpose": "Process-level transport regression",
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
                "rest_seconds": 120,
                "comment": None,
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": exercise_id,
                        "circuit_position": None,
                        "comment": None,
                        "sets": [
                            {
                                "position": position,
                                "round_no": None,
                                "logical_set_no": position,
                                "side": "not_applicable",
                                "set_type_code": "working",
                                "target_rpe_min": None,
                                "target_rpe_max": None,
                                "target_rir_min": None,
                                "target_rir_max": None,
                                "tempo": None,
                                "pause_seconds": None,
                                "rest_after_seconds": 120,
                                "metrics": [
                                    {
                                        "metric_kind": "repetitions",
                                        "exact_value": "5",
                                        "min_value": None,
                                        "max_value": None,
                                        "unit_code": "rep",
                                        "custom_definition": None,
                                    }
                                ],
                                "loads": [],
                            }
                            for position in range(1, 4)
                        ],
                    }
                ],
            }
        ],
    }
    expected_plan = PlanTreeInput.model_validate(plan).model_dump(mode="json")
    expected_recommendation = ProgramRecommendationInput.model_validate(recommendation).model_dump(
        mode="json"
    )

    async with (
        streamable_http_client(f_g02_plan_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tool_names = {tool.name for tool in (await session.list_tools()).tools}
        assert {
            "create_plan_draft",
            "render_preview",
            "acknowledge_preview_shown",
            "confirm_preview",
        } <= tool_names

        draft_result = await session.call_tool(
            "create_plan_draft",
            arguments={
                "conversation_id": "synthetic-f-g02-plan",
                "message_id": "synthetic-plan-request",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-plan-{uuid4().hex}",
                "plan": plan,
                "recommendations": [recommendation],
            },
        )
        assert draft_result.isError is False
        assert draft_result.structuredContent is not None
        draft_envelope = draft_result.structuredContent
        draft = DraftResult.model_validate(draft_envelope["data"])

        preview_result = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": "synthetic-f-g02-plan",
                "message_id": "synthetic-plan-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": str(draft.revision_id),
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-preview-{uuid4().hex}",
            },
        )
        assert preview_result.isError is False
        assert preview_result.structuredContent is not None
        preview_envelope = preview_result.structuredContent
        preview_data = preview_envelope["data"]

        premature_confirmation = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": preview_data["revision_id"],
                "preview_digest": preview_data["digest"],
                "conversation_id": "synthetic-f-g02-plan",
                "confirmation_message_id": "synthetic-plan-premature-confirmation",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-plan-premature-{uuid4().hex}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Synthetic attempt before preview is shown",
                "confirmation_mode": "same_dialog",
            },
        )
        assert premature_confirmation.isError is False
        assert premature_confirmation.structuredContent is not None
        assert premature_confirmation.structuredContent["error"]["code"] == (
            "stale_or_unshown_preview"
        )

        shown_message_id = "synthetic-plan-preview-shown"
        shown_result = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": preview_data["revision_id"],
                "preview_digest": preview_data["digest"],
                "canonical_text": preview_data["canonical_text"],
                "conversation_id": "synthetic-f-g02-plan",
                "shown_message_id": shown_message_id,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-plan-shown-{uuid4().hex}",
                "timezone": "Asia/Novokuznetsk",
            },
        )
        assert shown_result.isError is False
        assert shown_result.structuredContent is not None
        shown_data = shown_result.structuredContent["data"]
        assert shown_data["preview_id"] == preview_data["preview_id"]
        assert shown_data["shown_message_id"] == shown_message_id

        confirmation_result = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": preview_data["revision_id"],
                "preview_digest": preview_data["digest"],
                "conversation_id": "synthetic-f-g02-plan",
                "confirmation_message_id": "synthetic-plan-owner-confirmation",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-plan-confirm-{uuid4().hex}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Подтверждаю именно показанный синтетический план",
                "confirmation_mode": "same_dialog",
            },
        )
        assert confirmation_result.isError is False
        assert confirmation_result.structuredContent is not None
        confirmation_data = confirmation_result.structuredContent["data"]

    preview = PreviewResult.model_validate(preview_envelope["data"])
    assert str(draft.draft_id) == "30000000-0000-4000-8000-000000000101"
    assert str(draft.revision_id) == "30000000-0000-4000-8000-000000000102"
    assert str(preview.preview_id) == "30000000-0000-4000-8000-000000000103"
    assert preview.revision_id == draft.revision_id
    assert preview.confirmation_kind == "normal"
    assert preview.complete is True
    assert preview.canonical_payload == {
        "fixture": "synthetic-only",
        "draft_revision_id": str(draft.revision_id),
        "detail_level": "full",
        "draft_kind": "plan",
        "plan": expected_plan,
        "recommendations": [expected_recommendation],
    }
    canonical_payload = cast(dict[str, Any], preview.canonical_payload)
    preview_plan = canonical_payload["plan"]
    assert preview_plan["planned_date"] == "2026-10-12"
    exercise = preview_plan["blocks"][0]["exercises"][0]
    assert exercise["exercise_id"] == exercise_id
    assert [item["metrics"][0]["exact_value"] for item in exercise["sets"]] == [
        "5",
        "5",
        "5",
    ]
    preview_recommendation = canonical_payload["recommendations"][0]
    assert preview_recommendation["fact_links"][0]["ref"]["revision_id"] == goal_revision_id
    assert preview_recommendation["conclusion_links"][0]["conclusion_version_id"] == (
        "30000000-0000-4000-8000-000000000004"
    )
    assert preview.canonical_text == (
        "СИНТЕТИЧЕСКИЙ ПЛАН — только тест transport.\n"
        "Дата: 2026-10-12.\n"
        "Жим лёжа: 3 подхода по 5 повторений.\n"
        "На момент предпросмотра подтверждение ещё не выполнялось."
    )
    assert preview.shown_message_id is None
    assert preview_envelope["meta"]["correlation_id"] == correlation_id
    assert preview_envelope["meta"]["causation_id"] == causation_id
    assert confirmation_data["status"] == "committed"
    assert confirmation_data["domain_changes"] == []
    assert confirmation_data["created_revisions"] == []


@pytest.mark.spike
@pytest.mark.asyncio
async def test_only_cmd04_program_publication_can_be_confirmed(
    f_g02_plan_mcp_url: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    goal_revision_id = "30000000-0000-4000-8000-000000000012"
    exercise_id = "30000000-0000-4000-8000-000000000013"
    exercise_revision_id = "30000000-0000-4000-8000-000000000014"
    conclusion_version_id = "30000000-0000-4000-8000-000000000015"
    recommendation = {
        "recommendation_kind": "program_structure",
        "decision_text": "Synthetic program recommendation for transport testing.",
        "confidence": "moderate",
        "revisit_condition": "Review after the synthetic program period.",
        "fact_links": [
            {
                "ref": {
                    "object_type": "training_goal",
                    "object_id": "30000000-0000-4000-8000-000000000011",
                    "revision_id": goal_revision_id,
                },
                "role": "constraint",
                "reason": "Synthetic dated goal reference.",
                "snapshot_digest": "e" * 64,
            }
        ],
        "calculation_links": [],
        "conclusion_links": [{"conclusion_version_id": conclusion_version_id, "role": "supports"}],
        "assumptions": {"synthetic": True},
        "alternatives": {},
        "short_explanation": "Synthetic fixture only; not a training recommendation.",
    }
    program = {
        "name": "Synthetic dated strength program",
        "description": None,
        "starts_on": "2026-10-12",
        "ends_on": "2026-12-31",
        "status_code": "active",
        "comment": None,
    }
    version = {
        "version_label": "synthetic-v1",
        "rationale": "Process-level transport regression.",
        "period_start": "2026-10-12",
        "period_end": "2026-12-31",
        "templates": [
            {
                "position": 1,
                "day_type_code": "training",
                "purpose": "Synthetic strength training day.",
                "blocks": [
                    {
                        "position": 1,
                        "block_type_code": "straight",
                        "name": None,
                        "declared_rounds": None,
                        "rest_seconds": 120,
                        "comment": None,
                        "exercises": [
                            {
                                "position": 1,
                                "exercise_ref": {
                                    "object_type": "exercise",
                                    "object_id": exercise_id,
                                    "revision_id": exercise_revision_id,
                                },
                                "circuit_position": None,
                                "comment": None,
                                "sets": [
                                    {
                                        "position": position,
                                        "round_no": None,
                                        "logical_set_no": position,
                                        "side": "not_applicable",
                                        "set_type_code": "working",
                                        "tempo": None,
                                        "pause_seconds": None,
                                        "rest_after_seconds": 120,
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
                                    for position in range(1, 4)
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "recommendations": [recommendation],
    }
    expected_program = ProgramIdentityInput.model_validate(program).model_dump(mode="json")
    expected_version = ProgramVersionDraftInput.model_validate(version).model_dump(mode="json")

    async with (
        streamable_http_client(f_g02_plan_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tool_names = {tool.name for tool in (await session.list_tools()).tools}
        assert {
            "create_program_draft",
            "create_program_publication_draft",
            "render_preview",
            "confirm_preview",
        } <= tool_names

        draft_result = await session.call_tool(
            "create_program_draft",
            arguments={
                "conversation_id": "synthetic-f-g02-program",
                "message_id": "synthetic-program-request",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-program-{uuid4().hex}",
                "goal_revision_id": goal_revision_id,
                "program": program,
                "version": version,
            },
        )
        assert draft_result.isError is False
        assert draft_result.structuredContent is not None
        draft_envelope = draft_result.structuredContent
        draft = DraftResult.model_validate(draft_envelope["data"])

        preview_result = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": "synthetic-f-g02-program",
                "message_id": "synthetic-program-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": str(draft.revision_id),
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"synthetic-program-preview-{uuid4().hex}",
            },
        )
        assert preview_result.isError is False
        assert preview_result.structuredContent is not None
        preview_envelope = preview_result.structuredContent

    preview = PreviewResult.model_validate(preview_envelope["data"])
    assert str(draft.draft_id) == "30000000-0000-4000-8000-000000000111"
    assert str(draft.revision_id) == "30000000-0000-4000-8000-000000000112"
    assert str(preview.preview_id) == "30000000-0000-4000-8000-000000000113"
    assert preview.revision_id == draft.revision_id
    assert preview.confirmation_kind == "normal"
    assert preview.complete is True
    assert preview.canonical_payload == {
        "fixture": "synthetic-only",
        "draft_revision_id": str(draft.revision_id),
        "detail_level": "full",
        "draft_kind": "program",
        "goal_revision_id": goal_revision_id,
        "intent": "create",
        "program": expected_program,
        "program_id": None,
        "expected_current_version_id": None,
        "state": "open",
        "version": expected_version,
    }
    canonical_payload = cast(dict[str, Any], preview.canonical_payload)
    exercise_ref = canonical_payload["version"]["templates"][0]["blocks"][0]["exercises"][0][
        "exercise_ref"
    ]
    assert exercise_ref == {
        "object_type": "exercise",
        "object_id": exercise_id,
        "revision_id": exercise_revision_id,
    }
    preview_recommendation = canonical_payload["version"]["recommendations"][0]
    assert preview_recommendation["fact_links"][0]["ref"]["revision_id"] == goal_revision_id
    assert preview_recommendation["conclusion_links"][0]["conclusion_version_id"] == (
        conclusion_version_id
    )
    assert preview.canonical_text == (
        "СИНТЕТИЧЕСКАЯ ПРОГРАММА — только тест transport.\n"
        "Период: 2026-10-12 — 2026-12-31.\n"
        "Сохранена точная редакция упражнения и цели.\n"
        "Публикация и подтверждение не выполнялись."
    )
    assert preview.shown_message_id is None
    assert preview_envelope["meta"]["correlation_id"] == correlation_id
    assert preview_envelope["meta"]["causation_id"] == causation_id

    async with (
        streamable_http_client(f_g02_plan_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        publication_preview = await _reject_program_confirmation_then_prepare_publication(
            session, draft, preview, correlation_id, causation_id
        )

    assert publication_preview.canonical_text == (
        "СИНТЕТИЧЕСКАЯ ПУБЛИКАЦИЯ ПРОГРАММЫ — отдельный draft для теста.\n"
        "Источник: точная открытая revision технического черновика программы.\n"
        "Публикация ещё не выполнена."
    )


@pytest.mark.spike
@pytest.mark.asyncio
async def test_planning_inputs_reach_mcp_with_exact_references_and_synthetic_evidence(
    f_g02_plan_mcp_url: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    common = {
        "conversation_id": "synthetic-f-g02-planning-context",
        "timezone": "Asia/Novokuznetsk",
        "correlation_id": correlation_id,
        "causation_id": causation_id,
    }
    async with (
        streamable_http_client(f_g02_plan_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        profile_result = await session.call_tool(
            "get_profile_context",
            arguments={**common, "message_id": "synthetic-profile", "purpose": "plan preparation"},
        )
        planning_result = await session.call_tool(
            "get_planning_context",
            arguments={
                **common,
                "message_id": "synthetic-planning-context",
                "target_date": "2026-10-12",
            },
        )
        exercise_result = await session.call_tool(
            "search_exercises",
            arguments={
                **common,
                "message_id": "synthetic-exercise-search",
                "text": "synthetic bench press",
                "metric_kinds": ["repetitions"],
                "equipment_types": ["barbell"],
            },
        )
        assert not profile_result.isError
        assert not planning_result.isError
        assert not exercise_result.isError
        assert profile_result.structuredContent is not None
        assert planning_result.structuredContent is not None
        assert exercise_result.structuredContent is not None
        profile = ProfileContextResponse.model_validate(
            profile_result.structuredContent["data"]["response"]
        )
        planning = PlanningContextResponse.model_validate(
            planning_result.structuredContent["data"]["response"]
        )
        exercises = ExerciseSearchResponse.model_validate(
            exercise_result.structuredContent["data"]["response"]
        )
        applicability_snapshot = {
            "synthetic": True,
            "decision_date": planning.target_date.isoformat(),
            "profile_ref": profile.profile_ref.model_dump(mode="json")
            if profile.profile_ref
            else None,
            "goal_refs": [item.ref.model_dump(mode="json") for item in profile.goals],
            "exercise_ref": exercises.items[0].exercise_ref.model_dump(mode="json"),
            "equipment_inventory_digest": planning.equipment_inventory_digest,
        }
        evidence_result = await session.call_tool(
            "get_eligible_conclusions",
            arguments={
                **common,
                "message_id": "synthetic-evidence-eligibility",
                "topic": "synthetic strength planning",
                "decision_date": "2026-10-12",
                "applicability_snapshot": applicability_snapshot,
            },
        )

    assert not profile_result.isError
    assert not planning_result.isError
    assert not exercise_result.isError
    assert not evidence_result.isError
    assert profile_result.structuredContent is not None
    assert planning_result.structuredContent is not None
    assert exercise_result.structuredContent is not None
    assert evidence_result.structuredContent is not None
    profile = ProfileContextResponse.model_validate(
        profile_result.structuredContent["data"]["response"]
    )
    planning = PlanningContextResponse.model_validate(
        planning_result.structuredContent["data"]["response"]
    )
    exercises = ExerciseSearchResponse.model_validate(
        exercise_result.structuredContent["data"]["response"]
    )
    evidence = EligibleConclusionsResponse.model_validate(
        evidence_result.structuredContent["data"]["response"]
    )

    assert profile.profile_ref is not None
    assert str(profile.profile_ref.revision_id) == "30000000-0000-4000-8000-000000000202"
    assert len(profile.goals) == 1
    assert str(profile.goals[0].ref.revision_id) == "30000000-0000-4000-8000-000000000204"
    assert profile.goals[0].goal.target_date.isoformat() == "2027-04-01"
    assert len(profile.equipment) == 1
    assert str(profile.equipment[0].ref.revision_id) == "30000000-0000-4000-8000-000000000206"
    assert planning.target_date.isoformat() == "2026-10-12"
    assert planning.facts[0].ref == profile.goals[0].ref
    assert planning.equipment_inventory_snapshot[0].ref == profile.equipment[0].ref
    assert str(planning.eligible_conclusion_version_ids[0]) == (
        "30000000-0000-4000-8000-000000000209"
    )
    assert len(exercises.items) == 1
    assert str(exercises.items[0].exercise_ref.revision_id) == (
        "30000000-0000-4000-8000-000000000208"
    )
    assert exercises.items[0].equipment_types == ["barbell"]
    assert len(evidence.items) == 1
    assert evidence.items[0].status == "verified"
    assert evidence.items[0].applicability_assessment == {"synthetic": True, "eligible": True}
    assert [str(source_id) for source_id in evidence.items[0].source_ids] == [
        "30000000-0000-4000-8000-000000000210"
    ]
    assert evidence_result.structuredContent["data"]["response"] == evidence.model_dump(mode="json")
    assert evidence_result.structuredContent["meta"]["correlation_id"] == correlation_id
    assert evidence_result.structuredContent["meta"]["causation_id"] == causation_id


@pytest.mark.spike
@pytest.mark.asyncio
async def test_plan_draft_uses_exact_ids_from_complete_synthetic_reads(
    f_g02_plan_mcp_url: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    common = {
        "conversation_id": "synthetic-f-g02-exact-planning-chain",
        "timezone": "Asia/Novokuznetsk",
        "correlation_id": correlation_id,
        "causation_id": causation_id,
    }
    async with (
        streamable_http_client(f_g02_plan_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        profile_result = await session.call_tool(
            "get_profile_context",
            arguments={
                **common,
                "message_id": "synthetic-exact-profile",
                "purpose": "plan preparation",
            },
        )
        history_pages: list[WorkoutHistoryResponse] = []
        history_items = []
        history_query: dict[str, Any] = {
            "mode": "all_confirmed",
            "limit": 1,
            "cursor": None,
        }
        for page_no in range(10):
            history_result = await session.call_tool(
                "get_workout_history",
                arguments={
                    **common,
                    "message_id": f"synthetic-complete-history-page-{page_no + 1}",
                    "query": history_query,
                },
            )
            assert history_result.isError is False
            assert history_result.structuredContent is not None
            history_page = WorkoutHistoryResponse.model_validate(
                history_result.structuredContent["data"]["response"]
            )
            if history_pages:
                first_page = history_pages[0]
                assert history_page.snapshot_id == first_page.snapshot_id
                assert history_page.snapshot_confirmed_at == first_page.snapshot_confirmed_at
                assert history_page.total_count == first_page.total_count
            history_pages.append(history_page)
            history_items.extend(history_page.items)
            if history_page.next_cursor is None:
                break
            history_query = {**history_query, "cursor": history_page.next_cursor}
        assert history_pages
        assert history_pages[-1].next_cursor is None
        history = history_pages[0]
        assert history.snapshot_id is not None
        assert history.snapshot_confirmed_at is not None
        assert history.total_count is not None
        planning_result = await session.call_tool(
            "get_planning_context",
            arguments={
                **common,
                "message_id": "synthetic-exact-planning-context",
                "target_date": "2026-10-12",
            },
        )
        exercise_result = await session.call_tool(
            "search_exercises",
            arguments={
                **common,
                "message_id": "synthetic-exact-exercise",
                "text": "synthetic bench press",
                "metric_kinds": ["repetitions"],
                "equipment_types": ["barbell"],
            },
        )
        assert not profile_result.isError
        assert not planning_result.isError
        assert not exercise_result.isError
        assert profile_result.structuredContent is not None
        assert planning_result.structuredContent is not None
        assert exercise_result.structuredContent is not None
        profile = ProfileContextResponse.model_validate(
            profile_result.structuredContent["data"]["response"]
        )
        planning = PlanningContextResponse.model_validate(
            planning_result.structuredContent["data"]["response"]
        )
        exercises = ExerciseSearchResponse.model_validate(
            exercise_result.structuredContent["data"]["response"]
        )
        assert profile.profile_ref is not None
        assert len(profile.goals) == 1
        assert history.total_count == len(history_items) == 2
        history_refs = [item.session_ref.model_dump(mode="json") for item in history_items]
        history_ref_keys = {
            (item.session_ref.object_id, item.session_ref.revision_id) for item in history_items
        }
        assert len(history_ref_keys) == len(history_items)
        assert len(history_pages) == 2
        assert planning.facts[0].ref == profile.goals[0].ref
        assert len(exercises.items) == 1
        exercise_ref = exercises.items[0].exercise_ref
        applicability_snapshot = {
            "synthetic": True,
            "profile_ref": profile.profile_ref.model_dump(mode="json"),
            "goal_ref": profile.goals[0].ref.model_dump(mode="json"),
            "exercise_ref": exercise_ref.model_dump(mode="json"),
            "equipment_inventory_digest": planning.equipment_inventory_digest,
            "history_snapshot": {
                "snapshot_id": str(history.snapshot_id),
                "snapshot_confirmed_at": history.snapshot_confirmed_at.isoformat(),
                "total_count": history.total_count,
                "session_refs": history_refs,
            },
        }
        evidence_result = await session.call_tool(
            "get_eligible_conclusions",
            arguments={
                **common,
                "message_id": "synthetic-exact-evidence",
                "topic": "synthetic strength planning",
                "decision_date": "2026-10-12",
                "applicability_snapshot": applicability_snapshot,
            },
        )
        assert evidence_result.isError is False
        assert evidence_result.structuredContent is not None
        evidence = EligibleConclusionsResponse.model_validate(
            evidence_result.structuredContent["data"]["response"]
        )
        assert len(evidence.items) == 1
        conclusion_id = evidence.items[0].conclusion_version_id

        plan: dict[str, Any] = {
            "planned_date": "2026-10-12",
            "planned_time": None,
            "program_id": None,
            "program_version_id": None,
            "day_template_id": None,
            "plan_type": "training",
            "name": "Synthetic exact-reference plan",
            "purpose": "Process-level reference chaining",
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
                    "rest_seconds": 120,
                    "comment": None,
                    "exercises": [
                        {
                            "position": 1,
                            "exercise_id": str(exercise_ref.object_id),
                            "circuit_position": None,
                            "comment": None,
                            "sets": [
                                {
                                    "position": 1,
                                    "round_no": None,
                                    "logical_set_no": 1,
                                    "side": "not_applicable",
                                    "set_type_code": "working",
                                    "target_rpe_min": None,
                                    "target_rpe_max": None,
                                    "target_rir_min": None,
                                    "target_rir_max": None,
                                    "tempo": None,
                                    "pause_seconds": None,
                                    "rest_after_seconds": 120,
                                    "metrics": [
                                        {
                                            "metric_kind": "repetitions",
                                            "exact_value": "5",
                                            "min_value": None,
                                            "max_value": None,
                                            "unit_code": "rep",
                                            "custom_definition": None,
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
        recommendation = {
            "recommendation_kind": "plan_structure",
            "decision_text": "Synthetic exact-reference recommendation.",
            "confidence": "moderate",
            "revisit_condition": "Review after the synthetic session.",
            "fact_links": [
                {
                    "ref": profile.goals[0].ref.model_dump(mode="json"),
                    "role": "constraint",
                    "reason": "Exact active goal returned by QRY-01.",
                    "snapshot_digest": planning.facts[0].snapshot_digest,
                }
            ]
            + [
                {
                    "ref": item.session_ref.model_dump(mode="json"),
                    "role": "baseline",
                    "reason": "Exact workout revision in the complete synthetic history snapshot.",
                    "snapshot_digest": digest_payload(item.workout),
                }
                for item in history_items
            ],
            "calculation_links": [],
            "conclusion_links": [{"conclusion_version_id": str(conclusion_id), "role": "supports"}],
            "assumptions": {"synthetic": True},
            "alternatives": {},
            "short_explanation": "Synthetic fixture only; not training advice.",
        }
        draft_result = await session.call_tool(
            "create_plan_draft",
            arguments={
                **common,
                "message_id": "synthetic-exact-plan-draft",
                "idempotency_key": f"synthetic-exact-plan-{uuid4().hex}",
                "plan": plan,
                "recommendations": [recommendation],
            },
        )
        assert draft_result.isError is False
        assert draft_result.structuredContent is not None
        draft = DraftResult.model_validate(draft_result.structuredContent["data"])
        preview_result = await session.call_tool(
            "render_preview",
            arguments={
                **common,
                "message_id": "synthetic-exact-plan-preview",
                "revision_id": str(draft.revision_id),
                "detail_level": "full",
                "idempotency_key": f"synthetic-exact-preview-{uuid4().hex}",
            },
        )
        assert preview_result.isError is False
        assert preview_result.structuredContent is not None

    preview = PreviewResult.model_validate(preview_result.structuredContent["data"])
    assert preview.complete is True
    assert preview.shown_message_id is None
    canonical_payload = cast(dict[str, Any], preview.canonical_payload)
    preview_plan = canonical_payload["plan"]
    assert preview_plan["blocks"][0]["exercises"][0]["exercise_id"] == str(exercise_ref.object_id)
    preview_recommendation = canonical_payload["recommendations"][0]
    assert preview_recommendation["fact_links"][0]["ref"] == profile.goals[0].ref.model_dump(
        mode="json"
    )
    assert [item["ref"] for item in preview_recommendation["fact_links"][1:]] == history_refs
    assert [item["snapshot_digest"] for item in preview_recommendation["fact_links"][1:]] == [
        digest_payload(item.workout) for item in history_items
    ]
    assert preview_recommendation["conclusion_links"][0]["conclusion_version_id"] == str(
        conclusion_id
    )
    assert preview_result.structuredContent["meta"]["correlation_id"] == correlation_id
