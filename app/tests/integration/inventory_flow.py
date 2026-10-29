"""Synthetic E-G04 full-card, replay, revision race and rollback checks."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from typing import Any
from uuid import UUID, uuid4

from evidence_fixture import insufficient_recommendations
from plan_flow import post as plan_post
from plan_flow import prepare_confirmation
from program_flow import confirm_body, context, post, preview
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from trainer.infrastructure.db.context_repository import ContextRepository
from trainer.transport.http.contracts import ConfirmRequest

PATH = "/api/v1/commands/inventory-change-drafts"
TABLES = (
    "equipment_items",
    "equipment_item_revisions",
    "confirmations",
    "domain_changes",
    "commit_results",
    "idempotent_operations",
)


def pending_plan(engine: Any, prefix: str) -> tuple[str, dict[str, Any], str]:
    with engine.connect() as db:
        exercise_id = db.execute(
            text("""
            SELECT e.exercise_id FROM trainer.exercises e
            JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id
            WHERE r.canonical_name='Synthetic pull-up' AND r.status_code='active'
            ORDER BY e.created_at LIMIT 1
        """)
        ).scalar_one()
    body = {
        "context": context(prefix + "-draft"),
        "plan": {
            "planned_date": (datetime.now(UTC).date() + timedelta(days=2)).isoformat(),
            "planned_time": None,
            "program_id": None,
            "program_version_id": None,
            "day_template_id": None,
            "plan_type": "training",
            "name": prefix,
            "purpose": "synthetic inventory membership race",
            "comment": None,
            "temporary_conditions": {
                "place": None,
                "equipment_additions": [],
                "equipment_removals": [],
            },
            "blocks": [
                {
                    "position": 1,
                    "block_type_code": "straight",
                    "name": None,
                    "declared_rounds": None,
                    "rest_seconds": None,
                    "comment": None,
                    "exercises": [
                        {
                            "position": 1,
                            "exercise_id": str(exercise_id),
                            "circuit_position": None,
                            "comment": None,
                            "sets": [
                                {
                                    "position": 1,
                                    "round_no": None,
                                    "logical_set_no": 1,
                                    "side": "both",
                                    "set_type_code": "working",
                                    "target_rpe_min": None,
                                    "target_rpe_max": None,
                                    "target_rir_min": None,
                                    "target_rir_max": None,
                                    "tempo": None,
                                    "pause_seconds": None,
                                    "rest_after_seconds": None,
                                    "metrics": [
                                        {
                                            "metric_kind": "repetitions",
                                            "exact_value": "8",
                                            "min_value": None,
                                            "max_value": None,
                                            "unit_code": "repetition",
                                            "custom_definition": None,
                                        }
                                    ],
                                    "loads": [],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "recommendations": insufficient_recommendations(engine),
    }
    draft = plan_post("/api/v1/commands/plan-drafts", body, prefix + "-draft")["data"]
    return prepare_confirmation(draft, prefix)


def create_unknown_card(card: dict[str, Any], prefix: str) -> dict[str, Any]:
    draft = post(
        PATH,
        {
            "context": context(prefix + "-draft"),
            "intent": "create",
            "equipment": {**card, "name": "Synthetic " + prefix},
        },
        prefix + "-draft",
    )["data"]
    shown = preview(draft, prefix)
    return {
        "path": f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
        "body": confirm_body(shown, prefix + "-confirm"),
        "key": prefix + "-confirm",
    }


def main() -> None:
    engine = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    card: dict[str, Any] = {
        "equipment_type": "wrist_wraps",
        "name": "Synthetic E-G04 unknown wraps",
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
    body = {"context": context("eg04-create"), "intent": "create", "equipment": card}
    draft = post(PATH, deepcopy(body), "eg04-create")["data"]
    assert post(PATH, deepcopy(body), "eg04-create")["data"] == draft
    shown = preview(draft, "eg04-create")
    payload = confirm_body(shown, "eg04-create-confirm")
    with ThreadPoolExecutor(max_workers=2) as pool:
        commits = list(
            pool.map(
                lambda _: post(
                    f"/api/v1/commands/previews/{shown['preview_id']}/confirm",
                    deepcopy(payload),
                    "eg04-create-confirm",
                )["data"],
                range(2),
            )
        )
    assert {result["status"] for result in commits} == {"committed", "already_committed"}
    assert len({result["commit_result_id"] for result in commits}) == 1
    ref = commits[0]["created_revisions"][0]
    correction = {
        "intent": "correct",
        "equipment_id": ref["object_id"],
        "expected_current_revision_id": ref["revision_id"],
        "equipment": {
            **card,
            "quantity": 2,
            "quantity_unit_code": "pair",
        },
        "reason": "Synthetic explicit owner correction",
    }
    previews = []
    for index in range(2):
        prefix = f"eg04-correct-{index}"
        candidate = post(PATH, {"context": context(prefix), **deepcopy(correction)}, prefix)["data"]
        previews.append(preview(candidate, prefix))
    first, second = previews
    committed = post(
        f"/api/v1/commands/previews/{first['preview_id']}/confirm",
        confirm_body(first, "eg04-correct-first"),
        "eg04-correct-first",
    )["data"]
    error = post(
        f"/api/v1/commands/previews/{second['preview_id']}/confirm",
        confirm_body(second, "eg04-correct-stale"),
        "eg04-correct-stale",
        409,
    )["error"]
    assert error["code"] == "stale_preview"
    stale = post(PATH, {"context": context("eg04-stale"), **correction}, "eg04-stale", 409)
    assert stale["error"]["code"] == "stale_dependency"
    assert stale["error"]["required_next_action"] == "read_current_profile_context"
    current = post(
        "/api/v1/queries/profile-context",
        {
            "context": context("eg04-read"),
            "purpose": "synthetic acceptance",
        },
    )["data"]
    item = next(
        item for item in current["equipment"] if item["ref"]["object_id"] == ref["object_id"]
    )
    assert item == {"ref": committed["created_revisions"][0], "equipment": correction["equipment"]}

    fault_draft = post(
        PATH,
        {
            "context": context("eg04-fault-draft"),
            "intent": "create",
            "equipment": card,
        },
        "eg04-fault-draft",
    )["data"]
    fault_preview = preview(fault_draft, "eg04-fault")
    fault_payload = ConfirmRequest.model_validate(
        {
            **confirm_body(fault_preview, "eg04-fault-confirm"),
            "request_digest": "0" * 64,
        }
    ).model_dump(mode="json")
    with Session(engine) as session:
        before = {
            table: session.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            for table in TABLES
        }
        try:
            ContextRepository(session).confirm(
                preview_id=UUID(fault_preview["preview_id"]),
                payload=fault_payload,
                key="eg04-fault-confirm",
                correlation_id=uuid4(),
                fail_before_commit=True,
            )
        except RuntimeError as error:
            assert "synthetic fault" in str(error)
            session.rollback()
        else:
            raise AssertionError("synthetic inventory fault did not abort")
        after = {
            table: session.execute(text(f"SELECT count(*) FROM trainer.{table}")).scalar_one()
            for table in TABLES
        }
        assert before == after
    post(
        f"/api/v1/commands/previews/{fault_preview['preview_id']}/confirm",
        confirm_body(fault_preview, "eg04-fault-retry"),
        "eg04-fault-retry",
    )
    with engine.connect() as db:
        plan_count = db.execute(text("SELECT count(*) FROM trainer.workout_plans")).scalar_one()
    # Membership changes invalidate a shown plan even when the new item is unused.
    plan_path, plan_body, plan_key = pending_plan(engine, "eg04-plan-membership")
    first_membership = create_unknown_card(card, "eg04-unused-membership")
    post(first_membership["path"], first_membership["body"], first_membership["key"])
    stale_plan = plan_post(
        f"/api/v1/commands/previews/{plan_path}/confirm",
        plan_body,
        plan_key,
        409,
    )
    assert stale_plan["error"]["code"] == "stale_preview"
    with engine.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM trainer.workout_plans")).scalar_one()
            == plan_count
        )

    # Queue both commits behind the exact profile row. FIFO lock acquisition
    # forces the inventory mutation ahead of the plan's full-membership recheck.
    race_path, race_body, race_key = pending_plan(engine, "eg04-plan-race")
    race_membership = create_unknown_card(card, "eg04-race-membership")
    observer = create_engine(os.environ["TRAINER_TEST_ADMIN_DSN"], isolation_level="AUTOCOMMIT")
    with engine.connect() as locker:
        profile_id = locker.execute(
            text("""
            SELECT profile_id FROM trainer.profiles WHERE archived_at IS NULL FOR UPDATE
        """)
        ).scalar_one()

        def blocked_commits() -> int:
            with observer.connect() as probe:
                return int(
                    probe.execute(
                        text("""
                SELECT count(*) FROM pg_stat_activity
                WHERE pid <> pg_backend_pid() AND wait_event_type='Lock'
                  AND (query LIKE '%trainer.profiles%' OR query LIKE '%pg_advisory_xact_lock%')
            """)
                    ).scalar_one()
                )

        def wait_for_blocked(expected: int) -> None:
            deadline = monotonic() + 10
            while monotonic() < deadline and blocked_commits() < expected:
                sleep(0.05)
            assert blocked_commits() >= expected, (profile_id, expected)

        with ThreadPoolExecutor(max_workers=2) as pool:
            inventory_future = pool.submit(
                post,
                race_membership["path"],
                race_membership["body"],
                race_membership["key"],
            )
            try:
                wait_for_blocked(1)
                plan_future = pool.submit(
                    plan_post,
                    f"/api/v1/commands/previews/{race_path}/confirm",
                    race_body,
                    race_key,
                    409,
                )
                wait_for_blocked(2)
                assert not inventory_future.done() and not plan_future.done()
            finally:
                locker.rollback()
            assert inventory_future.result(timeout=20)["data"]["status"] == "committed"
            assert plan_future.result(timeout=20)["error"]["code"] == "stale_preview"
    observer.dispose()
    with engine.connect() as db:
        assert (
            db.execute(text("SELECT count(*) FROM trainer.workout_plans")).scalar_one()
            == plan_count
        )
    print(
        "PASS: E-G04 full card, idempotency, stale revision, rollback/retry, "
        "membership and locked phantom race"
    )


if __name__ == "__main__":
    main()
