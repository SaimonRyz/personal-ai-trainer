from copy import deepcopy
from typing import Any

import pytest

from trainer.domain.context import DomainValidationError
from trainer.domain.evidence import (
    PROTOCOL_CHECKS,
    PROTOCOL_VERSION,
    validate_protocol,
    validate_recommendation_basis,
)


def protocol() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "decision": "verified",
        "checklist": [
            {"check_id": code, "result": True, "note": None} for code in sorted(PROTOCOL_CHECKS)
        ],
    }


def recommendation() -> dict[str, Any]:
    return {
        "decision_text": "Temporary volume",
        "short_explanation": "No verified basis: provisional choice based on confirmed history.",
        "revisit_condition": "Next confirmed session",
        "fact_links": [{"ref": "exact-test-ref"}],
        "conclusion_links": [],
        "confidence": "insufficient",
        "assumptions": {"assumption": "Use a reversible provisional volume"},
    }


def test_complete_owner_protocol_and_honest_insufficient() -> None:
    validate_protocol(protocol())
    validate_recommendation_basis(recommendation())


@pytest.mark.parametrize("change", ["missing", "duplicate", "failed", "unknown", "version"])
def test_verification_is_fail_closed(change: str) -> None:
    p = protocol()
    if change == "missing":
        p["checklist"].pop()
    elif change == "duplicate":
        p["checklist"].append(deepcopy(p["checklist"][0]))
    elif change == "failed":
        p["checklist"][0]["result"] = False
    elif change == "unknown":
        p["checklist"][0]["check_id"] = "model_says_verified"
    else:
        p["protocol_version"] = "automatic"
    with pytest.raises(DomainValidationError):
        validate_protocol(p)


@pytest.mark.parametrize("confidence", ["low", "moderate", "high"])
def test_no_scientific_support_forbids_confident_claim(confidence: str) -> None:
    rec = recommendation()
    rec["confidence"] = confidence
    with pytest.raises(DomainValidationError, match="insufficient"):
        validate_recommendation_basis(rec)


@pytest.mark.parametrize("assumptions", [{}, {"x": None}, {"x": "  "}, {"x": False}])
def test_insufficient_requires_a_textual_assumption(assumptions: dict[str, Any]) -> None:
    rec = recommendation()
    rec["assumptions"] = assumptions
    with pytest.raises(DomainValidationError, match="assumption"):
        validate_recommendation_basis(rec)


def test_conflict_reduces_confidence() -> None:
    rec = recommendation()
    rec["conclusion_links"] = [
        {
            "role": "supports",
            "status_at_decision": "verified",
            "applicability_assessment": {"eligible": True},
        },
        {"role": "contradicts"},
    ]
    rec["confidence"] = "high"
    with pytest.raises(DomainValidationError, match="reduced"):
        validate_recommendation_basis(rec)
    rec["confidence"] = "low"
    validate_recommendation_basis(rec)
