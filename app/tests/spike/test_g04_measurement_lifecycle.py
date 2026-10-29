from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession
from trainer.transport.http.contracts import RecordCandidatesResponse, WorkoutHistoryResponse

DRAFT_CASES = [
    pytest.param(
        "revise_program_draft",
        {
            "draft_id": "20000000-0000-4000-8000-000000000100",
            "base_revision_id": "20000000-0000-4000-8000-000000000101",
            "changes": [
                {
                    "op": "replace",
                    "path": "/goal_revision_id",
                    "value": "20000000-0000-4000-8000-000000000102",
                }
            ],
        },
        id="cmd19-revise-program-draft",
    ),
    pytest.param(
        "create_measurement_session_draft",
        {
            "measurement": {
                "intent": "create",
                "session": {
                    "measured_on": "2026-10-10",
                    "measured_time": None,
                    "conditions": None,
                    "comment": None,
                    "values": [
                        {
                            "measurement_kind": "body_mass",
                            "original_value": "123.4",
                            "original_unit": "kilogram",
                            "side": None,
                            "method": None,
                            "comment": "synthetic fixture only",
                        }
                    ],
                },
            }
        },
        id="cmd11-measurement",
    ),
    pytest.param(
        "create_measurement_session_draft",
        {
            "measurement": {
                "intent": "correct",
                "measurement_root_id": "20000000-0000-4000-8000-000000000090",
                "expected_current_session_id": "20000000-0000-4000-8000-000000000091",
                "reason": "Synthetic correction of the previous circumference method",
                "session": {
                    "measured_on": "2026-10-10",
                    "measured_time": None,
                    "conditions": "synthetic correction fixture",
                    "comment": None,
                    "values": [
                        {
                            "measurement_kind": "waist_circumference",
                            "original_value": "84.2",
                            "original_unit": "centimeter",
                            "side": None,
                            "method": {
                                "mode": "new",
                                "description": "Synthetic tape position at navel",
                            },
                            "comment": None,
                        }
                    ],
                },
            }
        },
        id="cmd11-measurement-correction",
    ),
    pytest.param(
        "create_workout_correction_draft",
        {
            "correction": {
                "target": {
                    "object_type": "workout_session",
                    "object_id": "20000000-0000-4000-8000-000000000001",
                    "revision_id": "20000000-0000-4000-8000-000000000002",
                },
                "patch": [
                    {
                        "op": "replace",
                        "path": "/report",
                        "value": {
                            "plan_id": None,
                            "plan_revision_id": None,
                            "started_at": None,
                            "ended_at": None,
                            "local_start_date": "2026-10-01",
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
                    }
                ],
                "reason": "Synthetic fixture exact-revision correction",
            }
        },
        id="cmd07-complete-report-correction",
    ),
    pytest.param(
        "create_record_decision_draft",
        {
            "decision": {
                "candidate_id": "20000000-0000-4000-8000-000000000003",
                "decision": "reject",
                "technique_self_confirmation": False,
                "reason": "Synthetic candidate rejection",
            }
        },
        id="cmd08-record-candidate-rejection",
    ),
    pytest.param(
        "create_record_decision_draft",
        {
            "decision": {
                "candidate_id": "20000000-0000-4000-8000-000000000012",
                "decision": "accept",
                "technique_self_confirmation": True,
                "reason": "Synthetic explicit technique self-attestation",
            }
        },
        id="cmd08-record-candidate-acceptance",
    ),
    pytest.param(
        "create_historical_record_draft",
        {
            "historical_record": {
                "intent": "create",
                "historical": {
                    "exercise_id": "20000000-0000-4000-8000-000000000004",
                    "exercise_revision_id": "20000000-0000-4000-8000-000000000005",
                    "metric_kind": "record_external_load",
                    "value": "100",
                    "unit_code": "kilogram",
                    "achieved_on": "2025-01-01",
                    "condition": {
                        "side": "not_applicable",
                        "loads": [
                            {
                                "load_kind": "external",
                                "direction": None,
                                "equipment_type": "barbell",
                                "equipment_id": None,
                                "item_count": "1",
                                "mass_kg": "100",
                                "mass_meaning": "total_external_mass",
                            }
                        ],
                        "fixed_reps": 1,
                        "fixed_distance_m": None,
                        "unknown_fields": [],
                    },
                    "body_mass_kg": None,
                    "historical_import": True,
                    "pre_system_assertion": True,
                    "technique_self_confirmation": True,
                    "reason": "Synthetic historical record",
                    "comment": None,
                },
            }
        },
        id="cmd24-historical-record",
    ),
    pytest.param(
        "create_historical_record_draft",
        {
            "historical_record": {
                "intent": "correct",
                "target_record_id": "20000000-0000-4000-8000-000000000011",
                "expected_current": True,
                "historical": {
                    "exercise_id": "20000000-0000-4000-8000-000000000004",
                    "exercise_revision_id": "20000000-0000-4000-8000-000000000005",
                    "metric_kind": "record_external_load",
                    "value": "100",
                    "unit_code": "kilogram",
                    "achieved_on": "2025-01-01",
                    "condition": {
                        "side": "not_applicable",
                        "loads": [
                            {
                                "load_kind": "external",
                                "direction": None,
                                "equipment_type": "barbell",
                                "equipment_id": None,
                                "item_count": "1",
                                "mass_kg": "100",
                                "mass_meaning": "total_external_mass",
                            }
                        ],
                        "fixed_reps": 1,
                        "fixed_distance_m": None,
                        "unknown_fields": [],
                    },
                    "body_mass_kg": None,
                    "historical_import": True,
                    "pre_system_assertion": True,
                    "technique_self_confirmation": True,
                    "reason": "Synthetic historical correction",
                    "comment": None,
                },
            }
        },
        id="cmd24-historical-record-correction",
    ),
    pytest.param(
        "calculate_metric",
        {
            "calculation": {
                "method_id": "e1rm_brzycki",
                "method_version": "1",
                "mode": "persist",
                "source": {
                    "kind": "actual_set",
                    "session_id": "20000000-0000-4000-8000-000000000006",
                    "session_revision_id": "20000000-0000-4000-8000-000000000007",
                    "exercise_id": "20000000-0000-4000-8000-000000000008",
                    "exercise_revision_id": "20000000-0000-4000-8000-000000000009",
                    "actual_set_id": "20000000-0000-4000-8000-000000000010",
                },
                "execution_attestation": {
                    "source_actual_set_id": "20000000-0000-4000-8000-000000000010",
                    "grip_condition": "synthetic standard grip",
                    "bench_pause_condition": "intentional_chest_pause",
                    "standard_confirmed": True,
                    "spotter_released_before_rep": True,
                    "no_assistance_during_rep": True,
                },
                "prior_derived_value_id": None,
            }
        },
        id="cmd17-persistent-e1rm-draft",
    ),
]


@pytest.mark.spike
@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_name", "draft_specific_args"), DRAFT_CASES)
async def test_g04_draft_uses_mcp_and_exact_preview_lifecycle(
    g04_mcp_url: str,
    tool_name: str,
    draft_specific_args: dict[str, object],
) -> None:
    conversation_id = "synthetic-g04-measurement"
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    run_id = uuid4().hex
    async with (
        streamable_http_client(g04_mcp_url) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        draft = await session.call_tool(
            tool_name,
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-draft",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-draft-{run_id}",
                **draft_specific_args,
            },
        )
        assert draft.isError is False
        assert draft.structuredContent is not None
        draft_data = draft.structuredContent["data"]
        assert draft_data["synthetic"] is True
        assert draft_data["effect"] == "none"
        revision_id = draft_data["revision_id"]
        if tool_name == "create_measurement_session_draft":
            accepted_measurement = draft_data["accepted_measurement"]
            assert accepted_measurement["intent"] == draft_specific_args["measurement"]["intent"]
            assert (
                accepted_measurement["session"]["values"][0]["measurement_kind"]
                == (draft_specific_args["measurement"]["session"]["values"][0]["measurement_kind"])
            )
            if accepted_measurement["intent"] == "correct":
                assert (
                    accepted_measurement["measurement_root_id"]
                    == (draft_specific_args["measurement"]["measurement_root_id"])
                )
                assert (
                    accepted_measurement["expected_current_session_id"]
                    == (draft_specific_args["measurement"]["expected_current_session_id"])
                )
        if tool_name == "create_historical_record_draft":
            accepted_historical = draft_data["accepted_historical_record"]
            expected_historical = draft_specific_args["historical_record"]
            assert accepted_historical["intent"] == expected_historical["intent"]
            assert accepted_historical["historical"] == expected_historical["historical"]
            if accepted_historical["intent"] == "correct":
                assert (
                    accepted_historical["target_record_id"]
                    == (expected_historical["target_record_id"])
                )
                assert accepted_historical["expected_current"] is True
        if tool_name == "create_record_decision_draft":
            accepted_decision = draft_data["accepted_decision"]
            expected_decision = draft_specific_args["decision"]
            assert accepted_decision["candidate_id"] == expected_decision["candidate_id"]
            assert accepted_decision["decision"] == expected_decision["decision"]
            assert (
                accepted_decision["technique_self_confirmation"]
                is expected_decision["technique_self_confirmation"]
            )

        preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": revision_id,
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-preview-{run_id}",
            },
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        preview_data = preview.structuredContent["data"]
        assert preview_data["revision_id"] == revision_id
        assert preview_data["complete"] is True
        canonical_text = preview_data["canonical_text"]
        assert canonical_text == "SPIKE PREVIEW\n- technical fixture\n- no domain effect"

        shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "canonical_text": canonical_text,
                "conversation_id": conversation_id,
                "shown_message_id": "synthetic-shown",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-shown-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert shown.isError is False

        confirmed = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "conversation_id": conversation_id,
                "confirmation_message_id": "synthetic-confirmation",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-confirm-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Подтверждаю только синтетический fixture для теста",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
                "referenced_preview_id": None,
                "reshown_message_id": None,
            },
        )
        assert confirmed.isError is False
        assert confirmed.structuredContent is not None
        result = confirmed.structuredContent["data"]
        assert result["status"] == "committed"
        assert result["domain_changes"] == []
        assert result["created_revisions"] == []


@pytest.mark.spike
@pytest.mark.asyncio
async def test_g04_history_exact_revision_flows_to_correction_confirmation(
    g04_mcp_url: str,
) -> None:
    conversation_id = "synthetic-g04-history-correction"
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    run_id = uuid4().hex
    history_query = {
        "mode": "all_confirmed",
        "date_from": None,
        "date_to": None,
        "exercise_id": None,
        "lifecycle": [],
        "limit": 100,
        "cursor": None,
    }
    correction_template = next(
        case.values[1]["correction"]
        for case in DRAFT_CASES
        if case.id == "cmd07-complete-report-correction"
    )
    async with (
        streamable_http_client(g04_mcp_url) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        history = await session.call_tool(
            "get_workout_history",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-history-read",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "query": history_query,
            },
        )
        assert history.isError is False
        assert history.structuredContent is not None
        history_data = history.structuredContent["data"]
        assert history_data["synthetic"] is True
        assert history_data["filters"] == history_query
        snapshot = WorkoutHistoryResponse.model_validate(history_data["response"])
        assert snapshot.total_count == 1
        assert snapshot.next_cursor is None
        assert len(snapshot.items) == 1
        exact_target = snapshot.items[0].session_ref.model_dump(mode="json")

        correction = deepcopy(correction_template)
        correction["target"] = exact_target
        draft = await session.call_tool(
            "create_workout_correction_draft",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-correction-draft",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-history-correction-{run_id}",
                "correction": correction,
            },
        )
        assert draft.isError is False
        assert draft.structuredContent is not None
        draft_data = draft.structuredContent["data"]
        assert draft_data["synthetic"] is True
        assert draft_data["effect"] == "none"
        assert draft_data["accepted_target"] == exact_target
        revision_id = draft_data["revision_id"]

        preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-correction-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": revision_id,
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-history-preview-{run_id}",
            },
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        preview_data = preview.structuredContent["data"]
        assert preview_data["revision_id"] == revision_id
        assert preview_data["complete"] is True
        canonical_text = preview_data["canonical_text"]
        assert canonical_text == "SPIKE PREVIEW\n- technical fixture\n- no domain effect"

        shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "canonical_text": canonical_text,
                "conversation_id": conversation_id,
                "shown_message_id": "synthetic-correction-shown",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-history-shown-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert shown.isError is False

        confirmed = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "conversation_id": conversation_id,
                "confirmation_message_id": "synthetic-correction-confirmation",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-history-confirm-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Подтверждаю только синтетический fixture для теста",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
                "referenced_preview_id": None,
                "reshown_message_id": None,
            },
        )
        assert confirmed.isError is False
        assert confirmed.structuredContent is not None
        result = confirmed.structuredContent["data"]
        assert result["status"] == "committed"
        assert result["domain_changes"] == []
        assert result["created_revisions"] == []


@pytest.mark.spike
@pytest.mark.asyncio
async def test_g04_history_exact_set_flows_to_e1rm_confirmation(
    g04_mcp_url: str,
) -> None:
    conversation_id = "synthetic-g04-history-e1rm"
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    run_id = uuid4().hex
    history_query = {
        "mode": "all_confirmed",
        "date_from": None,
        "date_to": None,
        "exercise_id": None,
        "lifecycle": [],
        "limit": 100,
        "cursor": None,
    }
    calculation_template = next(
        case.values[1]["calculation"]
        for case in DRAFT_CASES
        if case.id == "cmd17-persistent-e1rm-draft"
    )
    async with (
        streamable_http_client(g04_mcp_url) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        history = await session.call_tool(
            "get_workout_history",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-history-read",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "query": history_query,
            },
        )
        assert history.isError is False
        assert history.structuredContent is not None
        history_data = history.structuredContent["data"]
        assert history_data["synthetic"] is True
        snapshot = WorkoutHistoryResponse.model_validate(history_data["response"])
        assert snapshot.total_count == 1
        assert snapshot.next_cursor is None
        assert len(snapshot.items) == 1
        history_item = snapshot.items[0]
        workout = history_item.workout
        exercise = workout["blocks"][0]["exercises"][0]
        actual_set = exercise["sets"][0]
        exercise_ref = next(
            provenance["ref"]
            for provenance in history_item.provenance
            if provenance.get("kind") == "exercise_revision"
        )
        assert exercise_ref["object_id"] == exercise["exercise_id"]
        session_ref = history_item.session_ref
        expected_source = {
            "kind": "actual_set",
            "session_id": str(session_ref.object_id),
            "session_revision_id": str(session_ref.revision_id),
            "exercise_id": exercise_ref["object_id"],
            "exercise_revision_id": exercise_ref["revision_id"],
            "actual_set_id": actual_set["actual_set_id"],
        }
        expected_attestation = {
            **calculation_template["execution_attestation"],
            "source_actual_set_id": actual_set["actual_set_id"],
        }
        calculation = deepcopy(calculation_template)
        calculation["source"] = expected_source
        calculation["execution_attestation"] = expected_attestation
        draft = await session.call_tool(
            "calculate_metric",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-e1rm-draft",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-history-e1rm-{run_id}",
                "calculation": calculation,
            },
        )
        assert draft.isError is False
        assert draft.structuredContent is not None
        draft_data = draft.structuredContent["data"]
        assert draft_data["synthetic"] is True
        assert draft_data["effect"] == "none"
        assert draft_data["accepted_calculation"] == {
            "source": expected_source,
            "execution_attestation": expected_attestation,
        }
        revision_id = draft_data["revision_id"]

        preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-e1rm-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": revision_id,
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-e1rm-preview-{run_id}",
            },
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        preview_data = preview.structuredContent["data"]
        assert preview_data["revision_id"] == revision_id
        assert preview_data["complete"] is True
        canonical_text = preview_data["canonical_text"]
        assert canonical_text == "SPIKE PREVIEW\n- technical fixture\n- no domain effect"

        shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "canonical_text": canonical_text,
                "conversation_id": conversation_id,
                "shown_message_id": "synthetic-e1rm-shown",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-e1rm-shown-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert shown.isError is False

        confirmed = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "conversation_id": conversation_id,
                "confirmation_message_id": "synthetic-e1rm-confirmation",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-e1rm-confirm-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Подтверждаю только синтетический fixture для теста",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
                "referenced_preview_id": None,
                "reshown_message_id": None,
            },
        )
        assert confirmed.isError is False
        assert confirmed.structuredContent is not None
        result = confirmed.structuredContent["data"]
        assert result["status"] == "committed"
        assert result["domain_changes"] == []
        assert result["created_revisions"] == []


@pytest.mark.spike
@pytest.mark.asyncio
async def test_g04_pending_candidate_acceptance_uses_exact_id_and_self_attestation(
    g04_mcp_url: str,
) -> None:
    conversation_id = "synthetic-g04-candidate-acceptance"
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    run_id = uuid4().hex
    candidate_query = {
        "exercise_id": None,
        "metric_kind": None,
        "status": "pending",
        "limit": 10,
        "cursor": None,
    }
    async with (
        streamable_http_client(g04_mcp_url) as (
            read_stream,
            write_stream,
            _,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        candidates = await session.call_tool(
            "get_record_candidates",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-pending-candidate-read",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "query": candidate_query,
            },
        )
        assert candidates.isError is False
        assert candidates.structuredContent is not None
        candidate_data = candidates.structuredContent["data"]
        assert candidate_data["synthetic"] is True
        assert candidate_data["filters"] == candidate_query
        candidate_response = RecordCandidatesResponse.model_validate(candidate_data["response"])
        assert len(candidate_response.items) == 1
        candidate = candidate_response.items[0]
        assert candidate.status == "pending"
        assert candidate.current_record_ref is None
        expected_decision = {
            "candidate_id": str(candidate.candidate_id),
            "decision": "accept",
            "technique_self_confirmation": True,
            "reason": "Synthetic explicit self-attestation for transport test",
        }
        draft = await session.call_tool(
            "create_record_decision_draft",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-candidate-decision-draft",
                "timezone": "Asia/Novokuznetsk",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-candidate-decision-{run_id}",
                "decision": expected_decision,
            },
        )
        assert draft.isError is False
        assert draft.structuredContent is not None
        draft_data = draft.structuredContent["data"]
        assert draft_data["synthetic"] is True
        assert draft_data["effect"] == "none"
        accepted_decision = draft_data["accepted_decision"]
        assert accepted_decision["candidate_id"] == expected_decision["candidate_id"]
        assert accepted_decision["decision"] == "accept"
        assert accepted_decision["technique_self_confirmation"] is True
        assert accepted_decision["reason"] == expected_decision["reason"]
        assert accepted_decision["request_digest"]
        revision_id = draft_data["revision_id"]

        preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-candidate-decision-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": revision_id,
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-candidate-preview-{run_id}",
            },
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        preview_data = preview.structuredContent["data"]
        assert preview_data["revision_id"] == revision_id
        assert preview_data["complete"] is True
        canonical_text = preview_data["canonical_text"]
        assert canonical_text == "SPIKE PREVIEW\n- technical fixture\n- no domain effect"

        shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "canonical_text": canonical_text,
                "conversation_id": conversation_id,
                "shown_message_id": "synthetic-candidate-shown",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-candidate-shown-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert shown.isError is False

        confirmed = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revision_id,
                "preview_digest": preview_data["digest"],
                "conversation_id": conversation_id,
                "confirmation_message_id": "synthetic-candidate-confirmation",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"g04-candidate-confirm-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Подтверждаю только синтетический fixture для теста",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
                "referenced_preview_id": None,
                "reshown_message_id": None,
            },
        )
        assert confirmed.isError is False
        assert confirmed.structuredContent is not None
        result = confirmed.structuredContent["data"]
        assert result["status"] == "committed"
        assert result["domain_changes"] == []
        assert result["created_revisions"] == []
