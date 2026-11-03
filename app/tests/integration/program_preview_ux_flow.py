"""Synthetic program-preview/read-back gate; run only on an isolated database."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, text

from program_flow import confirm_body, context, post, preview
from trainer.domain.context import digest_payload


def main() -> None:
    import os

    engine = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    with engine.begin() as db:
        profile = db.execute(text("""
            SELECT profile_id,current_revision_id FROM trainer.profiles
            WHERE archived_at IS NULL LIMIT 1
        """)).mappings().one()
        goal = db.execute(text("""
            SELECT g.goal_id,g.current_revision_id,r.goal_type FROM trainer.goals g
            JOIN trainer.goal_revisions r ON r.goal_revision_id=g.current_revision_id
            WHERE g.archived_at IS NULL AND g.logically_deleted_at IS NULL
            ORDER BY g.goal_id LIMIT 1
        """)).mappings().one()
        goal_digest = digest_payload(db.execute(
            text("SELECT to_jsonb(r) FROM trainer.goal_revisions r WHERE goal_revision_id=:id"),
            {"id": goal["current_revision_id"]},
        ).scalar_one())
        confirmation = db.execute(text("""
            SELECT c.confirmation_id FROM trainer.confirmations c
            WHERE NOT EXISTS (SELECT 1 FROM trainer.verification_protocol_runs p
                              WHERE p.confirmation_id=c.confirmation_id)
            ORDER BY c.confirmed_at DESC LIMIT 1
        """)).scalar_one()
        exercise = db.execute(text("""
            SELECT e.exercise_id,e.current_revision_id FROM trainer.exercises e
            JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id
            WHERE r.status_code='active' AND NOT r.unilateral
              AND EXISTS (SELECT 1 FROM trainer.exercise_measurements m
                          WHERE m.exercise_revision_id=r.exercise_revision_id
                            AND m.metric_kind='repetitions')
            ORDER BY e.exercise_id LIMIT 1
        """)).mappings().one()
        knowledge_id, knowledge_version, scope_id, protocol_id, status_id = (uuid4() for _ in range(5))
        db.execute(text("""
            INSERT INTO trainer.conclusions(conclusion_id,topic,current_version_id,created_at)
            VALUES (:id,'synthetic preview support',:version,now())
        """), {"id": knowledge_id, "version": knowledge_version})
        db.execute(text("""
            INSERT INTO trainer.conclusion_versions(conclusion_version_id,conclusion_id,version_no,
                statement,population_conditions,confidence,review_due_on,content_digest,created_at)
            VALUES (:id,:parent,1,'synthetic only','all','low','2099-12-31',:digest,now())
        """), {"id": knowledge_version, "parent": knowledge_id, "digest": "0" * 64})
        db.execute(text("""
            INSERT INTO trainer.applicability_scopes(scope_id,conclusion_version_id,population,
                goal,inclusion,exclusion,readable_form,created_at)
            VALUES (:id,:version,'all',:goal,'{}','{}','synthetic scope',now())
        """), {"id": scope_id, "version": knowledge_version, "goal": goal["goal_type"]})
        db.execute(text("""
            INSERT INTO trainer.verification_protocol_runs(protocol_run_id,conclusion_version_id,
                reviewer_profile_id,protocol_version,checklist_result,decision,reason,performed_at,
                confirmation_id,created_at)
            VALUES (:id,:version,:profile,'synthetic','{}','verified','synthetic only',now(),:confirmation,now())
        """), {"id": protocol_id, "version": knowledge_version,
                "profile": profile["profile_id"], "confirmation": confirmation})
        db.execute(text("""
            INSERT INTO trainer.knowledge_status_revisions(status_revision_id,conclusion_version_id,
                revision_no,status,effective_at,reason,protocol_run_id,created_at)
            VALUES (:id,:version,1,'verified',now()-interval '1 day','synthetic',:protocol,now())
        """), {"id": status_id, "version": knowledge_version, "protocol": protocol_id})
    start = datetime.now(UTC).date()
    end = start + timedelta(days=30)
    stage_end = start + timedelta(days=18)
    exercise_ref = {"object_type": "exercise", "object_id": str(exercise["exercise_id"]),
                    "revision_id": str(exercise["current_revision_id"])}
    sets = [{"position": number, "logical_set_no": number, "side": "both",
             "set_type_code": "working", "metrics": [{"metric_kind": "repetitions",
             "exact_value": "6.000000", "unit_code": "repetition"}]}
            for number in range(1, 4)]
    program = {"name": "Вымышленная программа для проверки обзора", "starts_on": start.isoformat(),
               "ends_on": end.isoformat(), "status_code": "active"}
    version = {"version_label": "Вымышленный этап", "period_start": start.isoformat(),
               "period_end": end.isoformat(),
               "rationale": "Расписание: вторник. Рабочие веса подбирать отдельно; повышать только при готовности.",
               "templates": [{"position": 1, "day_type_code": "training", "weekday_code": "tuesday",
                              "purpose": "Вторник — вымышленная силовая тренировка",
                              "blocks": [{"position": 1, "block_type_code": "straight",
                                          "exercises": [{"position": 1, "exercise_ref": exercise_ref,
                                                         "sets": sets}]}]}],
               "cycle_stages": [{"starts_on": start.isoformat(), "ends_on": stage_end.isoformat(),
                                  "title": "Вымышленная первая неделя",
                                  "description": "Сохранять три подхода по шесть повторений",
                                  "day_rules": [{"day_position": 1,
                                                 "description": "При усталости уменьшить объём"}]}],
               "recommendations": [{
                   "recommendation_kind": "program_design",
                   "decision_text": "Сохранять вымышленный умеренный объём",
                   "confidence": "low", "revisit_condition": "после недели",
                   "fact_links": [{"ref": {"object_type": "goal", "object_id": str(goal["goal_id"]),
                                           "revision_id": str(goal["current_revision_id"])},
                                   "role": "baseline", "reason": "синтетическая цель",
                                   "snapshot_digest": goal_digest}],
                   "calculation_links": [],
                   "conclusion_links": [{"conclusion_version_id": str(knowledge_version),
                                         "role": "supports"}],
                   "assumptions": {"load": "рабочие веса не сообщены"},
                   "alternatives": {},
                   "short_explanation": "вымышленные данные для проверки",
               }]}
    draft = post("/api/v1/commands/program-drafts", {
        "context": context("ux-draft"), "intent": "create",
        "goal_revision_id": str(goal["current_revision_id"]),
        "program": program, "version": version,
    }, "ux-draft")["data"]
    publication = post("/api/v1/commands/program-publication-drafts", {
        "context": context("ux-publication"), "source_draft_id": draft["draft_id"],
        "source_revision_id": draft["revision_id"],
    }, "ux-publication")["data"]
    shown = preview(publication, "ux-publication")
    rendered = shown["canonical_text"]
    assert "Расписание: вторник" in rendered
    assert "3 × 6 повторений" in rendered
    assert "Вымышленная первая неделя" in rendered
    assert "6.000000" not in rendered
    assert "Рабочие веса" in rendered
    with engine.connect() as db:
        preview_count = db.execute(text("SELECT count(*) FROM trainer.previews")).scalar_one()
    for section, selector in (("day", {"day_position": 1}),
                              ("week", {"week_start": start.isoformat()}),
                              ("week", {"week_start": (start + timedelta(days=7)).isoformat()}),
                              ("basis", {})):
        detail = post("/api/v1/queries/program-draft-detail", {
            "context": context("ux-detail-" + section),
            "revision_id": publication["revision_id"], "section": section, **selector,
        })["data"]
        assert detail["revision_id"] == publication["revision_id"]
        assert detail["canonical_text"]
        assert "preview_id" not in detail
        if section == "week":
            assert selector["week_start"] in detail["canonical_text"]
            assert "При усталости уменьшить объём" in detail["canonical_text"]
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM trainer.previews")).scalar_one() == preview_count
    committed = post(f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
                     confirm_body(shown, shown["shown_message_id"]),
                     "ux-confirm")["data"]
    assert committed["status"] == "committed"
    program_ref = next(change for change in committed["domain_changes"]
                       if change["object_type"] == "program")
    saved = post("/api/v1/queries/programs/get", {
        "context": context("ux-readback"), "mode": "current",
        "program_id": program_ref["object_id"],
    })["data"]
    assert saved["version"]["templates"][0]["weekday_code"] == "tuesday"
    assert saved["version"]["cycle_stages"][0]["title"] == "Вымышленная первая неделя"
    assert len(saved["version"]["templates"][0]["blocks"][0]["exercises"][0]["sets"]) == 3
    conflicting = deepcopy(version)
    conflicting["templates"][0]["weekday_code"] = None
    conflicting["templates"][0]["purpose"] = "Вымышленная силовая тренировка"
    bad_program = {**program, "name": "Вымышленная программа с ошибкой расписания"}
    bad_draft = post("/api/v1/commands/program-drafts", {
        "context": context("ux-conflict-draft"), "intent": "create",
        "goal_revision_id": str(goal["current_revision_id"]),
        "program": bad_program, "version": conflicting,
    }, "ux-conflict-draft")["data"]
    bad_publication = post("/api/v1/commands/program-publication-drafts", {
        "context": context("ux-conflict-publication"),
        "source_draft_id": bad_draft["draft_id"],
        "source_revision_id": bad_draft["revision_id"],
    }, "ux-conflict-publication")["data"]
    rejected = post("/api/v1/commands/previews", {
        "context": context("ux-conflict-preview"),
        "revision_id": bad_publication["revision_id"], "detail_level": "full",
    }, "ux-conflict-preview", expected=422)["error"]
    assert rejected["code"] == "validation_error"
    assert rejected["field_errors"][0]["code"] == "schedule_conflict"
    assert rejected["field_errors"][0]["path"] == "version.rationale"
    stale_program = {**program, "name": "Вымышленная программа для проверки устаревания"}
    stale_draft = post("/api/v1/commands/program-drafts", {
        "context": context("ux-stale-draft"), "intent": "create",
        "goal_revision_id": str(goal["current_revision_id"]),
        "program": stale_program, "version": version,
    }, "ux-stale-draft")["data"]
    stale_publication = post("/api/v1/commands/program-publication-drafts", {
        "context": context("ux-stale-publication"),
        "source_draft_id": stale_draft["draft_id"],
        "source_revision_id": stale_draft["revision_id"],
    }, "ux-stale-publication")["data"]
    revised_version = deepcopy(version)
    revised_version["cycle_stages"][0]["description"] = "Изменённый вымышленный объём"
    post(f"/api/v1/commands/drafts/{stale_draft['draft_id']}/revisions", {
        "context": context("ux-stale-revise"),
        "base_revision_id": stale_draft["revision_id"],
        "changes": [{"op": "replace", "path": "/version", "value": revised_version}],
    }, "ux-stale-revise")
    for path, body, key in (
        ("/api/v1/commands/previews", {
            "context": context("ux-stale-preview"),
            "revision_id": stale_publication["revision_id"], "detail_level": "full",
        }, "ux-stale-preview"),
        ("/api/v1/queries/program-draft-detail", {
            "context": context("ux-stale-detail"),
            "revision_id": stale_publication["revision_id"], "section": "day",
            "day_position": 1,
        }, None),
    ):
        error = post(path, body, key, expected=409)["error"]
        assert error["code"] in {"stale_preview", "already_superseded"}, (path, error)
    print("PASS: program weekday/stage preview, exact section reads, publication/read-back, schedule conflict and stale source")


if __name__ == "__main__":
    main()
