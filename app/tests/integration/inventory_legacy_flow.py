"""Read an exact synthetic legacy D plan without rewriting its saved format."""

from __future__ import annotations

import json
import os
from copy import deepcopy

from program_flow import context, post
from sqlalchemy import create_engine, text

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import ContextRepository


def main() -> None:
    admin = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"])
    with admin.connect() as db:
        saved = dict(
            db.execute(
                text("""
            SELECT r.plan_revision_id,p.plan_id,r.context_snapshot,
                   r.context_snapshot_digest,r.content_digest
            FROM trainer.workout_plans p JOIN trainer.workout_plan_revisions r
              ON r.plan_id=p.plan_id
            WHERE r.context_snapshot->>'inventory_snapshot_version'='global_inventory_v2'
            ORDER BY r.created_at LIMIT 1
        """)
            )
            .mappings()
            .one()
        )
    query = {
        "context": context("eg04-legacy-read"),
        "mode": "exact",
        "plan_id": str(saved["plan_id"]),
        "revision_id": str(saved["plan_revision_id"]),
    }
    current = post("/api/v1/queries/plans/get", query)["data"]
    old = deepcopy(dict(saved["context_snapshot"]))
    for key in (
        "inventory_snapshot_version",
        "equipment_inventory_snapshot",
        "equipment_inventory_digest",
    ):
        del old[key]
    old_availability = {"place_id": None, "effective_at": old["effective_at"], "items": []}
    old.update(
        {
            "availability_place_id": None,
            "equipment_availability_snapshot": old_availability,
            "equipment_availability_digest": digest_payload(old_availability),
        }
    )
    old_digest = digest_payload(old)
    old_content = ContextRepository.__new__(ContextRepository)._plan_content_digest(
        current["tree"],
        current["recommendation_ids"],
        old,
    )

    def write_snapshot(snapshot: dict[str, object], digest: str, content: str) -> None:
        with admin.begin() as db:
            db.execute(text("SET LOCAL session_replication_role=replica"))
            db.execute(
                text("""
                UPDATE trainer.workout_plan_revisions
                SET context_snapshot=CAST(:snapshot AS jsonb),
                    context_snapshot_digest=:digest,content_digest=:content
                WHERE plan_revision_id=:revision
            """),
                {
                    "snapshot": json.dumps(snapshot),
                    "digest": digest,
                    "content": content,
                    "revision": saved["plan_revision_id"],
                },
            )

    try:
        write_snapshot(old, old_digest, old_content)
        historical = post("/api/v1/queries/plans/get", query)["data"]
        assert historical["context_snapshot"] == old
        assert historical["context_snapshot_digest"] == old_digest
        assert historical["content_digest"] == old_content
        assert "equipment_inventory_snapshot" not in historical["context_snapshot"]
        corrupted = {**old, "inventory_snapshot_version": "global_inventory_v2"}
        write_snapshot(corrupted, digest_payload(corrupted), old_content)
        invalid = post("/api/v1/queries/plans/get", query, expected=500)["error"]
        assert invalid["code"] == "inconsistent_domain_state"
    finally:
        write_snapshot(
            dict(saved["context_snapshot"]),
            str(saved["context_snapshot_digest"]),
            str(saved["content_digest"]),
        )
    assert post("/api/v1/queries/plans/get", query)["data"] == current
    print("PASS: E-G04 exact legacy plan read, closed mixed-format rejection and restoration")


if __name__ == "__main__":
    main()
