# ruff: noqa: E501
"""Exact fact/calculation invalidation and historical E-G07 explanation probe."""

from __future__ import annotations

import os

from evidence_fixture import insufficient_recommendations, synthetic_plan
from evidence_flow import ENGINE, commit, context, explanation, post, prepare, read_plan
from sqlalchemy import create_engine, text

from trainer.domain.context import digest_payload


def main() -> None:
    admin = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"])
    with ENGINE.connect() as db:
        calculation = db.execute(
            text(
                "SELECT derived_value_id FROM trainer.derived_values WHERE state='current' "
                "AND profile_id=(SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL) "
                "ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar_one()
        goal = db.execute(
            text(
                "SELECT to_jsonb(r) FROM trainer.goal_revisions r JOIN trainer.goals g "
                "ON g.current_revision_id=r.goal_revision_id WHERE g.archived_at IS NULL "
                "AND EXISTS(SELECT 1 FROM trainer.goal_revisions old WHERE old.goal_id=g.goal_id "
                "AND old.goal_revision_id<>g.current_revision_id) ORDER BY g.goal_id LIMIT 1"
            )
        ).scalar_one()
        previous = db.execute(
            text(
                "SELECT goal_revision_id FROM trainer.goal_revisions WHERE goal_id=:goal "
                "AND goal_revision_id<>:revision ORDER BY revision_no DESC LIMIT 1"
            ),
            {"goal": goal["goal_id"], "revision": goal["goal_revision_id"]},
        ).scalar_one()
    recs = insufficient_recommendations(ENGINE)
    recs[0]["calculation_links"] = [{"derived_value_id": str(calculation), "role": "input"}]
    recs[0]["fact_links"].append(
        {
            "ref": {
                "object_type": "goal",
                "object_id": goal["goal_id"],
                "revision_id": goal["goal_revision_id"],
            },
            "role": "baseline",
            "reason": "Exact dated synthetic goal",
            "snapshot_digest": digest_payload(goal),
        }
    )

    def draft(prefix: str) -> dict:
        return post(
            "/api/v1/commands/plan-drafts",
            {
                "context": context(prefix),
                "plan": synthetic_plan(ENGINE, prefix),
                "recommendations": recs,
            },
            key=prefix,
        )["data"]

    saved = read_plan(commit(draft("eg07-dependencies-saved"), "eg07-dependencies-saved"))
    recommendation = saved["recommendation_ids"][0]
    before = explanation(recommendation)
    assert before["derived_value_ids"] == [str(calculation)]
    calc_preview, calc_confirmation = prepare(draft("eg07-calc-stale"), "eg07-calc-stale")
    with admin.begin() as db:
        db.execute(
            text(
                "UPDATE trainer.derived_values SET state='stale',stale_reason='synthetic E-G07 input invalidation' WHERE derived_value_id=:id"
            ),
            {"id": calculation},
        )
    post(
        f"/api/v1/commands/previews/{calc_preview}/confirm",
        calc_confirmation,
        key="eg07-calc-stale-confirm",
        expected=409,
    )
    after = explanation(recommendation)
    assert after["derived_value_ids"] == before["derived_value_ids"]
    assert any(r["object_id"] == str(calculation) for r in after["stale_markers"])
    # Restore only this disposable fixture calculation for the independent fact probe.
    with admin.begin() as db:
        db.execute(
            text(
                "UPDATE trainer.derived_values SET state='current',stale_reason=NULL WHERE derived_value_id=:id"
            ),
            {"id": calculation},
        )
    fact_preview, fact_confirmation = prepare(draft("eg07-fact-stale"), "eg07-fact-stale")
    with admin.begin() as db:
        db.execute(
            text("UPDATE trainer.goals SET current_revision_id=:revision WHERE goal_id=:goal"),
            {"revision": previous, "goal": goal["goal_id"]},
        )
    try:
        post(
            f"/api/v1/commands/previews/{fact_preview}/confirm",
            fact_confirmation,
            key="eg07-fact-stale-confirm",
            expected=409,
        )
        changed = explanation(recommendation)
        assert changed["fact_refs"] == before["fact_refs"]
        assert any(r["object_id"] == goal["goal_id"] for r in changed["stale_markers"])
    finally:
        with admin.begin() as db:
            db.execute(
                text("UPDATE trainer.goals SET current_revision_id=:revision WHERE goal_id=:goal"),
                {"revision": goal["goal_revision_id"], "goal": goal["goal_id"]},
            )
    admin.dispose()
    print(
        "PASS: E-G07 exact fact/calculation preview invalidation and immutable explanation/stale markers"
    )


if __name__ == "__main__":
    main()
