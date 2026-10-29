"""Verify correction recovery and history reads after the API process restarts."""

from __future__ import annotations

from sqlalchemy import text
from workout_flow import ENGINE, context, post

with ENGINE.connect() as db:
    operation = (
        db.execute(
            text("""
        SELECT o.operation_id,o.commit_result_id,d.object_id,d.new_revision_id
        FROM trainer.idempotent_operations o
        JOIN trainer.commit_results cr ON cr.operation_id=o.operation_id
        JOIN trainer.domain_changes d ON d.commit_result_id=cr.commit_result_id
          AND d.object_type='workout_session'
        WHERE o.scope='local-owner:CMD-00' AND o.idempotency_key='dg07-correction-confirm-key'
          AND o.state='committed'
    """)
        )
        .mappings()
        .one()
    )

status = post(
    "/api/v1/queries/operations/status",
    {
        "context": context("dg07-after-restart-status"),
        "locator": {"kind": "operation_id", "operation_id": str(operation["operation_id"])},
    },
)["data"]
assert status["state"] == "committed"
assert status["commit_result"]["commit_result_id"] == str(operation["commit_result_id"])

history = post(
    "/api/v1/queries/workouts/history",
    {
        "context": context("dg07-after-restart-history"),
        "date_from": "2026-09-01",
        "date_to": "2026-09-30",
        "limit": 100,
    },
)["data"]["items"]
with ENGINE.connect() as db:
    latest_revision_id = str(
        db.execute(
            text("SELECT current_revision_id FROM trainer.workout_sessions WHERE session_id=:id"),
            {"id": operation["object_id"]},
        ).scalar_one()
    )
current = next(
    item
    for item in history
    if item["current"] and item["session_ref"]["object_id"] == str(operation["object_id"])
)
assert current["session_ref"]["revision_id"] == latest_revision_id
print("PASS: D-G07 QRY-10 recovery and QRY-05 read-back after API restart")
