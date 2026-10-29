"""Real PostgreSQL outage and idempotent retry probe for D-G08 TS-04."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import context_flow as flow
from sqlalchemy import create_engine, text

from trainer.transport.http.contracts import GoalDraftRequest

KEY = os.environ["TS04_KEY"]
GOAL_ID = uuid5(NAMESPACE_URL, f"trainer-ts04:{KEY}")
ADMIN_DSN = os.environ["TRAINER_TEST_ADMIN_DSN"]
ENGINE = create_engine(ADMIN_DSN)
AT = os.environ.get("TS04_AT") or datetime.now(UTC).replace(microsecond=0).isoformat()
CONVERSATION = f"ts04-{KEY}"


def create_goal_request() -> dict[str, object]:
    return {
        "context": flow.context("goal-draft", AT, conversation=CONVERSATION),
        "intent": "create",
        "goal": flow.goal_payload(goal_id=str(GOAL_ID), name="Synthetic database outage retry"),
        "expected_revision_id": None,
        "reason": "synthetic TS-04 outage probe",
    }


def request_digest() -> str:
    normalized = GoalDraftRequest.model_validate(
        {**create_goal_request(), "request_digest": "0" * 64}
    ).model_dump(mode="json")
    normalized.pop("request_digest")
    return flow.digest(normalized)


def outage_phase() -> None:
    response = flow.post(
        "/api/v1/commands/goal-drafts",
        create_goal_request(),
        key=KEY,
        expected_error=500,
    )
    assert "data" not in response, response
    error = response.get("error", {})
    assert error.get("code") == "internal_error", response
    assert error.get("retryable") is True, response
    assert error.get("operation_state") == "failed", response
    print(
        "TS04_OUTAGE_PASS: HTTP 500, internal_error, retryable=true, "
        "operation_state=failed, no success data; PostgreSQL stopped"
    )


def recovery_phase() -> None:
    with ENGINE.connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM trainer.goals WHERE goal_id=:id"),
                {"id": GOAL_ID},
            ).scalar_one()
            == 0
        )

    body = create_goal_request()
    first = flow.post("/api/v1/commands/goal-drafts", body, key=KEY)["data"]
    replay = flow.post("/api/v1/commands/goal-drafts", body, key=KEY)["data"]
    assert replay == first

    prefix = f"ts04-{KEY}"
    preview = flow.preview_and_show(first, at=AT, prefix=prefix)
    committed = flow.confirm_preview(preview, first, at=AT, prefix=prefix)["data"]
    assert committed["status"] == "committed"

    with ENGINE.connect() as db:
        goal_count = db.execute(
            text("SELECT count(*) FROM trainer.goals WHERE goal_id=:id"),
            {"id": GOAL_ID},
        ).scalar_one()
        operation_rows = db.execute(
            text("""
                SELECT count(*), min(state), max(state)
                FROM trainer.idempotent_operations
                WHERE scope='local-owner:CMD-02' AND idempotency_key=:key
            """),
            {"key": KEY},
        ).one()
        draft_count = db.execute(
            text("""
                SELECT count(*) FROM trainer.drafts
                WHERE draft_id=:id AND intent_type='goal'
            """),
            {"id": first["draft_id"]},
        ).scalar_one()
        domain_change_count = db.execute(
            text("""
                SELECT count(*) FROM trainer.domain_changes
                WHERE object_type='goal' AND object_id=:id
            """),
            {"id": GOAL_ID},
        ).scalar_one()

    assert goal_count == 1
    assert operation_rows == (1, "committed", "committed")
    assert draft_count == 1
    assert domain_change_count == 1
    print(
        "TS04_RECOVERY_PASS: same-key retry/replay returned one draft; explicit confirm "
        "created exactly one goal, operation and domain change"
    )


if __name__ == "__main__":
    print("TS04_REQUEST_DIGEST", request_digest(), flush=True)
    mode = os.environ.get("TS04_MODE")
    if mode == "outage":
        outage_phase()
    elif mode == "recovery":
        recovery_phase()
    else:
        raise SystemExit("TS04_MODE must be outage or recovery")
