from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from fastapi import Header, HTTPException, Request
from pydantic import TypeAdapter

from trainer.spike.app import FIXTURE_REVISION_ID, create_spike_app, digest, meta
from trainer.transport.http.contracts import (
    HistoricalRecordDraftRequest,
    ProgramDraftRevisionRequest,
    RecordDecisionDraftRequest,
    WorkoutCorrectionRequest,
)
from trainer.transport.http.measurement_contracts import MeasurementDraftRequest
from trainer.transport.http.progress_contracts import CalculationRequest

TOKEN = "spike-local-token"
app = create_spike_app(TOKEN)


async def create_draft_response(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="validation_error")
    request_digest = body.pop("request_digest", None)
    if request_digest != digest(body):
        raise HTTPException(status_code=400, detail="digest_mismatch")
    if not idempotency_key or not x_correlation_id or not x_causation_id:
        raise HTTPException(status_code=400, detail="validation_error")
    data = {
        "draft_id": str(uuid4()),
        "revision_id": str(FIXTURE_REVISION_ID),
        "state": "open",
        "synthetic": True,
        "effect": "none",
    }
    return {"data": data, "meta": meta(x_correlation_id, None)}


@app.post("/api/v1/commands/drafts/{draft_id}/revisions")
async def revise_program_draft(
    draft_id: str,
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    body = await request.json()
    ProgramDraftRevisionRequest.model_validate(body)
    return await create_draft_response(
        request, authorization, x_correlation_id, x_causation_id, idempotency_key
    )


@app.post("/api/v1/commands/measurement-session-drafts")
async def create_measurement_session_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    contract = TypeAdapter(MeasurementDraftRequest).validate_python(await request.json())
    response = await create_draft_response(
        request,
        authorization,
        x_correlation_id,
        x_causation_id,
        idempotency_key,
    )
    response["data"]["accepted_measurement"] = contract.model_dump(mode="json")
    return response


@app.post("/api/v1/commands/correction-drafts")
async def create_workout_correction_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    correction = WorkoutCorrectionRequest.model_validate(await request.json())
    response = await create_draft_response(
        request,
        authorization,
        x_correlation_id,
        x_causation_id,
        idempotency_key,
    )
    response["data"]["accepted_target"] = correction.target.model_dump(mode="json")
    return response


@app.post("/api/v1/commands/record-decision-drafts")
async def create_record_decision_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    decision = RecordDecisionDraftRequest.model_validate(await request.json())
    response = await create_draft_response(
        request,
        authorization,
        x_correlation_id,
        x_causation_id,
        idempotency_key,
    )
    response["data"]["accepted_decision"] = decision.model_dump(mode="json")
    return response


@app.post("/api/v1/commands/historical-record-drafts")
async def create_historical_record_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    contract = TypeAdapter(HistoricalRecordDraftRequest).validate_python(await request.json())
    response = await create_draft_response(
        request,
        authorization,
        x_correlation_id,
        x_causation_id,
        idempotency_key,
    )
    response["data"]["accepted_historical_record"] = contract.model_dump(mode="json")
    return response


@app.post("/api/v1/commands/calculations")
async def create_calculation_draft(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
    x_causation_id: str = Header(),
    idempotency_key: str = Header(),
) -> dict[str, Any]:
    calculation = CalculationRequest.model_validate(await request.json())
    response = await create_draft_response(
        request,
        authorization,
        x_correlation_id,
        x_causation_id,
        idempotency_key,
    )
    response["data"]["accepted_calculation"] = {
        "source": calculation.source.model_dump(mode="json"),
        "execution_attestation": (
            calculation.execution_attestation.model_dump(mode="json")
            if calculation.execution_attestation is not None
            else None
        ),
    }
    return response


def empty_query_response(path: str, filters: dict[str, Any]) -> dict[str, Any]:
    if path.endswith("/workouts/history"):
        payload: dict[str, Any] = {
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
                        "session_id": "20000000-0000-4000-8000-000000000030",
                        "profile_id": "20000000-0000-4000-8000-000000000035",
                        "plan_id": "20000000-0000-4000-8000-000000000033",
                        "session_revision_id": "20000000-0000-4000-8000-000000000031",
                        "plan_revision_id": "20000000-0000-4000-8000-000000000034",
                        "started_at": "2026-10-10T08:30:00+07:00",
                        "local_start_date": "2026-10-10",
                        "ended_at": "2026-10-10T09:30:00+07:00",
                        "duration_seconds": 3600,
                        "session_type": "training",
                        "name": "Synthetic strength session",
                        "lifecycle": "completed",
                        "effort": "7.00",
                        "comment": "synthetic confirmed report",
                        "report_gaps": [],
                        "blocks": [
                            {
                                "actual_block_id": "20000000-0000-4000-8000-000000000036",
                                "session_revision_id": "20000000-0000-4000-8000-000000000031",
                                "planned_block_id": "20000000-0000-4000-8000-000000000037",
                                "position": 1,
                                "block_type": "superset",
                                "declared_rounds": 1,
                                "rest_seconds": 90,
                                "comment": "synthetic paired block",
                                "exercises": [
                                    {
                                        "execution_id": "20000000-0000-4000-8000-000000000038",
                                        "actual_block_id": "20000000-0000-4000-8000-000000000036",
                                        "exercise_id": "20000000-0000-4000-8000-000000000039",
                                        "planned_exercise_id": (
                                            "20000000-0000-4000-8000-000000000040"
                                        ),
                                        "position": 1,
                                        "result": "performed",
                                        "reason": None,
                                        "sets": [
                                            {
                                                "actual_set_id": (
                                                    "20000000-0000-4000-8000-000000000041"
                                                ),
                                                "execution_id": (
                                                    "20000000-0000-4000-8000-000000000038"
                                                ),
                                                "planned_set_id": (
                                                    "20000000-0000-4000-8000-000000000042"
                                                ),
                                                "position": 1,
                                                "round_no": 1,
                                                "logical_set_no": 1,
                                                "side": "both",
                                                "set_type": "working",
                                                "full_reps": 8,
                                                "partial_reps": 0,
                                                "failed_attempts": 0,
                                                "reported_rpe": "7.00",
                                                "reported_rir": None,
                                                "failure_reached": False,
                                                "rom_state": None,
                                                "technique_state": None,
                                                "tempo": None,
                                                "pause_seconds": None,
                                                "rest_after_seconds": 90,
                                                "status": "performed",
                                                "comment": None,
                                                "metrics": [
                                                    {
                                                        "metric_kind": "repetitions",
                                                        "value": "8.000000",
                                                        "unit_code": "count",
                                                        "custom_definition": None,
                                                        "source_kind": "user",
                                                    }
                                                ],
                                                "loads": [
                                                    {
                                                        "actual_load_component_id": (
                                                            "20000000-0000-4000-8000-000000000043"
                                                        ),
                                                        "actual_set_id": (
                                                            "20000000-0000-4000-8000-000000000041"
                                                        ),
                                                        "planned_load_component_id": (
                                                            "20000000-0000-4000-8000-000000000044"
                                                        ),
                                                        "position": 1,
                                                        "load_kind": "external",
                                                        "equipment_id": (
                                                            "20000000-0000-4000-8000-000000000045"
                                                        ),
                                                        "equipment_snapshot": {
                                                            "name": "Synthetic barbell"
                                                        },
                                                        "item_count": None,
                                                        "mass": "20.000000",
                                                        "unit_code": "kilogram",
                                                        "mass_meaning": "total_loaded_mass",
                                                        "direction": "added",
                                                        "raw_ambiguous_text": None,
                                                        "assembly_snapshot": None,
                                                    }
                                                ],
                                            }
                                        ],
                                        "replacement_source": None,
                                    }
                                ],
                            }
                        ],
                        "workout_metrics": [
                            {
                                "metric_kind": "duration",
                                "value": "3600.000000",
                                "unit_code": "second",
                                "source_kind": "user",
                                "derived_value_id": None,
                            }
                        ],
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
        }
    elif path.endswith("/progress"):
        exercise_id = filters.get("exercise_id") or "20000000-0000-4000-8000-000000000051"

        def volume_point(
            *,
            point_key: str,
            session_id: str,
            revision_id: str,
            actual_set_id: str,
            load_id: str,
            achieved_on: str,
            reps: str,
            value: str,
        ) -> dict[str, Any]:
            session_ref = {
                "object_type": "workout_session",
                "object_id": session_id,
                "revision_id": revision_id,
            }
            actual_set_ref = {
                "object_type": "actual_set",
                "object_id": actual_set_id,
                "revision_id": revision_id,
            }
            return {
                "kind": "derived",
                "point_key": point_key * 64,
                "point": {
                    "derived_value_ref": None,
                    "method_id": "external_working_set_volume",
                    "method_version": "1",
                    "metric_kind": "external_working_set_volume",
                    "value": {"exact_value": value, "unit_code": "kilogram_repetition"},
                    "achieved_on": achieved_on,
                    "exercise_ref": {"object_type": "exercise", "object_id": exercise_id},
                    "exercise_revision_ref": {
                        "object_type": "exercise",
                        "object_id": exercise_id,
                        "revision_id": "20000000-0000-4000-8000-000000000076",
                    },
                    "session_ref": session_ref,
                    "session_revision_ref": {
                        "object_type": "workout_session_revision",
                        "object_id": revision_id,
                        "revision_id": None,
                    },
                    "source_actual_set_ref": None,
                    "condition_id": None,
                    "state": "ephemeral",
                    "completeness": "complete",
                    "included_count": 1,
                    "excluded_count": 0,
                    "inputs": [
                        {
                            "role": "session_revision",
                            "source_ref": session_ref,
                            "normalized_value": None,
                            "unit_code": None,
                            "snapshot": {"local_start_date": achieved_on},
                        },
                        {
                            "role": "exercise_revision",
                            "source_ref": {
                                "object_type": "exercise",
                                "object_id": exercise_id,
                                "revision_id": "20000000-0000-4000-8000-000000000076",
                            },
                            "normalized_value": None,
                            "unit_code": None,
                            "snapshot": {"canonical_name": "Synthetic exercise"},
                        },
                        {
                            "role": "actual_set",
                            "source_ref": actual_set_ref,
                            "normalized_value": reps,
                            "unit_code": "repetition",
                            "snapshot": {"set_type": "working", "status": "performed"},
                        },
                        {
                            "role": "load_component",
                            "source_ref": {
                                "object_type": "actual_load_component",
                                "object_id": load_id,
                                "revision_id": revision_id,
                            },
                            "normalized_value": "20",
                            "unit_code": "kilogram",
                            "snapshot": {"mass_meaning": "total_external_mass"},
                        },
                    ],
                    "exclusions": [],
                    "input_digest": "c" * 64,
                    "result_digest": "d" * 64,
                    "stale_reason": None,
                },
            }

        payload = {
            "items": [
                volume_point(
                    point_key="a",
                    session_id="20000000-0000-4000-8000-000000000070",
                    revision_id="20000000-0000-4000-8000-000000000071",
                    actual_set_id="20000000-0000-4000-8000-000000000072",
                    load_id="20000000-0000-4000-8000-000000000073",
                    achieved_on="2026-10-02",
                    reps="8",
                    value="160",
                ),
                volume_point(
                    point_key="b",
                    session_id="20000000-0000-4000-8000-000000000074",
                    revision_id="20000000-0000-4000-8000-000000000075",
                    actual_set_id="20000000-0000-4000-8000-000000000077",
                    load_id="20000000-0000-4000-8000-000000000078",
                    achieved_on="2026-10-09",
                    reps="9",
                    value="180",
                ),
            ],
            "next_cursor": None,
            "has_more": False,
            "gaps": [],
            "comparison_status": "two_points",
        }
    elif path.endswith("/record-candidates"):
        session_ref = {
            "object_type": "workout_session",
            "object_id": "20000000-0000-4000-8000-000000000080",
            "revision_id": "20000000-0000-4000-8000-000000000081",
        }
        set_ref = {
            "object_type": "actual_set",
            "object_id": "20000000-0000-4000-8000-000000000082",
            "revision_id": "20000000-0000-4000-8000-000000000081",
        }
        exercise_ref = {
            "object_type": "exercise",
            "object_id": "20000000-0000-4000-8000-000000000051",
            "revision_id": None,
        }
        condition = {
            "condition_id": "20000000-0000-4000-8000-000000000083",
            "rule_version": "record_v1",
            "scope": "comparable",
            "metric_kind": "record_full_reps",
            "unit_code": "repetition",
            "side": "not_applicable",
            "loads": [
                {
                    "load_kind": "bodyweight",
                    "direction": None,
                    "equipment_type": None,
                    "equipment_id": None,
                    "item_count": None,
                    "mass_kg": None,
                    "mass_meaning": None,
                }
            ],
            "fixed_reps": None,
            "fixed_distance_m": None,
            "unknown_fields": [],
            "normalized_key": "synthetic-record-condition-v1",
        }
        payload = {
            "items": [
                {
                    "candidate_id": "20000000-0000-4000-8000-000000000084",
                    "source_session_ref": session_ref,
                    "source_set_ref": set_ref,
                    "exercise_ref": exercise_ref,
                    "achieved_on": "2026-10-10",
                    "metric_kind": "record_full_reps",
                    "candidate_value": {
                        "exact_value": "10",
                        "unit_code": "repetition",
                    },
                    "condition": condition,
                    "status": "pending",
                    "current_record_ref": None,
                    "invalidated_reason": None,
                    "decision_confirmation_ref": None,
                    "eligible_now": True,
                }
            ],
            "next_cursor": None,
        }
    elif path.endswith("/records"):
        condition_id = "20000000-0000-4000-8000-000000000083"
        payload = {
            "items": [
                {
                    "record_id": "20000000-0000-4000-8000-000000000085",
                    "exercise_ref": {
                        "object_type": "exercise",
                        "object_id": "20000000-0000-4000-8000-000000000051",
                        "revision_id": None,
                    },
                    "metric_kind": "record_full_reps",
                    "value": {"exact_value": "9", "unit_code": "repetition"},
                    "achieved_on": "2026-10-03",
                    "condition": {
                        "condition_id": condition_id,
                        "rule_version": "record_v1",
                        "scope": "comparable",
                        "metric_kind": "record_full_reps",
                        "unit_code": "repetition",
                        "side": "not_applicable",
                        "loads": [
                            {
                                "load_kind": "bodyweight",
                                "direction": None,
                                "equipment_type": None,
                                "equipment_id": None,
                                "item_count": None,
                                "mass_kg": None,
                                "mass_meaning": None,
                            }
                        ],
                        "fixed_reps": None,
                        "fixed_distance_m": None,
                        "unknown_fields": [],
                        "normalized_key": "synthetic-record-condition-v1",
                    },
                    "is_current": True,
                    "is_historical_import": False,
                    "validity": "valid",
                    "source_session_ref": {
                        "object_type": "workout_session",
                        "object_id": "20000000-0000-4000-8000-000000000100",
                        "revision_id": "20000000-0000-4000-8000-000000000101",
                    },
                    "source_set_ref": {
                        "object_type": "actual_set",
                        "object_id": "20000000-0000-4000-8000-000000000102",
                        "revision_id": "20000000-0000-4000-8000-000000000101",
                    },
                    "source_candidate_ref": {
                        "object_type": "record_candidate",
                        "object_id": "20000000-0000-4000-8000-000000000103",
                        "revision_id": None,
                    },
                    "confirmation_ref": {
                        "object_type": "confirmation",
                        "object_id": "20000000-0000-4000-8000-000000000104",
                        "revision_id": None,
                    },
                    "supersedes_record_ref": None,
                    "body_mass_kg": None,
                    "current_exit_reason": None,
                    "current_exit_change_id": None,
                    "comment": "synthetic accepted record",
                }
            ],
            "next_cursor": None,
        }
    elif path.endswith("/measurements") and filters.get("mode") == "sessions":
        payload = {
            "items": [
                {
                    "measurement_root_id": "20000000-0000-4000-8000-000000000090",
                    "session_id": "20000000-0000-4000-8000-000000000091",
                    "supersedes_session_id": None,
                    "current": True,
                    "measured_on": "2026-10-10",
                    "measured_time": "08:00:00",
                    "conditions": "synthetic morning measurement",
                    "comment": None,
                    "confirmation_id": "20000000-0000-4000-8000-000000000092",
                    "values": [
                        {
                            "measurement_value_id": "20000000-0000-4000-8000-000000000093",
                            "measurement_kind": "body_mass",
                            "original_value": "80.0",
                            "original_unit": "kilogram",
                            "normalized_value": "80.0",
                            "normalized_unit": "kilogram",
                            "conversion_version": "body_units_v1",
                            "side": None,
                            "method_description": None,
                            "method_id": None,
                            "comment": None,
                            "normalization_status": "verified_v1",
                        }
                    ],
                }
            ],
            "next_cursor": None,
        }
    elif path.endswith("/measurements") and filters.get("mode") == "series":
        payload = {
            "points": [
                {
                    "session_id": "20000000-0000-4000-8000-000000000022",
                    "measurement_value_id": filters["method_source_value_id"],
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
        }
    elif path.endswith("/measurements") and filters.get("mode") == "bmi":
        raise HTTPException(status_code=422, detail="insufficient_data")
    else:
        raise HTTPException(status_code=404, detail="not_found")
    if path.endswith("/workouts/history") and filters.get("mode") == "all_confirmed":
        if filters.get("limit") == 1:
            page_two_cursor = "synthetic-all-confirmed-page-2"
            second_item = deepcopy(payload["items"][0])
            second_item["session_ref"]["object_id"] = "20000000-0000-4000-8000-000000000037"
            second_item["session_ref"]["revision_id"] = "20000000-0000-4000-8000-000000000038"
            second_item["local_start_date"] = "2026-10-09"
            second_item["confirmed_at"] = "2026-10-09T09:30:00+07:00"
            second_item["confirmation_ref"]["object_id"] = "20000000-0000-4000-8000-000000000039"
            second_item["workout"]["session_id"] = "20000000-0000-4000-8000-000000000037"
            second_item["workout"]["session_revision_id"] = "20000000-0000-4000-8000-000000000038"
            second_item["workout"]["local_start_date"] = "2026-10-09"
            second_item["workout"]["started_at"] = "2026-10-09T08:30:00+07:00"
            second_item["workout"]["ended_at"] = "2026-10-09T09:30:00+07:00"
            second_item["workout"]["name"] = "Synthetic strength session page 2"

            cursor = filters.get("cursor")
            if cursor is None:
                payload["next_cursor"] = page_two_cursor
            elif cursor == page_two_cursor:
                payload["items"] = [second_item]
                payload["next_cursor"] = None
            else:
                raise HTTPException(status_code=410, detail="history_snapshot_expired")
        payload.update(
            {
                "snapshot_id": "20000000-0000-4000-8000-000000000099",
                "snapshot_confirmed_at": "2026-10-11T10:00:00Z",
                "total_count": 2 if filters.get("limit") == 1 else len(payload["items"]),
            }
        )
    return payload


async def read_query_response(
    request: Request,
    authorization: str,
    x_correlation_id: str,
) -> dict[str, Any]:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthenticated")
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("context"), dict):
        raise HTTPException(status_code=400, detail="validation_error")
    filters = {key: value for key, value in body.items() if key != "context"}
    if (
        request.url.path.endswith("/measurements")
        and filters.get("mode") == "bmi"
        and body["context"].get("conversation_id") == "synthetic-g04-bmi-positive"
    ):
        response = {
            "value": "24.691358",
            "unit_code": "kg_per_m2",
            "method": "bmi_latest_confirmed_height_v1",
            "body_mass": {
                "session_id": "20000000-0000-4000-8000-000000000301",
                "measurement_value_id": "20000000-0000-4000-8000-000000000302",
                "confirmation_id": "20000000-0000-4000-8000-000000000303",
                "measured_on": "2026-10-10",
                "measured_time": None,
                "original_value": "80.0",
                "original_unit": "kilogram",
                "normalized_value": "80.0",
                "normalized_unit": "kilogram",
                "conversion_version": "body_units_v1",
            },
            "height": {
                "session_id": "20000000-0000-4000-8000-000000000304",
                "measurement_value_id": "20000000-0000-4000-8000-000000000305",
                "confirmation_id": "20000000-0000-4000-8000-000000000306",
                "measured_on": "2025-01-15",
                "measured_time": None,
                "original_value": "180.0",
                "original_unit": "centimeter",
                "normalized_value": "1.8",
                "normalized_unit": "meter",
                "conversion_version": "body_units_v1",
            },
        }
    else:
        response = empty_query_response(request.url.path, filters)
    return {
        "data": {
            "synthetic": True,
            "filters": filters,
            "response": response,
        },
        "meta": meta(x_correlation_id, None),
    }


@app.post("/api/v1/queries/workouts/history")
async def query_workout_history(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
) -> dict[str, Any]:
    return await read_query_response(request, authorization, x_correlation_id)


@app.post("/api/v1/queries/progress")
async def query_progress(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
) -> dict[str, Any]:
    return await read_query_response(request, authorization, x_correlation_id)


@app.post("/api/v1/queries/record-candidates")
async def query_record_candidates(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
) -> dict[str, Any]:
    return await read_query_response(request, authorization, x_correlation_id)


@app.post("/api/v1/queries/records")
async def query_records(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
) -> dict[str, Any]:
    return await read_query_response(request, authorization, x_correlation_id)


@app.post("/api/v1/queries/measurements")
async def query_measurements(
    request: Request,
    authorization: str = Header(),
    x_correlation_id: str = Header(),
) -> dict[str, Any]:
    return await read_query_response(request, authorization, x_correlation_id)
