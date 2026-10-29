from __future__ import annotations

from uuid import uuid4

import pytest
from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession
from trainer.transport.http.contracts import WorkoutReportInput


def _report(comment: str, *, effort: str | None) -> dict[str, object]:
    planned_exercise_id = str(uuid4())
    skipped_replacement_exercise_id = str(uuid4())
    unknown_exercise_id = str(uuid4())
    completed_set_id, skipped_set_id, unknown_set_id = (str(uuid4()) for _ in range(3))
    replacement_exercise_id = str(uuid4())
    return {
        "plan_id": str(uuid4()),
        "plan_revision_id": str(uuid4()),
        "started_at": None,
        "ended_at": None,
        "local_start_date": "2026-10-11",
        "duration_seconds": None,
        "session_type": "training",
        "name": None,
        "lifecycle": "completed",
        "effort": effort,
        "comment": comment,
        "workout_metrics": [],
        "blocks": [
            {
                "position": 1,
                "planned_block_id": str(uuid4()),
                "block_type_code": "straight",
                "exercises": [
                    {
                        "position": 1,
                        "exercise_id": planned_exercise_id,
                        "planned_exercise_id": planned_exercise_id,
                        "result": "partial",
                        "sets": [
                            {
                                "position": 1,
                                "planned_set_id": completed_set_id,
                                "logical_set_no": 1,
                                "set_type_code": "working",
                                "full_reps": 8,
                                "status": "performed",
                                "metrics": [],
                                "loads": [],
                            },
                            {
                                "position": 2,
                                "planned_set_id": skipped_set_id,
                                "logical_set_no": 2,
                                "set_type_code": "working",
                                "status": "skipped",
                                "metrics": [],
                                "loads": [],
                            },
                        ],
                    },
                    {
                        "position": 2,
                        "exercise_id": skipped_replacement_exercise_id,
                        "planned_exercise_id": skipped_replacement_exercise_id,
                        "result": "skipped",
                        "reason": "replacement",
                        "sets": [],
                    },
                    {
                        "position": 3,
                        "exercise_id": replacement_exercise_id,
                        "planned_exercise_id": None,
                        "result": "added",
                        "replacement_source": {
                            "kind": "skipped_execution",
                            "block_position": 1,
                            "exercise_position": 2,
                        },
                        "reason": "replacement",
                        "sets": [
                            {
                                "position": 1,
                                "planned_set_id": None,
                                "logical_set_no": 1,
                                "set_type_code": "working",
                                "full_reps": 10,
                                "status": "performed",
                                "metrics": [],
                                "loads": [],
                            }
                        ],
                    },
                ],
            }
        ],
        "confirmed_unknown_planned_exercise_ids": [unknown_exercise_id],
        "confirmed_unknown_planned_set_ids": [unknown_set_id],
        "confirmed_unknown_planned_load_component_ids": [],
        "confirmed_unknown_metrics": [],
    }


@pytest.mark.spike
@pytest.mark.asyncio
async def test_f_g03_report_revision_requires_new_exact_preview_confirmation(
    f_g03_workout_mcp_url: str,
) -> None:
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    run_id = uuid4().hex
    conversation_id = "synthetic-f-g03-workout"
    original_report = WorkoutReportInput.model_validate(
        _report("Synthetic original report", effort=None)
    ).model_dump(mode="json")
    revised_report = WorkoutReportInput.model_validate(
        _report("Synthetic complete replacement", effort="7")
    ).model_dump(mode="json")

    async with (
        streamable_http_client(f_g03_workout_mcp_url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        draft = await session.call_tool(
            "create_workout_report_draft",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-report-create",
                "timezone": "Asia/Novokuznetsk",
                "report": original_report,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-create-{run_id}",
            },
        )
        assert draft.isError is False
        assert draft.structuredContent is not None
        draft_data = draft.structuredContent["data"]
        assert draft_data["synthetic"] is True
        assert draft_data["effect"] == "none"
        assert draft_data["submitted_report"] == original_report
        assert draft_data["submitted_report"]["blocks"] == original_report["blocks"]
        assert (
            draft_data["submitted_report"]["confirmed_unknown_planned_set_ids"]
            == (original_report["confirmed_unknown_planned_set_ids"])
        )

        old_preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-original-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": draft_data["revision_id"],
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-original-preview-{run_id}",
            },
        )
        assert old_preview.isError is False
        assert old_preview.structuredContent is not None
        old_preview_data = old_preview.structuredContent["data"]
        old_shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": old_preview_data["preview_id"],
                "revision_id": draft_data["revision_id"],
                "preview_digest": old_preview_data["digest"],
                "canonical_text": old_preview_data["canonical_text"],
                "conversation_id": conversation_id,
                "shown_message_id": "synthetic-original-shown",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-original-shown-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert old_shown.isError is False

        revised = await session.call_tool(
            "revise_workout_report_draft",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-report-revision",
                "timezone": "Asia/Novokuznetsk",
                "draft_id": draft_data["draft_id"],
                "base_revision_id": draft_data["revision_id"],
                "change": {"op": "replace", "path": "/report", "value": revised_report},
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-revise-{run_id}",
            },
        )
        assert revised.isError is False
        assert revised.structuredContent is not None
        revised_data = revised.structuredContent["data"]
        assert revised_data["draft_id"] == draft_data["draft_id"]
        assert revised_data["session_id"] == draft_data["session_id"]
        assert revised_data["revision_id"] != draft_data["revision_id"]
        assert revised_data["revision_no"] == 2
        assert revised_data["replaced_report"] == revised_report
        assert revised_data["effect"] == "none"
        assert revised_data["replaced_report"]["blocks"] == revised_report["blocks"]
        assert (
            revised_data["replaced_report"]["confirmed_unknown_planned_exercise_ids"]
            == (revised_report["confirmed_unknown_planned_exercise_ids"])
        )

        stale_confirmation = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": old_preview_data["preview_id"],
                "revision_id": draft_data["revision_id"],
                "preview_digest": old_preview_data["digest"],
                "conversation_id": conversation_id,
                "confirmation_message_id": "synthetic-stale-confirm",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-stale-confirm-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Confirm stale synthetic preview only",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
            },
        )
        assert stale_confirmation.isError is False
        assert stale_confirmation.structuredContent is not None
        assert stale_confirmation.structuredContent["error"]["code"] == "stale_preview"

        stale_preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-stale-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": draft_data["revision_id"],
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-stale-preview-{run_id}",
            },
        )
        assert stale_preview.isError is False
        assert stale_preview.structuredContent is not None
        assert stale_preview.structuredContent["error"]["code"] == "stale_revision"

        preview = await session.call_tool(
            "render_preview",
            arguments={
                "conversation_id": conversation_id,
                "message_id": "synthetic-current-preview",
                "timezone": "Asia/Novokuznetsk",
                "revision_id": revised_data["revision_id"],
                "detail_level": "full",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-preview-{run_id}",
            },
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        preview_data = preview.structuredContent["data"]
        assert preview_data["revision_id"] == revised_data["revision_id"]
        assert preview_data["complete"] is True
        assert preview_data["canonical_payload"]["report"] == revised_report
        canonical_report = preview_data["canonical_payload"]["report"]
        exercises = canonical_report["blocks"][0]["exercises"]
        assert exercises[0]["sets"][1]["status"] == "skipped"
        assert exercises[0]["sets"][1]["full_reps"] is None
        assert (
            canonical_report["confirmed_unknown_planned_set_ids"]
            == (revised_report["confirmed_unknown_planned_set_ids"])
        )
        assert exercises[1]["result"] == "skipped"
        assert exercises[1]["reason"] == "replacement"
        assert exercises[2]["result"] == "added"
        assert exercises[2]["replacement_source"] == {
            "kind": "skipped_execution",
            "block_position": 1,
            "exercise_position": 2,
        }
        canonical_text = preview_data["canonical_text"]

        shown = await session.call_tool(
            "acknowledge_preview_shown",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revised_data["revision_id"],
                "preview_digest": preview_data["digest"],
                "canonical_text": canonical_text,
                "conversation_id": conversation_id,
                "shown_message_id": "synthetic-report-shown",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-shown-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "cross_dialog": False,
            },
        )
        assert shown.isError is False

        confirmed = await session.call_tool(
            "confirm_preview",
            arguments={
                "preview_id": preview_data["preview_id"],
                "revision_id": revised_data["revision_id"],
                "preview_digest": preview_data["digest"],
                "conversation_id": conversation_id,
                "confirmation_message_id": "synthetic-report-confirm",
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "idempotency_key": f"f-g03-confirm-{run_id}",
                "timezone": "Asia/Novokuznetsk",
                "explicit_intent": "Confirm this synthetic report preview only",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
            },
        )
        assert confirmed.isError is False
        assert confirmed.structuredContent is not None
        confirmation = confirmed.structuredContent["data"]
        assert confirmation["status"] == "committed"
        assert confirmation["domain_changes"] == []
        assert confirmation["created_revisions"] == []
