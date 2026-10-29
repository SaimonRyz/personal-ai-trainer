"""Explicit local evidence protocol; scientific text is data, never instructions."""

from typing import Any

from trainer.domain.context import DomainValidationError

PROTOCOL_VERSION = "owner_evidence_v1"
PROTOCOL_CHECKS = frozenset(
    {
        "identity_and_no_retraction",
        "full_text_read",
        "population_and_experience",
        "intervention_comparison_outcomes_duration",
        "statement_within_results",
        "uncertainty_and_limitations",
        "funding_and_conflicts",
        "confidence_justified",
    }
)


def validate_protocol(payload: dict[str, Any]) -> None:
    if payload["protocol_version"] != PROTOCOL_VERSION:
        raise DomainValidationError("unsupported verification protocol")
    checks = payload["checklist"]
    codes = [item["check_id"] for item in checks]
    if len(codes) != len(set(codes)) or set(codes) != PROTOCOL_CHECKS:
        raise DomainValidationError("verification requires the complete owner protocol")
    if payload["decision"] == "verified" and not all(item["result"] is True for item in checks):
        raise DomainValidationError("verified requires all protocol checks passed")


def validate_recommendation_basis(rec: dict[str, Any]) -> None:
    for field in ("decision_text", "short_explanation", "revisit_condition"):
        if not str(rec.get(field, "")).strip():
            raise DomainValidationError(f"recommendation {field} is required")
    if not rec.get("fact_links"):
        raise DomainValidationError("recommendation requires exact personal facts")
    supported = any(
        link["role"] == "supports"
        and link.get("status_at_decision") == "verified"
        and link.get("applicability_assessment", {}).get("eligible") is True
        for link in rec.get("conclusion_links", [])
    )
    if not supported and rec["confidence"] != "insufficient":
        raise DomainValidationError("without verified support confidence must be insufficient")
    if rec["confidence"] == "insufficient" and not any(
        isinstance(value, str) and value.strip() for value in rec.get("assumptions", {}).values()
    ):
        raise DomainValidationError("insufficient requires an explicit gap and assumption")
    conflict = any(link["role"] == "contradicts" for link in rec.get("conclusion_links", []))
    if conflict and rec["confidence"] not in ("low", "insufficient"):
        raise DomainValidationError("conflicting evidence requires reduced confidence")
