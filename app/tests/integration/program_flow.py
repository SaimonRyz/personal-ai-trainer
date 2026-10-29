# ruff: noqa: E501
"""Synthetic HTTP/PostgreSQL D-G04 acceptance; no access to the main volume."""

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

from pydantic import TypeAdapter
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.evidence import EvidenceRepository as ContextRepository
from trainer.transport.http.contracts import (
    ConfirmRequest,
    InventoryChangeDraftRequest,
    PreviewRequest,
    PreviewShownRequest,
    ProgramDraftRequest,
    ProgramDraftRevisionRequest,
    ProgramPublicationDraftRequest,
    ProgramResponse,
)
from trainer.transport.http.measurement_contracts import MeasurementDraftRequest

BASE = os.environ["TRAINER_TEST_BASE_URL"]
TOKEN = os.environ["TRAINER_API_TOKEN"]


def context(message: str) -> dict[str, Any]:
    return {
        "conversation_id": "synthetic-dg04",
        "message_id": message,
        "effective_at": datetime.now(UTC).isoformat(),
        "timezone": "UTC",
        "locale": "ru",
    }


def post(
    path: str,
    body: dict[str, Any],
    key: str | None = None,
    expected: int = 200,
    normalize: bool = True,
) -> dict[str, Any]:
    if key:
        model = (
            ConfirmRequest
            if path.endswith("/confirm")
            else PreviewShownRequest
            if path.endswith("/shown")
            else PreviewRequest
            if path.endswith("/previews")
            else ProgramDraftRevisionRequest
            if path.endswith("/revisions")
            else ProgramPublicationDraftRequest
            if path.endswith("/program-publication-drafts")
            else ProgramDraftRequest
        )
        if normalize:
            if path.endswith("/inventory-change-drafts"):
                body = (
                    TypeAdapter(InventoryChangeDraftRequest)
                    .validate_python({**body, "request_digest": "0" * 64})
                    .model_dump(mode="json")
                )
            elif path.endswith("/measurement-session-drafts"):
                body = (
                    TypeAdapter(MeasurementDraftRequest)
                    .validate_python({**body, "request_digest": "0" * 64})
                    .model_dump(mode="json")
                )
            else:
                body = model.model_validate({**body, "request_digest": "0" * 64}).model_dump(
                    mode="json"
                )
        body.pop("request_digest", None)
        body = {**body, "request_digest": digest_payload(body)}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
    }
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as result:
            assert result.status == expected
            return json.loads(result.read())
    except urllib.error.HTTPError as error:
        result = json.loads(error.read())
        assert error.code == expected, (error.code, result)
        return result


def preview(draft: dict[str, Any], prefix: str) -> dict[str, Any]:
    result = post(
        "/api/v1/commands/previews",
        {
            "context": context(prefix + "-preview"),
            "revision_id": draft["revision_id"],
            "detail_level": "full",
        },
        prefix + "-preview",
    )["data"]
    message = prefix + "-shown"
    shown = post(
        f"/api/v1/commands/previews/{result['preview_id']}/shown",
        {
            "context": context(message),
            "revision_id": draft["revision_id"],
            "preview_digest": result["digest"],
            "shown_message_id": message,
            "displayed_text_digest": hashlib.sha256(result["canonical_text"].encode()).hexdigest(),
            "cross_dialog": False,
        },
        message,
    )["data"]
    assert shown["confirmation_kind"] == result["confirmation_kind"]
    return shown


def confirm_body(p: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "context": context(message),
        "revision_id": p["revision_id"],
        "preview_digest": p["digest"],
        "confirmation_kind": p["confirmation_kind"],
        "confirmation_message_id": message,
        "explicit_intent": "synthetic publication confirmation",
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }


def publish(
    body: dict[str, Any], prefix: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    draft_body = {"context": context(prefix + "-draft"), **body}
    draft = post("/api/v1/commands/program-drafts", deepcopy(draft_body), prefix + "-draft")["data"]
    assert (
        post("/api/v1/commands/program-drafts", deepcopy(draft_body), prefix + "-draft")["data"]
        == draft
    )
    assert not draft["gaps"], draft
    technical = preview(draft, prefix + "-technical")
    rejected = post(
        f"/api/v1/commands/previews/{technical['preview_id']}/confirm",
        confirm_body(technical, prefix + "-technical-confirm"),
        prefix + "-technical-confirm",
        409,
    )
    assert rejected["error"]["code"] == "confirmation_not_supported"
    assert rejected["error"]["required_next_action"] == "prepare_program_publication"
    publication = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context(prefix + "-publication"),
            "source_draft_id": draft["draft_id"],
            "source_revision_id": draft["revision_id"],
            "expected_current_version_id": body.get("expected_current_version_id"),
            "publication_reason": "synthetic",
        },
        prefix + "-publication",
    )["data"]
    p = preview(publication, prefix + "-publication")
    assert p["confirmation_kind"] == "publication"
    # Message IDs are opaque correlation identities.  Their equality cannot
    # prove or disprove real chat chronology; the server-enforced boundary is
    # the persisted /shown operation before /confirm for this exact preview.
    committed = post(
        f"/api/v1/commands/previews/{p['preview_id']}/confirm",
        confirm_body(p, p["shown_message_id"]),
        prefix + "-confirm-after-shown",
    )["data"]
    return draft, p, committed


def main() -> None:
    engine = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    knowledge_id, knowledge_version, protocol_id, status_id, scope_id = (uuid4() for _ in range(5))
    with engine.begin() as db:
        profile = (
            db.execute(
                text(
                    "SELECT profile_id,current_revision_id FROM trainer.profiles WHERE archived_at IS NULL"
                )
            )
            .mappings()
            .one()
        )
        goal = (
            db.execute(
                text(
                    "SELECT g.goal_id,g.current_revision_id,r.goal_type FROM trainer.goals g JOIN trainer.goal_revisions r ON r.goal_revision_id=g.current_revision_id WHERE r.status='active' ORDER BY g.goal_id LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
        confirmation = db.execute(
            text(
                "SELECT confirmation_id FROM trainer.profile_revisions WHERE profile_revision_id=:id"
            ),
            {"id": profile["current_revision_id"]},
        ).scalar_one()
        goal_digest = digest_payload(
            db.execute(
                text("SELECT to_jsonb(r) FROM trainer.goal_revisions r WHERE goal_revision_id=:id"),
                {"id": goal["current_revision_id"]},
            ).scalar_one()
        )
        exercise = (
            db.execute(
                text(
                    "SELECT e.exercise_id,e.current_revision_id FROM trainer.exercises e JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id WHERE r.status_code='active' AND r.canonical_name='Жим лёжа' AND NOT r.unilateral AND EXISTS(SELECT 1 FROM trainer.exercise_measurements m WHERE m.exercise_revision_id=r.exercise_revision_id AND m.metric_kind='repetitions')"
                )
            )
            .mappings()
            .one()
        )
        db.execute(
            text(
                "INSERT INTO trainer.conclusions(conclusion_id,topic,current_version_id,created_at) VALUES (:id,'synthetic',:version,now())"
            ),
            {"id": knowledge_id, "version": knowledge_version},
        )
        db.execute(
            text(
                "INSERT INTO trainer.conclusion_versions(conclusion_version_id,conclusion_id,version_no,statement,population_conditions,confidence,review_due_on,content_digest,created_at) VALUES (:id,:parent,1,'synthetic, not a scientific claim','all','low','2099-12-31',:digest,now())"
            ),
            {"id": knowledge_version, "parent": knowledge_id, "digest": "0" * 64},
        )
        db.execute(
            text(
                "INSERT INTO trainer.applicability_scopes(scope_id,conclusion_version_id,population,goal,inclusion,exclusion,readable_form,created_at) VALUES (:id,:version,'all',:goal,'{}','{}','synthetic unrestricted fixture',now())"
            ),
            {"id": scope_id, "version": knowledge_version, "goal": goal["goal_type"]},
        )
        db.execute(
            text(
                "INSERT INTO trainer.verification_protocol_runs(protocol_run_id,conclusion_version_id,reviewer_profile_id,protocol_version,checklist_result,decision,reason,performed_at,confirmation_id,created_at) VALUES (:id,:version,:profile,'synthetic','{}','verified','synthetic only',now(),:confirmation,now())"
            ),
            {
                "id": protocol_id,
                "version": knowledge_version,
                "profile": profile["profile_id"],
                "confirmation": confirmation,
            },
        )
        db.execute(
            text(
                "INSERT INTO trainer.knowledge_status_revisions(status_revision_id,conclusion_version_id,revision_no,status,effective_at,reason,protocol_run_id,created_at) VALUES (:id,:version,1,'verified',now()-interval '1 day','synthetic',:protocol,now())"
            ),
            {"id": status_id, "version": knowledge_version, "protocol": protocol_id},
        )
    version = {
        "version_label": "synthetic v1",
        "rationale": "synthetic rationale",
        "period_start": datetime.now(UTC).date().isoformat(),
        "period_end": (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
        "templates": [
            {
                "position": 1,
                "day_type_code": "training",
                "purpose": "synthetic training",
                "blocks": [
                    {
                        "position": 1,
                        "block_type_code": "straight",
                        "exercises": [
                            {
                                "position": 1,
                                "exercise_ref": {
                                    "object_type": "exercise",
                                    "object_id": str(exercise["exercise_id"]),
                                    "revision_id": str(exercise["current_revision_id"]),
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
        "recommendations": [
            {
                "recommendation_kind": "program_design",
                "decision_text": "Synthetic decision",
                "confidence": "low",
                "revisit_condition": "synthetic revisit",
                "fact_links": [
                    {
                        "ref": {
                            "object_type": "goal",
                            "object_id": str(goal["goal_id"]),
                            "revision_id": str(goal["current_revision_id"]),
                        },
                        "role": "baseline",
                        "reason": "synthetic dated goal",
                        "snapshot_digest": goal_digest,
                    }
                ],
                "calculation_links": [],
                "conclusion_links": [
                    {"conclusion_version_id": str(knowledge_version), "role": "supports"}
                ],
                "assumptions": {},
                "alternatives": {},
                "short_explanation": "synthetic short",
                "detailed_explanation": "synthetic detailed",
            }
        ],
    }
    body = {
        "intent": "create",
        "goal_revision_id": str(goal["current_revision_id"]),
        "program": {
            "name": "Synthetic program",
            "starts_on": "2026-09-01",
            "ends_on": "2026-12-31",
            "status_code": "active",
        },
        "version": version,
    }
    # Every current item is a dependency, even unused inventory-only/unknown cards.
    inventory_card = {
        "equipment_type": "exercise_mat",
        "name": "Synthetic unused mat",
        "maker": None,
        "brand": None,
        "model": None,
        "quantity": None,
        "quantity_unit_code": None,
        "condition": None,
        "comment": None,
        "characteristics": {
            "mass_per_item_kg": None,
            "bar_sleeve_diameter_mm": None,
            "plate_bore_diameter_mm": None,
        },
    }
    unused_draft = post(
        "/api/v1/commands/inventory-change-drafts",
        {
            "context": context("unused-inventory-create"),
            "intent": "create",
            "equipment": inventory_card,
        },
        "unused-inventory-create",
    )["data"]
    unused_preview = preview(unused_draft, "unused-inventory-create")
    unused_commit = post(
        f"/api/v1/commands/previews/{unused_preview['preview_id']}/confirm",
        confirm_body(unused_preview, "unused-inventory-confirm"),
        "unused-inventory-confirm",
    )["data"]
    unused_ref = unused_commit["created_revisions"][0]
    availability_draft = post(
        "/api/v1/commands/program-drafts",
        {"context": context("availability-draft"), **deepcopy(body)},
        "availability-draft",
    )["data"]
    availability_refs = [
        dependency["object_id"]
        for dependency in availability_draft["dependencies"]
        if dependency["dependency_type"] == "equipment_item"
    ]
    assert unused_ref["object_id"] in availability_refs, availability_draft["dependencies"]
    unused_correction = post(
        "/api/v1/commands/inventory-change-drafts",
        {
            "context": context("unused-inventory-correct"),
            "intent": "correct",
            "equipment_id": unused_ref["object_id"],
            "expected_current_revision_id": unused_ref["revision_id"],
            "equipment": {**inventory_card, "quantity": 2, "quantity_unit_code": "piece"},
            "reason": "Synthetic owner correction of unused inventory",
        },
        "unused-inventory-correct",
    )["data"]
    correction_preview = preview(unused_correction, "unused-inventory-correct")
    post(
        f"/api/v1/commands/previews/{correction_preview['preview_id']}/confirm",
        confirm_body(correction_preview, "unused-inventory-correct-confirm"),
        "unused-inventory-correct-confirm",
    )
    stale_availability = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context("availability-stale"),
            "source_draft_id": availability_draft["draft_id"],
            "source_revision_id": availability_draft["revision_id"],
        },
        "availability-stale",
        409,
    )
    assert stale_availability["error"]["code"] == "stale_dependency"
    membership_draft = post(
        "/api/v1/commands/program-drafts",
        {"context": context("membership-draft"), **deepcopy(body)},
        "membership-draft",
    )["data"]
    added_draft = post(
        "/api/v1/commands/inventory-change-drafts",
        {
            "context": context("membership-new-card"),
            "intent": "create",
            "equipment": {
                **inventory_card,
                "equipment_type": "sportswear",
                "name": "Synthetic unused sportswear",
            },
        },
        "membership-new-card",
    )["data"]
    added_preview = preview(added_draft, "membership-new-card")
    post(
        f"/api/v1/commands/previews/{added_preview['preview_id']}/confirm",
        confirm_body(added_preview, "membership-new-card-confirm"),
        "membership-new-card-confirm",
    )
    membership_stale = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context("membership-stale"),
            "source_draft_id": membership_draft["draft_id"],
            "source_revision_id": membership_draft["revision_id"],
        },
        "membership-stale",
        409,
    )
    assert membership_stale["error"]["code"] == "stale_dependency"
    revision_draft = post(
        "/api/v1/commands/program-drafts",
        {"context": context("revision-source"), **deepcopy(body)},
        "revision-source",
    )["data"]
    old_publication = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context("revision-old-publication"),
            "source_draft_id": revision_draft["draft_id"],
            "source_revision_id": revision_draft["revision_id"],
        },
        "revision-old-publication",
    )["data"]
    old_preview = preview(old_publication, "revision-old")
    revision_body = {
        "context": context("revision-replace"),
        "base_revision_id": revision_draft["revision_id"],
        "changes": [{"op": "replace", "path": "/version", "value": deepcopy(version)}],
    }
    revision_body["changes"][0]["value"]["version_label"] = "replacement draft"
    revision_path = f"/api/v1/commands/drafts/{revision_draft['draft_id']}/revisions"
    revised = post(revision_path, deepcopy(revision_body), "revision-replace")["data"]
    assert revised["draft_id"] == revision_draft["draft_id"] and revised["revision_no"] == 2
    assert revised == post(revision_path, deepcopy(revision_body), "revision-replace")["data"]
    assert (
        post(revision_path, deepcopy(revision_body), "revision-old-base", 409)["error"]["code"]
        == "revision_conflict"
    )
    assert (
        post(
            f"/api/v1/commands/previews/{old_preview['preview_id']}/confirm",
            confirm_body(old_preview, "revision-old-confirm"),
            "revision-old-confirm",
            409,
        )["error"]["code"]
        == "already_superseded"
    )
    replacement_publication = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context("revision-new-publication"),
            "source_draft_id": revised["draft_id"],
            "source_revision_id": revised["revision_id"],
        },
        "revision-new-publication",
    )["data"]
    replacement_preview = preview(replacement_publication, "revision-new")
    post(
        f"/api/v1/commands/previews/{replacement_preview['preview_id']}/confirm",
        confirm_body(replacement_preview, "revision-new-confirm"),
        "revision-new-confirm",
    )
    _, p, commit = publish(body, "program-v1")
    program_ref = next(r for r in commit["domain_changes"] if r["object_type"] == "program")
    version_ref = next(
        r for r in commit["created_revisions"] if r["object_type"] == "program_version"
    )
    query = {
        "context": context("read-v1"),
        "mode": "current",
        "program_id": program_ref["object_id"],
    }
    first = post("/api/v1/queries/programs/get", query)["data"]
    ProgramResponse.model_validate(first)
    assert first["current"] and first["version"]["publication_state"] == "published"
    assert first["version"]["recommendations"][0]["short_explanation"] == "synthetic short"
    assert first["version"]["recommendations"][0]["conclusion_links"][0]["status_revision_ref"][
        "object_id"
    ] == str(status_id)
    assert first["version"]["templates"][0]["blocks"][0]["exercises"][0]["exercise_ref"][
        "revision_id"
    ] == str(exercise["current_revision_id"])
    new_body = {
        "intent": "new_version",
        "program_id": program_ref["object_id"],
        "expected_current_version_id": version_ref["object_id"],
        "goal_revision_id": str(goal["current_revision_id"]),
        "version": deepcopy(version),
    }
    new_body["version"]["version_label"] = "synthetic v2"
    _, _, second_commit = publish(new_body, "program-v2")
    second = post("/api/v1/queries/programs/get", query)["data"]
    assert second["version"]["version_no"] == 2
    old = post(
        "/api/v1/queries/programs/get",
        {
            "context": context("exact-old"),
            "mode": "exact",
            "program_id": program_ref["object_id"],
            "program_version_id": version_ref["object_id"],
        },
    )["data"]
    assert not old["current"] and old["version"]["publication_state"] == "retired"
    assert (
        old["version"]["templates"] == first["version"]["templates"]
        and old["version"]["content_digest"] == first["version"]["content_digest"]
    )
    assert second["version"]["supersedes_version_id"] == version_ref["object_id"]
    wrong = post(
        "/api/v1/queries/programs/get",
        {
            "context": context("wrong"),
            "mode": "exact",
            "program_id": program_ref["object_id"],
            "program_version_id": str(uuid4()),
        },
        expected=404,
    )
    assert wrong["error"]["code"] == "not_found"
    # Exact version must belong to the requested Program; a persisted version of
    # another Program is not a valid cross-owner read and cannot mutate anything.
    with engine.begin() as db:
        other = (
            db.execute(
                text(
                    "SELECT program_id,current_version_id FROM trainer.programs "
                    "WHERE program_id<>:program ORDER BY program_id LIMIT 1"
                ),
                {"program": program_ref["object_id"]},
            )
            .mappings()
            .one()
        )
        before_program_rows = db.execute(
            text("SELECT count(*) FROM trainer.program_versions")
        ).scalar_one()
    cross_program = post(
        "/api/v1/queries/programs/get",
        {
            "context": context("wrong-other-program"),
            "mode": "exact",
            "program_id": program_ref["object_id"],
            "program_version_id": str(other["current_version_id"]),
        },
        expected=404,
    )
    assert cross_program["error"]["code"] == "not_found"
    with engine.begin() as db:
        assert (
            db.execute(text("SELECT count(*) FROM trainer.program_versions")).scalar_one()
            == before_program_rows
        )
    for index, change in enumerate(
        (
            lambda b: b["version"]["templates"][0].update(planned_date="2026-09-17"),
            lambda b: b["version"]["templates"][0]["blocks"][0].update(declared_rounds=2),
            lambda b: b["version"]["templates"][0]["blocks"][0]["exercises"][0]["sets"][0][
                "metrics"
            ][0].update(unit_code="second"),
        )
    ):
        invalid = ProgramDraftRequest.model_validate(
            {"context": context(f"invalid-{index}"), **deepcopy(body), "request_digest": "0" * 64}
        ).model_dump(mode="json")
        change(invalid)
        assert (
            post(
                "/api/v1/commands/program-drafts", invalid, f"invalid-{index}", 400, normalize=False
            )["error"]["code"]
            == "validation_error"
        )
    missing = deepcopy(body)
    missing["version"]["recommendations"] = []
    incomplete = post(
        "/api/v1/commands/program-drafts",
        {"context": context("missing-support"), **missing},
        "missing-support",
    )["data"]
    assert incomplete["gaps"] and not incomplete["preview_candidate"]["complete"]
    assert (
        post(
            "/api/v1/commands/program-publication-drafts",
            {
                "context": context("blocked-publication"),
                "source_draft_id": incomplete["draft_id"],
                "source_revision_id": incomplete["revision_id"],
            },
            "blocked-publication",
            400,
        )["error"]["code"]
        == "validation_error"
    )
    # Concurrent idempotent publication and a fault before commit both use separate synthetic programs.
    third = post(
        "/api/v1/commands/program-drafts",
        {"context": context("third-draft"), **body},
        "third-draft",
    )["data"]
    third_publication = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context("third-publication"),
            "source_draft_id": third["draft_id"],
            "source_revision_id": third["revision_id"],
        },
        "third-publication",
    )["data"]
    third_preview = preview(third_publication, "third")
    with Session(engine) as session:
        before = session.execute(text("SELECT count(*) FROM trainer.programs")).scalar_one()
        fault = ConfirmRequest.model_validate(
            {**confirm_body(third_preview, "fault-confirm"), "request_digest": "0" * 64}
        ).model_dump(mode="json")
        try:
            ContextRepository(session).confirm(
                preview_id=UUID(third_preview["preview_id"]),
                payload=fault,
                key="synthetic-fault",
                correlation_id=uuid4(),
                fail_before_commit=True,
            )
        except RuntimeError as error:
            assert "synthetic fault" in str(error)
            session.rollback()
        else:
            raise AssertionError("fault did not roll back")
        assert session.execute(text("SELECT count(*) FROM trainer.programs")).scalar_one() == before
    concurrent_body = confirm_body(third_preview, "third-confirm")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: post(
                    f"/api/v1/commands/previews/{third_preview['preview_id']}/confirm",
                    deepcopy(concurrent_body),
                    "third-confirm",
                )["data"],
                range(2),
            )
        )
    assert {r["status"] for r in results} == {"committed", "already_committed"}
    assert len({r["commit_result_id"] for r in results}) == 1
    with engine.connect() as db:
        for table in (
            "programs",
            "program_versions",
            "day_templates",
            "template_blocks",
            "template_exercises",
            "template_sets",
            "planned_metric_values",
            "recommendations",
            "recommendation_explanations",
        ):
            assert db.execute(
                text(
                    "SELECT has_table_privilege('trainer_app',:table,'DELETE'),has_table_privilege('trainer_app',:table,'TRUNCATE')"
                ),
                {"table": "trainer." + table},
            ).one() == (False, False)
    for statement in (
        "UPDATE trainer.programs SET name='illegal' WHERE program_id=:id",
        "UPDATE trainer.program_versions SET rationale='illegal' WHERE program_id=:id",
        "UPDATE trainer.program_versions SET publication_state='published' WHERE program_id=:id AND publication_state='retired'",
        "INSERT INTO trainer.day_templates(day_template_id,program_version_id,position,day_type,purpose,created_at) VALUES (:new,:version,99,'training','illegal extension',now())",
    ):
        try:
            with engine.begin() as db:
                db.execute(
                    text(statement),
                    {
                        "id": program_ref["object_id"],
                        "new": uuid4(),
                        "version": second["version"]["version_ref"]["object_id"],
                    },
                )
        except DBAPIError:
            pass
        else:
            raise AssertionError("immutable program violation accepted")
    # Retiring changes the version row xmin, but cannot reopen its immutable tree.
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "UPDATE trainer.program_versions SET publication_state='retired' WHERE program_version_id=:version"
                ),
                {"version": second["version"]["version_ref"]["object_id"]},
            )
            db.execute(
                text(
                    "INSERT INTO trainer.day_templates(day_template_id,program_version_id,position,day_type,purpose,created_at) VALUES (:new,:version,99,'training','illegal retire extension',now())"
                ),
                {"new": uuid4(), "version": second["version"]["version_ref"]["object_id"]},
            )
            raise AssertionError("retiring reopened immutable content")
    except DBAPIError as error:
        assert "cannot be extended" in str(error)
    # Freeze two candidates against v2; publishing one invalidates the other's preview.
    current_id = second["version"]["version_ref"]["object_id"]
    candidate = {**deepcopy(new_body), "expected_current_version_id": current_id}
    candidate["version"]["version_label"] = "synthetic v3"
    stale_draft = post(
        "/api/v1/commands/program-drafts",
        {"context": context("stale-draft"), **deepcopy(candidate)},
        "stale-draft",
    )["data"]
    stale_publication = post(
        "/api/v1/commands/program-publication-drafts",
        {
            "context": context("stale-publication"),
            "source_draft_id": stale_draft["draft_id"],
            "source_revision_id": stale_draft["revision_id"],
            "expected_current_version_id": current_id,
        },
        "stale-publication",
    )["data"]
    stale_preview = preview(stale_publication, "stale")
    publish(candidate, "program-v3")
    assert (
        post(
            f"/api/v1/commands/previews/{stale_preview['preview_id']}/confirm",
            confirm_body(stale_preview, "stale-confirm"),
            "stale-confirm",
            409,
        )["error"]["code"]
        == "stale_preview"
    )
    # Positive time/distance templates use exact revisions and the closed unit mapping.
    for kind, unit in (("duration", "second"), ("distance", "meter")):
        from exercise_flow import draft_and_confirm
        from exercise_flow import exercise as exercise_fixture

        metric_fixture = exercise_fixture("Synthetic program " + kind, alias="program " + kind)
        metric_fixture["measurements"] = [
            {
                "metric_kind": kind,
                "requiredness": "required",
                "unit_family": "time" if kind == "duration" else "length",
            }
        ]
        draft_and_confirm(
            {
                "intent": "create",
                "exercise": metric_fixture,
                "expected_revision_id": None,
                "reason": "synthetic program metric fixture",
            },
            "program-metric-" + kind,
        )
        with engine.connect() as db:
            metric_ex = (
                db.execute(
                    text(
                        "SELECT e.exercise_id,e.current_revision_id FROM trainer.exercises e JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id JOIN trainer.exercise_measurements m ON m.exercise_revision_id=r.exercise_revision_id WHERE m.metric_kind=:kind AND r.status_code='active' AND NOT r.unilateral AND (SELECT count(*) FROM trainer.exercise_measurements mx WHERE mx.exercise_revision_id=r.exercise_revision_id)=1 LIMIT 1"
                    ),
                    {"kind": kind},
                )
                .mappings()
                .one()
            )
        metric_body = deepcopy(body)
        ex = metric_body["version"]["templates"][0]["blocks"][0]["exercises"][0]
        ex["exercise_ref"] = {
            "object_type": "exercise",
            "object_id": str(metric_ex["exercise_id"]),
            "revision_id": str(metric_ex["current_revision_id"]),
        }
        ex["sets"][0]["metrics"] = [
            {"metric_kind": kind, "min_value": "30", "max_value": "60", "unit_code": unit}
        ]
        publish(metric_body, "program-" + kind)
    circuit_body = deepcopy(body)
    block = circuit_body["version"]["templates"][0]["blocks"][0]
    block.update(block_type_code="superset", declared_rounds=2)
    block["exercises"].append(deepcopy(block["exercises"][0]))
    for index, ex in enumerate(block["exercises"], 1):
        ex.update(position=index, circuit_position=index)
        ex["sets"][0]["round_no"] = 1
    publish(circuit_body, "program-superset")
    # Damage only the isolated fixture, prove fail-closed read, then restore it exactly.
    admin = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"])
    metric_id = first["version"]["templates"][0]["blocks"][0]["exercises"][0]["sets"][0]["metrics"][
        0
    ]["metric_target_id"]
    with admin.begin() as db:
        row = (
            db.execute(
                text(
                    "SELECT * FROM trainer.planned_metric_values WHERE planned_metric_value_id=:id"
                ),
                {"id": metric_id},
            )
            .mappings()
            .one()
        )
        saved = dict(row)
        db.execute(
            text("DELETE FROM trainer.planned_metric_values WHERE planned_metric_value_id=:id"),
            {"id": metric_id},
        )
    error = post(
        "/api/v1/queries/programs/get",
        {
            "context": context("damaged-read"),
            "mode": "exact",
            "program_id": program_ref["object_id"],
            "program_version_id": version_ref["object_id"],
        },
        expected=500,
    )["error"]
    assert (
        error["code"] == "inconsistent_domain_state"
        and not error["retryable"]
        and error["required_next_action"] == "run_integrity_check"
    )
    with admin.begin() as db:
        db.execute(text("SET LOCAL session_replication_role=replica"))
        db.execute(
            text(
                "INSERT INTO trainer.planned_metric_values(planned_metric_value_id,template_set_id,planned_set_id,metric_kind,exact_value,min_value,max_value,unit_code,custom_definition,created_at) VALUES (:planned_metric_value_id,:template_set_id,:planned_set_id,:metric_kind,:exact_value,:min_value,:max_value,:unit_code,CAST(:custom_definition AS jsonb),:created_at)"
            ),
            {
                **saved,
                "custom_definition": json.dumps(saved["custom_definition"])
                if saved["custom_definition"] is not None
                else None,
            },
        )
    assert (
        post(
            "/api/v1/queries/programs/get",
            {
                "context": context("restored"),
                "mode": "exact",
                "program_id": program_ref["object_id"],
                "program_version_id": version_ref["object_id"],
            },
        )["data"]["version"]["content_digest"]
        == first["version"]["content_digest"]
    )
    # Later knowledge status invalidates new design; historical publications keep exact status.
    # Address-only fixture pointer changes prove both exact goal and exercise invalidation.
    for dependency, identity_id, original_revision in (
        ("goal", goal["goal_id"], goal["current_revision_id"]),
        ("exercise", exercise["exercise_id"], exercise["current_revision_id"]),
    ):
        prefix = "stale-" + dependency
        dependency_draft = post(
            "/api/v1/commands/program-drafts",
            {"context": context(prefix + "-draft"), **deepcopy(body)},
            prefix + "-draft",
        )["data"]
        dependency_publication = post(
            "/api/v1/commands/program-publication-drafts",
            {
                "context": context(prefix + "-publication"),
                "source_draft_id": dependency_draft["draft_id"],
                "source_revision_id": dependency_draft["revision_id"],
            },
            prefix + "-publication",
        )["data"]
        dependency_preview = preview(dependency_publication, prefix)
        if dependency == "goal":
            from context_flow import confirm_preview, goal_payload, preview_and_show
            from context_flow import post as context_post

            at = datetime.now(UTC).isoformat()
            corrected_draft = context_post(
                "/api/v1/commands/goal-drafts",
                {
                    "context": context(prefix + "-correct"),
                    "intent": "correct",
                    "goal": goal_payload(
                        goal_id=str(identity_id), name="Synthetic stale dependency"
                    ),
                    "expected_revision_id": str(original_revision),
                    "reason": "synthetic stale check",
                },
                key=prefix + "-correct",
            )["data"]
            confirm_preview(
                preview_and_show(corrected_draft, at=at, prefix=prefix + "-correction"),
                corrected_draft,
                at=at,
                prefix=prefix + "-correction",
            )
        else:
            draft_and_confirm(
                {
                    "intent": "correct",
                    "exercise": exercise_fixture(
                        "Synthetic changed dependency",
                        exercise_id=str(identity_id),
                        alias="changed dependency",
                    ),
                    "expected_revision_id": str(original_revision),
                    "reason": "synthetic stale check",
                },
                prefix + "-correction",
            )
        try:
            assert (
                post(
                    f"/api/v1/commands/previews/{dependency_preview['preview_id']}/confirm",
                    confirm_body(dependency_preview, prefix + "-confirm"),
                    prefix + "-confirm",
                    409,
                )["error"]["code"]
                == "stale_preview"
            )
        finally:
            with admin.begin() as db:
                if dependency == "exercise":
                    db.execute(
                        text(
                            "UPDATE trainer.exercises SET active_normalized_name=(SELECT normalized_name FROM trainer.exercise_revisions WHERE exercise_revision_id=:revision) WHERE exercise_id=:id"
                        ),
                        {"id": identity_id, "revision": original_revision},
                    )
                db.execute(
                    text(
                        f"UPDATE trainer.{dependency}s SET current_revision_id=:revision WHERE {dependency}_id=:id"
                    ),
                    {"id": identity_id, "revision": original_revision},
                )
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO trainer.knowledge_status_revisions(status_revision_id,conclusion_version_id,revision_no,status,effective_at,reason,created_at) VALUES (:id,:version,2,'outdated',now(),'synthetic expiry',now())"
            ),
            {"id": uuid4(), "version": knowledge_version},
        )
    unsupported = post(
        "/api/v1/commands/program-drafts",
        {"context": context("outdated-design"), **body},
        "outdated-design",
        400,
    )["error"]
    assert unsupported["code"] == "validation_error"
    historical = post(
        "/api/v1/queries/programs/get",
        {
            "context": context("historical-knowledge"),
            "mode": "exact",
            "program_id": program_ref["object_id"],
            "program_version_id": version_ref["object_id"],
        },
    )["data"]
    assert (
        historical["version"]["recommendations"][0]["conclusion_links"][0]["status_at_decision"]
        == "verified"
    )
    assert second_commit["status"] == "committed"
    print(
        "PASS: D-G04 create/new_version/read-back/exact ownership/immutable history/evidence gaps/technical-preview rejection/server-recorded shown-before-confirm/concurrent idempotency/rollback/DB grants"
    )


if __name__ == "__main__":
    main()
