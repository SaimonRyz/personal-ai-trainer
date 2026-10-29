from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.domain.context import DomainValidationError
from trainer.domain.program import assess_applicability, validate_program_draft
from trainer.transport.http.contracts import (
    ProgramDraftReplacement,
    ProgramDraftRequest,
    ProgramResponse,
)


def program_payload() -> dict[str, Any]:
    return {
        "intent": "create",
        "program_id": None,
        "expected_current_version_id": None,
        "program": {
            "name": "Synthetic",
            "starts_on": "2026-09-01",
            "ends_on": "2026-12-31",
            "status_code": "active",
        },
        "goal_revision_id": str(uuid4()),
        "version": {
            "version_label": "v1",
            "rationale": "Synthetic",
            "period_start": "2026-09-01",
            "period_end": "2026-10-01",
            "recommendations": [],
            "templates": [
                {
                    "position": 1,
                    "day_type_code": "training",
                    "purpose": "Synthetic",
                    "blocks": [
                        {
                            "position": 1,
                            "block_type_code": "straight",
                            "exercises": [
                                {
                                    "position": 1,
                                    "exercise_ref": {
                                        "object_type": "exercise",
                                        "object_id": str(uuid4()),
                                        "revision_id": str(uuid4()),
                                    },
                                    "sets": [
                                        {
                                            "position": 1,
                                            "logical_set_no": 1,
                                            "side": "both",
                                            "set_type_code": "working",
                                            "metrics": [
                                                {
                                                    "metric_kind": "repetitions",
                                                    "exact_value": "8",
                                                    "unit_code": "repetition",
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
        },
    }


def test_valid_program_and_new_version() -> None:
    body = program_payload()
    validate_program_draft(body)
    body.update(
        intent="new_version",
        program=None,
        program_id=str(uuid4()),
        expected_current_version_id=str(uuid4()),
    )
    validate_program_draft(body)


@pytest.mark.parametrize(
    "exact,low,high",
    [
        (None, None, None),
        ("8", "7", None),
        ("8", None, "9"),
        ("8", "7", "9"),
        (None, "7", None),
        (None, None, "9"),
        (None, "9", "7"),
    ],
)
def test_metric_exact_xor_range(exact: str | None, low: str | None, high: str | None) -> None:
    body = program_payload()
    body["version"]["templates"][0]["blocks"][0]["exercises"][0]["sets"][0]["metrics"][0].update(
        exact_value=exact, min_value=low, max_value=high
    )
    with pytest.raises(DomainValidationError):
        validate_program_draft(body)


@pytest.mark.parametrize("kind", ["superset", "circuit"])
def test_round_structures_and_missing_circuit_positions(kind: str) -> None:
    body = program_payload()
    block = body["version"]["templates"][0]["blocks"][0]
    block.update(block_type_code=kind, declared_rounds=2)
    block["exercises"].append(deepcopy(block["exercises"][0]))
    for index, ex in enumerate(block["exercises"], 1):
        ex.update(position=index, circuit_position=index)
        ex["sets"][0]["round_no"] = 1
    validate_program_draft(body)
    block["exercises"][0]["circuit_position"] = None
    with pytest.raises(DomainValidationError):
        validate_program_draft(body)


@pytest.mark.parametrize("level", ["templates", "blocks", "exercises", "sets"])
def test_duplicate_positions(level: str) -> None:
    body = program_payload()
    node = body["version"]
    for key in ("templates", "blocks", "exercises", "sets"):
        if key == level:
            node[key].append(deepcopy(node[key][0]))
            break
        node = node[key][0]
    with pytest.raises(DomainValidationError):
        validate_program_draft(body)


def test_closed_program_contracts_and_required_view_children() -> None:
    body = program_payload()
    body.update(
        context={
            "conversation_id": "synthetic",
            "message_id": "draft",
            "effective_at": "2026-09-17T00:00:00Z",
            "timezone": "UTC",
        },
        request_digest="0" * 64,
    )
    ProgramDraftRequest.model_validate(body)
    body["version"]["templates"][0]["planned_date"] = "2026-09-17"
    with pytest.raises(ValidationError):
        ProgramDraftRequest.model_validate(body)
    schema = ProgramResponse.model_json_schema()
    for name, definition in schema["$defs"].items():
        if name.startswith("Program") or name.startswith("Recommendation"):
            assert definition["additionalProperties"] is False
    assert "templates" in schema["$defs"]["ProgramVersionView"]["required"]


def test_applicability_is_conservative_and_server_owned() -> None:
    scope = {
        "scope_id": str(uuid4()),
        "population": "all",
        "goal": None,
        "activity_or_load": None,
        "setting": None,
        "inclusion": {"sex": "male"},
        "exclusion": {},
    }
    assert assess_applicability([scope], {"sex": "male"})["outcome"] == "applicable"
    assert assess_applicability([scope], {})["outcome"] == "insufficient"
    scope["population"] = "free text cannot be inferred"
    assert assess_applicability([scope], {"sex": "male"})["outcome"] == "insufficient"


@pytest.mark.parametrize(
    "path,value",
    [
        ("/version", str(uuid4())),
        ("/program", program_payload()["version"]),
        ("/goal_revision_id", program_payload()["program"]),
        ("/intent", "new_version"),
        ("/version/templates/0/position", 2),
    ],
)
def test_replacement_paths_and_target_schemas_are_closed(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        ProgramDraftReplacement.model_validate({"op": "replace", "path": path, "value": value})
