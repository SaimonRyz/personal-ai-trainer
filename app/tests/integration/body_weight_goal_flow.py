"""Synthetic onboarding and planning-context check for a body-weight goal."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from context_flow import confirm_preview, context, post, preview_and_show


def main() -> None:
    at = datetime.now(UTC).replace(microsecond=0).isoformat()
    goal = {
        "goal_id": None,
        "parent_goal_id": None,
        "name": "Synthetic body-weight goal",
        "description": None,
        "goal_type": "body_weight",
        "start_value": "75",
        "target_value": "68",
        "unit_code": "kilogram",
        "set_on": "2026-11-02",
        "target_date": "2027-06-01",
        "priority": 10,
        "status": "active",
        "comment": None,
    }
    body = {
        "context": context("weight-draft", at),
        "profile": {"timezone": "Asia/Novokuznetsk"},
        "goals": [goal],
        "places": [],
        "equipment": [],
        "familiar_exercises": [],
    }
    draft = post("/api/v1/commands/onboarding-drafts", body, key="weight-draft")["data"]
    preview = preview_and_show(draft, at=at, prefix="weight")
    text = preview["canonical_text"]
    assert "целевой вес 68 кг" in text
    assert "исходный вес со слов пользователя 75 кг" in text
    assert "не создаёт отдельный замер тела" in text
    committed = confirm_preview(preview, draft, at=at, prefix="weight")["data"]
    assert committed["status"] in {"committed", "already_committed"}
    saved = post(
        "/api/v1/queries/profile-context",
        {"context": context("weight-read", at), "purpose": "synthetic weight goal readback"},
    )["data"]
    assert len(saved["goals"]) == 1
    assert saved["goals"][0]["goal"]["goal_type"] == "body_weight"
    assert Decimal(str(saved["goals"][0]["goal"]["start_value"])) == Decimal("75")
    assert Decimal(str(saved["goals"][0]["goal"]["target_value"])) == Decimal("68")
    planning = post(
        "/api/v1/queries/planning-context",
        {"context": context("weight-plan", at), "target_date": "2026-11-03"},
    )["data"]
    assert any(fact["ref"]["object_type"] == "goal" for fact in planning["facts"])
    print("BODY_WEIGHT_GOAL_FLOW_PASS")


if __name__ == "__main__":
    main()
