# ruff: noqa: E501
"""Synthetic E-G07 HTTP/DB probe; only a disposable isolated volume is allowed."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from evidence_fixture import insufficient_recommendations, synthetic_plan
from pydantic import TypeAdapter
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from trainer.domain.context import digest_payload
from trainer.domain.evidence import PROTOCOL_CHECKS, PROTOCOL_VERSION
from trainer.infrastructure.db.evidence import EvidenceRepository
from trainer.transport.http.contracts import (
    ConfirmRequest,
    PlanDraftRequest,
    PlanLifecycleRequest,
    PreviewRequest,
    PreviewShownRequest,
)
from trainer.transport.http.evidence_contracts import (
    EvidenceCommandRequest,
    VerificationDraftRequest,
)

BASE = os.environ["TRAINER_TEST_BASE_URL"]
AGENT = os.environ["TRAINER_API_TOKEN"]
OWNER = os.environ["TRAINER_OWNER_VERIFIER_TOKEN"]
ENGINE = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])


def context(message: str) -> dict[str, str]:
    return {
        "conversation_id": "synthetic-eg07",
        "message_id": message,
        "effective_at": datetime.now(UTC).isoformat(),
        "timezone": "UTC",
        "locale": "ru",
    }


def post(
    path: str,
    body: dict[str, Any],
    *,
    key: str | None = None,
    owner: bool = False,
    expected: int | tuple[int, ...] = 200,
) -> dict[str, Any]:
    if key is not None:
        model = (
            PlanDraftRequest
            if path.endswith("/plan-drafts")
            else PlanLifecycleRequest
            if path.endswith("/plan-lifecycle-drafts")
            else EvidenceCommandRequest
            if path.endswith("/evidence-drafts")
            else VerificationDraftRequest
            if path.endswith("/conclusion-verification-drafts")
            else PreviewShownRequest
            if path.endswith("/shown")
            else ConfirmRequest
            if path.endswith("/confirm")
            else PreviewRequest
        )
        normalized = (
            TypeAdapter(model)
            .validate_python({**body, "request_digest": "0" * 64})
            .model_dump(mode="json")
        )
        normalized.pop("request_digest")
        body = {**normalized, "request_digest": digest_payload(normalized)}
    headers = {
        "Authorization": f"Bearer {OWNER if owner else AGENT}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status, result = response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        status, result = error.code, json.loads(error.read())
    assert status in (expected if isinstance(expected, tuple) else (expected,)), (
        path,
        status,
        result,
    )
    return result


def prepare(
    draft: dict[str, Any], prefix: str, *, owner: bool = False
) -> tuple[str, dict[str, Any]]:
    preview = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        key=prefix + "-preview",
        owner=owner,
    )["data"]
    message = prefix + "-shown"
    post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        {
            "context": context(message),
            "revision_id": draft["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": message,
            "displayed_text_digest": hashlib.sha256(preview["canonical_text"].encode()).hexdigest(),
            "cross_dialog": False,
        },
        key=message,
        owner=owner,
    )
    return preview["preview_id"], {
        "context": context(message),
        "revision_id": draft["revision_id"],
        "preview_digest": preview["digest"],
        "confirmation_kind": preview["confirmation_kind"],
        "confirmation_message_id": message,
        "explicit_intent": "Confirm synthetic fixture only",
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }


def commit(draft: dict[str, Any], prefix: str, *, owner: bool = False) -> dict[str, Any]:
    preview, body = prepare(draft, prefix, owner=owner)
    path = f"/api/v1/commands/previews/{preview}/confirm"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: post(path, body, key=prefix + "-confirm", owner=owner)["data"], range(2)
            )
        )
    assert {r["status"] for r in results} == {"committed", "already_committed"}
    result = next(r for r in results if r["status"] == "committed")
    replay = next(r for r in results if r["status"] == "already_committed")
    assert replay["status"] == "already_committed"
    assert replay["commit_result_id"] == result["commit_result_id"]
    return result


def evidence_body(conclusion_id: str | None = None) -> dict[str, Any]:
    return {
        "context": context("evidence"),
        "evidence": {
            "sources": [
                {
                    "source": {
                        "citation_key": "synthetic-eg07-source",
                        "title": "SYNTHETIC fixture, no scientific claim",
                        "authors_or_org": "Synthetic gate",
                        "publication_year": 2026,
                        "source_type": "controlled_trial",
                        "doi": None,
                        "pmid": None,
                        "url": "https://example.invalid/synthetic-only",
                        "metadata_snapshot": {"synthetic": True},
                        "availability": "available",
                        "conflict_of_interest_note": "Synthetic fixture only",
                    },
                    "relation": "supports",
                    "cited_locator": "Synthetic table 1",
                    "note": "Do not use as evidence",
                }
            ],
            "conclusion": {
                "conclusion_id": conclusion_id,
                "topic": "synthetic-eg07",
                "statement": "Synthetic gate statement; not a real recommendation",
                "population_conditions": "Synthetic all",
                "confidence": "low",
                "review_due_on": (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
                "applicability": [
                    {
                        "population": "all",
                        "goal": None,
                        "activity_or_load": None,
                        "setting": None,
                        "inclusion": {},
                        "exclusion": {},
                        "readable_form": "Synthetic all",
                    }
                ],
                "caveats": [{"caveat_type": "limitation", "description": "Synthetic data only"}],
            },
        },
    }


def verification(version: str, decision: str = "verified") -> dict[str, Any]:
    return {
        "context": context("verify"),
        "conclusion_version_id": version,
        "protocol_version": PROTOCOL_VERSION,
        "checklist": [
            {"check_id": c, "result": True, "note": "Synthetic probe"}
            for c in sorted(PROTOCOL_CHECKS)
        ],
        "decision": decision,
        "reason": "Synthetic owner protocol probe",
    }


def eligible(topic: str = "synthetic-eg07", decision_date: str | None = None) -> dict[str, Any]:
    return post(
        "/api/v1/queries/conclusions/eligible",
        {
            "context": context("eligible"),
            "topic": topic,
            "applicability_snapshot": {},
            "decision_date": decision_date or datetime.now(UTC).date().isoformat(),
        },
    )["data"]


def plan_draft(
    prefix: str, *, version: str | None = None, confidence: str | None = None, expected: int = 200
) -> dict[str, Any]:
    recs = insufficient_recommendations(ENGINE)
    if version is not None:
        recs[0]["confidence"] = "low"
        recs[0]["conclusion_links"] = [{"conclusion_version_id": version, "role": "supports"}]
        recs[0]["short_explanation"] = "Synthetic decision using the exact synthetic verified basis"
    if confidence is not None:
        recs[0]["confidence"] = confidence
    return post(
        "/api/v1/commands/plan-drafts",
        {
            "context": context(prefix),
            "plan": synthetic_plan(ENGINE, prefix),
            "recommendations": recs,
        },
        key=prefix,
        expected=expected,
    )


def explanation(recommendation_id: str) -> dict[str, Any]:
    return post(
        "/api/v1/queries/recommendations/explanation",
        {
            "context": context("explanation"),
            "recommendation_id": recommendation_id,
            "detail_level": "detailed",
        },
    )["data"]


def read_plan(result: dict[str, Any]) -> dict[str, Any]:
    ref = next(r for r in result["created_revisions"] if r["object_type"] == "workout_plan")
    return post(
        "/api/v1/queries/plans/get",
        {
            "context": context("exact-plan"),
            "mode": "exact",
            "plan_id": ref["object_id"],
            "revision_id": ref["revision_id"],
        },
    )["data"]


def main() -> None:
    body = evidence_body()
    path = "/api/v1/commands/evidence-drafts"
    before = eligible()
    assert before["items"] == [] and before["gaps"]
    draft = post(path, body, key="eg07-evidence")["data"]
    assert post(path, body, key="eg07-evidence")["data"] == draft
    assert eligible()["items"] == []
    # Fault after every domain/status/confirmation/result/audit write must roll back all of them.
    fault_body = evidence_body()
    fault_body["evidence"]["sources"][0]["source"]["citation_key"] = "synthetic-eg07-rollback"
    fault_draft = post(path, fault_body, key="eg07-rollback-draft")["data"]
    fault_preview, fault_confirm = prepare(fault_draft, "eg07-rollback")
    normalized = ConfirmRequest.model_validate(
        {**fault_confirm, "request_digest": "0" * 64}
    ).model_dump(mode="json")
    normalized.pop("request_digest")
    normalized["request_digest"] = digest_payload(normalized)
    tables = (
        "scientific_sources",
        "conclusions",
        "conclusion_versions",
        "conclusion_source_links",
        "knowledge_status_revisions",
        "verification_protocol_runs",
        "confirmations",
        "commit_results",
        "domain_changes",
        "agent_operation_audit",
        "idempotent_operations",
    )
    with Session(ENGINE) as session:
        before = [
            session.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            for table in tables
        ]
        try:
            EvidenceRepository(session).confirm(
                preview_id=UUID(fault_preview),
                payload=normalized,
                key="eg07-rollback-confirm",
                correlation_id=uuid4(),
                fail_before_commit=True,
            )
        except RuntimeError as error:
            assert "synthetic fault" in str(error)
            session.rollback()
        else:
            raise AssertionError("evidence fault did not roll back")
        after = [
            session.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            for table in tables
        ]
        assert before == after
    # Discarding the draft is unnecessary: its exact preview remains retryable.
    post(
        f"/api/v1/commands/previews/{fault_preview}/confirm",
        fault_confirm,
        key="eg07-rollback-confirm",
    )
    # A real plan has an explanation even when no scientific basis exists.
    insufficient = plan_draft("eg07-insufficient")["data"]
    insufficient_saved = read_plan(commit(insufficient, "eg07-insufficient"))
    insufficient_explanation = explanation(insufficient_saved["recommendation_ids"][0])
    assert insufficient_explanation["confidence"] == "insufficient"
    assert insufficient_explanation["fact_refs"] and insufficient_explanation["assumptions"]
    assert insufficient_explanation["conclusion_basis"] == []
    for confidence in ("low", "moderate", "high"):
        plan_draft("eg07-unsupported-" + confidence, confidence=confidence, expected=400)
    result = commit(draft, "eg07-evidence")
    conclusion = next(r for r in result["created_revisions"] if r["object_type"] == "conclusion")
    version = conclusion["revision_id"]
    assert eligible()["items"] == []
    vpath = "/api/v1/commands/conclusion-verification-drafts"
    vbody = verification(version)
    assert (
        post(vpath, vbody, key="eg07-agent-verify", expected=403)["error"]["code"]
        == "capability_required"
    )
    vd = post(vpath, vbody, key="eg07-owner-verify", owner=True)["data"]
    post(
        "/api/v1/commands/previews",
        {
            "context": context("agent-preview"),
            "revision_id": vd["revision_id"],
            "detail_level": "full",
        },
        key="eg07-agent-preview",
        expected=403,
    )
    preview, confirmation = prepare(vd, "eg07-owner-verify", owner=True)
    confirm_path = f"/api/v1/commands/previews/{preview}/confirm"
    post(confirm_path, confirmation, key="eg07-agent-confirm", expected=403)
    verified = post(confirm_path, confirmation, key="eg07-owner-confirm", owner=True)["data"]
    assert verified["status"] == "committed"
    assert [i["conclusion_version_id"] for i in eligible()["items"]] == [version]
    plan_draft("eg07-overconfident", version=version, confidence="high", expected=422)
    supported = plan_draft("eg07-supported", version=version)["data"]
    supported_preview = post(
        "/api/v1/commands/previews",
        {
            "context": context("basis-preview"),
            "revision_id": supported["revision_id"],
            "detail_level": "full",
        },
        key="eg07-basis-preview",
    )["data"]
    assert "SYNTHETIC fixture, no scientific claim" in supported_preview["canonical_text"]
    assert "Synthetic data only" in supported_preview["canonical_text"]
    saved = read_plan(commit(supported, "eg07-supported"))
    recommendation_id = saved["recommendation_ids"][0]
    historical = explanation(recommendation_id)
    assert historical["fact_refs"] == [
        link["ref"] for link in saved["recommendations"][0]["fact_links"]
    ]
    assert historical["conclusion_basis"][0]["status_at_decision"] == "verified"
    assert historical["conclusion_basis"][0]["sources"][0]["relation"] == "supports"
    assert historical["conclusion_version_ids"] == [version]
    source_id = historical["conclusion_basis"][0]["sources"][0]["source_ref"]["object_id"]
    availability_update = {
        "context": context("availability-update"),
        "source_availability_update": {
            "source_id": source_id,
            "expected_availability": "available",
            "expected_last_checked_at": None,
            "new_availability": "unavailable",
            "reason": "Synthetic URL availability probe",
        },
    }
    availability_draft = post(path, availability_update, key="eg07-availability")["data"]
    assert post(path, availability_update, key="eg07-availability")["data"] == availability_draft
    # Confirm through one exact preview; its displayed check time is persisted.
    availability_preview = post(
        "/api/v1/commands/previews",
        {
            "context": context("availability-preview-check"),
            "revision_id": availability_draft["revision_id"],
            "detail_level": "full",
        },
        key="eg07-availability-preview-check",
    )["data"]
    canonical_text = availability_preview["canonical_text"]
    assert "available → unavailable" in canonical_text
    assert "Предыдущее время проверки: не было" in canonical_text
    assert "Новое время проверки (будет сохранено): " in canonical_text
    checked_at_text = canonical_text.split("Новое время проверки (будет сохранено): ", 1)[1].split(
        "\n", 1
    )[0]
    shown_message = "eg07-availability-shown"
    post(
        f"/api/v1/commands/previews/{availability_preview['preview_id']}/shown",
        {
            "context": context(shown_message),
            "revision_id": availability_draft["revision_id"],
            "preview_digest": availability_preview["digest"],
            "shown_message_id": shown_message,
            "displayed_text_digest": hashlib.sha256(canonical_text.encode()).hexdigest(),
            "cross_dialog": False,
        },
        key=shown_message,
    )
    availability_confirm = {
        "context": context(shown_message),
        "revision_id": availability_draft["revision_id"],
        "preview_digest": availability_preview["digest"],
        "confirmation_kind": availability_preview["confirmation_kind"],
        "confirmation_message_id": shown_message,
        "explicit_intent": "Confirm synthetic fixture only",
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }
    availability_result = post(
        f"/api/v1/commands/previews/{availability_preview['preview_id']}/confirm",
        availability_confirm,
        key="eg07-availability-confirm",
    )["data"]
    assert availability_result["status"] == "committed"
    availability_after = explanation(recommendation_id)["conclusion_basis"][0]["sources"][0]
    assert availability_after["metadata_snapshot"]["availability"] == "available"
    assert availability_after["current_availability"] == "unavailable"
    assert availability_after["last_checked_at"] is not None
    with ENGINE.connect() as db:
        persisted_checked_at = db.execute(
            text("SELECT last_checked_at FROM trainer.scientific_sources WHERE source_id=:id"),
            {"id": source_id},
        ).scalar_one()
    assert persisted_checked_at == datetime.fromisoformat(checked_at_text.replace("Z", "+00:00"))
    assert availability_after["current_availability"] == "unavailable"
    assert [item["conclusion_version_id"] for item in eligible()["items"]] == [version]
    stale = plan_draft("eg07-status-stale-plan", version=version)["data"]
    stale_preview, stale_confirmation = prepare(stale, "eg07-status-stale-plan")
    # New content is a new draft version; immutable previous version stays verified historically.
    revised = evidence_body(conclusion["object_id"])
    revised["evidence"]["conclusion"]["statement"] += " revision 2"
    rd = post(path, revised, key="eg07-revised")["data"]
    revised_result = commit(rd, "eg07-revised")
    new_version = next(
        r["revision_id"]
        for r in revised_result["created_revisions"]
        if r["object_type"] == "conclusion"
    )
    assert new_version != version and eligible()["items"] == []
    post(
        f"/api/v1/commands/previews/{stale_preview}/confirm",
        stale_confirmation,
        key="eg07-stale-plan-confirm",
        expected=409,
    )
    after = explanation(recommendation_id)
    assert after["conclusion_version_ids"] == historical["conclusion_version_ids"]
    assert len(after["conclusion_basis"]) == len(historical["conclusion_basis"]) == 1
    after_basis = after["conclusion_basis"][0]
    original_basis = historical["conclusion_basis"][0]
    assert after_basis["conclusion_version_ref"] == original_basis["conclusion_version_ref"]
    assert after_basis["status_at_decision"] == original_basis["status_at_decision"] == "verified"
    assert after_basis["current_status"] == original_basis["current_status"] == "verified"
    assert after_basis["review_due_on"] == original_basis["review_due_on"]
    assert after_basis["sources"][0]["source_ref"] == original_basis["sources"][0]["source_ref"]
    assert after_basis["sources"][0]["relation"] == original_basis["sources"][0]["relation"]
    assert (
        after_basis["sources"][0]["cited_locator"] == original_basis["sources"][0]["cited_locator"]
    )
    assert (
        after_basis["sources"][0]["metadata_snapshot"]
        == original_basis["sources"][0]["metadata_snapshot"]
    )
    assert after_basis["sources"][0]["metadata_snapshot"]["availability"] == "available"
    assert after_basis["sources"][0]["current_availability"] == "unavailable"
    assert after_basis["sources"][0]["last_checked_at"] is not None
    assert after["stale_markers"] and after["explanation"] == historical["explanation"]
    # Replacement plans also need a separate nonempty explanation and full basis preview.
    source = insufficient_saved["plan_ref"]
    replacement = post(
        "/api/v1/commands/plan-lifecycle-drafts",
        {
            "context": context("replacement"),
            "intent": "reschedule",
            "source_ref": source,
            "expected_current_revision_id": source["revision_id"],
            "reason": "Synthetic replacement",
            "replacement_plan": synthetic_plan(ENGINE, "eg07-replacement"),
            "replacement_recommendations": insufficient_recommendations(ENGINE),
        },
        key="eg07-replacement",
    )["data"]
    replacement_preview = post(
        "/api/v1/commands/previews",
        {
            "context": context("replacement-preview"),
            "revision_id": replacement["revision_id"],
            "detail_level": "full",
        },
        key="eg07-replacement-basis",
    )["data"]
    assert "Evidence basis:" in replacement_preview["canonical_text"]
    assert "Уверенность: недостаточно данных" in replacement_preview["canonical_text"]
    assert (
        "Нет достаточной применимой проверенной научной опоры; решение основано на перечисленных допущениях."
        in replacement_preview["canonical_text"]
    )
    replacement_result = commit(replacement, "eg07-replacement")
    replacement_refs = [
        r
        for r in replacement_result["created_revisions"]
        if r["object_type"] == "workout_plan" and r["object_id"] != source["object_id"]
    ]
    assert len(replacement_refs) == 1
    replacement_saved = read_plan({"created_revisions": replacement_refs})
    assert explanation(replacement_saved["recommendation_ids"][0])["confidence"] == "insufficient"
    # A new key can reject the current version; replay of the old verification remains exact.
    rejected = post(vpath, verification(new_version, "rejected"), key="eg07-reject", owner=True)[
        "data"
    ]
    commit(rejected, "eg07-reject", owner=True)
    assert post(vpath, vbody, key="eg07-owner-verify", owner=True)["data"] == vd
    with ENGINE.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM trainer.scientific_sources WHERE citation_key='synthetic-eg07-source'"
                )
            ).scalar_one()
            == 1
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM trainer.conclusion_versions WHERE conclusion_id=:id"),
                {"id": conclusion["object_id"]},
            ).scalar_one()
            == 2
        )
        assert (
            db.execute(
                text(
                    "SELECT status FROM trainer.knowledge_status_revisions WHERE conclusion_version_id=:id ORDER BY revision_no DESC LIMIT 1"
                ),
                {"id": version},
            ).scalar_one()
            == "verified"
        )
    # Conflicting duplicate citation metadata is rejected, not silently overwritten.
    conflict = deepcopy(body)
    conflict["evidence"]["sources"][0]["source"]["title"] = "Different synthetic identity"
    post(path, conflict, key="eg07-source-conflict", expected=409)
    # Expired evidence cannot be restored by a caller-supplied historical date.
    expired = evidence_body()
    expired["context"]["effective_at"] = "2000-01-01T00:00:00Z"
    expired["evidence"]["conclusion"]["topic"] = "synthetic-expired"
    expired["evidence"]["conclusion"]["review_due_on"] = (
        datetime.now(UTC).date() - timedelta(days=1)
    ).isoformat()
    expired_draft = post(path, expired, key="eg07-expired")["data"]
    expired_result = commit(expired_draft, "eg07-expired")
    expired_version = next(
        r["revision_id"]
        for r in expired_result["created_revisions"]
        if r["object_type"] == "conclusion"
    )
    post(vpath, verification(expired_version), key="eg07-expired-verify", owner=True, expected=422)
    assert eligible("synthetic-expired", "2000-01-01")["items"] == []
    # Both source roles survive the workflow and QRY-08 explicitly reports conflict.
    contradictory = evidence_body()
    contradictory["evidence"]["conclusion"]["topic"] = "synthetic-conflict"
    contrary_link = deepcopy(contradictory["evidence"]["sources"][0])
    contrary_link["relation"] = "contradicts"
    contradictory["evidence"]["sources"].append(contrary_link)
    cd = post(path, contradictory, key="eg07-conflict")["data"]
    cr = commit(cd, "eg07-conflict")
    cv = next(r["revision_id"] for r in cr["created_revisions"] if r["object_type"] == "conclusion")
    vd = post(vpath, verification(cv), key="eg07-conflict-verify", owner=True)["data"]
    commit(vd, "eg07-conflict-verify", owner=True)
    assert eligible("synthetic-conflict")["conflicts"]
    status_plan = plan_draft("eg07-status-only", version=cv)["data"]
    status_preview, status_confirmation = prepare(status_plan, "eg07-status-only")
    status_rejection = post(
        vpath, verification(cv, "rejected"), key="eg07-status-only-reject", owner=True
    )["data"]
    commit(status_rejection, "eg07-status-only-reject", owner=True)
    post(
        f"/api/v1/commands/previews/{status_preview}/confirm",
        status_confirmation,
        key="eg07-status-only-confirm",
        expected=409,
    )
    high_conflict = deepcopy(contradictory)
    high_conflict["evidence"]["conclusion"]["confidence"] = "high"
    high_draft = post(path, high_conflict, key="eg07-high-conflict")["data"]
    high_result = commit(high_draft, "eg07-high-conflict")
    high_version = next(
        r["revision_id"]
        for r in high_result["created_revisions"]
        if r["object_type"] == "conclusion"
    )
    post(
        vpath,
        verification(high_version),
        key="eg07-high-conflict-verify",
        owner=True,
        expected=422,
    )
    # Goal-specific scopes require an explicit exact goal fact, as program design does.
    with ENGINE.connect() as db:
        goal = db.execute(
            text(
                "SELECT to_jsonb(r) FROM trainer.goal_revisions r JOIN trainer.goals g ON g.current_revision_id=r.goal_revision_id WHERE r.status='active' AND g.archived_at IS NULL ORDER BY g.goal_id LIMIT 1"
            )
        ).scalar_one()
    goal_evidence = evidence_body()
    goal_evidence["evidence"]["conclusion"]["topic"] = "synthetic-goal"
    goal_evidence["evidence"]["conclusion"]["applicability"][0]["goal"] = goal["goal_type"]
    gd = post(path, goal_evidence, key="eg07-goal-evidence")["data"]
    gr = commit(gd, "eg07-goal-evidence")
    gv = next(r["revision_id"] for r in gr["created_revisions"] if r["object_type"] == "conclusion")
    gvd = post(vpath, verification(gv), key="eg07-goal-verify", owner=True)["data"]
    commit(gvd, "eg07-goal-verify", owner=True)
    assert eligible("synthetic-goal")["items"] == []  # unknown goal never matches
    plan_draft("eg07-goal-unknown", version=gv, expected=422)
    goal_recs = insufficient_recommendations(ENGINE)
    goal_recs[0]["confidence"] = "low"
    goal_recs[0]["conclusion_links"] = [{"conclusion_version_id": gv, "role": "supports"}]
    goal_recs[0]["fact_links"].append(
        {
            "ref": {
                "object_type": "goal",
                "object_id": goal["goal_id"],
                "revision_id": goal["goal_revision_id"],
            },
            "role": "baseline",
            "reason": "Exact synthetic dated goal",
            "snapshot_digest": digest_payload(goal),
        }
    )
    goal_plan = post(
        "/api/v1/commands/plan-drafts",
        {
            "context": context("goal-plan"),
            "plan": synthetic_plan(ENGINE, "eg07-goal-plan"),
            "recommendations": goal_recs,
        },
        key="eg07-goal-plan",
    )["data"]
    goal_saved = read_plan(commit(goal_plan, "eg07-goal-plan"))
    assert explanation(goal_saved["recommendation_ids"][0])["conclusion_basis"][0][
        "applicability_assessment"
    ]["eligible"]
    post(
        "/api/v1/queries/recommendations/explanation",
        {
            "context": context("missing-recommendation"),
            "recommendation_id": str(uuid4()),
            "detail_level": "short",
        },
        expected=404,
    )
    # Two revisions frozen against one predecessor cannot both become current.
    candidates = []
    for index in range(2):
        candidate = evidence_body(conclusion["object_id"])
        candidate["evidence"]["conclusion"]["statement"] += f" concurrent revision {index}"
        candidate_draft = post(path, candidate, key=f"eg07-race-draft-{index}")["data"]
        candidates.append(prepare(candidate_draft, f"eg07-race-{index}"))

    def race(index: int) -> dict[str, Any]:
        candidate_preview, candidate_confirm = candidates[index]
        return post(
            f"/api/v1/commands/previews/{candidate_preview}/confirm",
            candidate_confirm,
            key=f"eg07-race-confirm-{index}",
            expected=(200, 409),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        race_results = list(pool.map(race, range(2)))
    assert sum(r.get("data", {}).get("status") == "committed" for r in race_results) == 1
    assert sum(r.get("error", {}).get("code") == "stale_preview" for r in race_results) == 1
    with ENGINE.connect() as db:
        assert (
            db.execute(
                text(
                    "SELECT state FROM trainer.idempotent_operations WHERE scope='local-owner:CMD-14' AND idempotency_key='eg07-source-conflict'"
                )
            ).scalar_one()
            == "failed"
        )
    # Exercise real app-role denial, rather than relying solely on grants metadata.
    for sql in (
        "UPDATE trainer.scientific_sources SET title='forbidden'",
        "DELETE FROM trainer.scientific_sources",
        "CREATE TABLE trainer.synthetic_eg07_forbidden(id integer)",
    ):
        with ENGINE.connect() as db:
            try:
                db.execute(text(sql))
            except Exception as error:
                db.rollback()
                assert "permission denied" in str(error)
            else:
                db.rollback()
                raise AssertionError("app role exceeded evidence privilege boundary")
    with ENGINE.connect() as db:
        protocol = db.execute(
            text(
                "SELECT protocol_run_id FROM trainer.verification_protocol_runs WHERE conclusion_version_id=:id LIMIT 1"
            ),
            {"id": version},
        ).scalar_one()
    with ENGINE.connect() as db:
        try:
            db.execute(
                text(
                    "INSERT INTO trainer.knowledge_status_revisions(status_revision_id,conclusion_version_id,revision_no,status,effective_at,reason,protocol_run_id,created_at) VALUES (:id,:version,999,'verified',now(),'synthetic mismatch',:protocol,now())"
                ),
                {"id": uuid4(), "version": new_version, "protocol": protocol},
            )
        except Exception as error:
            db.rollback()
            assert "invalid knowledge protocol ownership" in str(error)
        else:
            db.rollback()
            raise AssertionError("mismatched protocol accepted")
    print(
        "PASS: E-G07 draft/owner-only verification/replay/source identity/new-version-draft/history/rejection"
    )


if __name__ == "__main__":
    main()
