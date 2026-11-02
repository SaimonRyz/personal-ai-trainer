"""Synthetic profile update gate: omitted fields cannot clear saved values."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from context_flow import confirm_preview, context, digest, post, preview_and_show
from trainer.transport.http.contracts import ProfileUpdateDraftRequest


def _update(body: dict, *, key: str, expected_status: int = 200) -> dict:
    if expected_status == 200:
        normalized = ProfileUpdateDraftRequest.model_validate(
            {**body, "request_digest": "0" * 64}
        ).model_dump(mode="json")
        normalized.pop("request_digest")
        body = normalized
    body = {**body, "request_digest": digest(body)}
    request = urllib.request.Request(
        os.environ["TRAINER_TEST_BASE_URL"] + "/api/v1/commands/profile-update-drafts",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['TRAINER_API_TOKEN']}",
            "Idempotency-Key": key, "X-Correlation-ID": str(uuid4()),
            "X-Causation-ID": str(uuid4()),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            assert response.status == expected_status
            return json.load(response)
    except urllib.error.HTTPError as error:
        assert error.code == expected_status, error.read().decode()
        return json.load(error)


def main() -> None:
    at = datetime.now(UTC).replace(microsecond=0).isoformat()
    place_id = str(uuid4())
    created = post("/api/v1/commands/onboarding-drafts", {
        "context": context("profile-create", at),
        "profile": {
            "birth_date": None, "sex": "not_stated", "timezone": "Asia/Novokuznetsk",
            "started_on": "2024-02-03", "desired_sessions_per_week": 4,
            "usual_place_id": place_id, "comment": "Вымышленный профиль",
        },
        "goals": [{
            "goal_id": None, "parent_goal_id": None, "name": "Вымышленная цель",
            "description": None, "goal_type": "body_weight", "start_value": "74",
            "target_value": "71", "unit_code": "kilogram", "set_on": "2026-10-01",
            "target_date": "2027-07-01", "priority": 10, "status": "active", "comment": None,
        }],
        "places": [{"place_id": place_id, "name": "Вымышленный зал",
                    "place_type": "home_gym", "status": "active", "comment": None}],
        "equipment": [], "familiar_exercises": [],
    }, key="profile-create")["data"]
    shown = preview_and_show(created, at=at, prefix="profile-create")
    assert confirm_preview(shown, created, at=at, prefix="profile-create")["data"]["status"] == "committed"
    saved = post("/api/v1/queries/profile-context", {
        "context": context("profile-before", at), "purpose": "synthetic readback"
    })["data"]
    ref = saved["profile_ref"]
    assert saved["profile"]["usual_place_id"] == place_id
    assert saved["profile"]["desired_sessions_per_week"] == 4

    base = {
        "context": context("profile-update", at),
        "profile_id": ref["object_id"],
        "expected_profile_revision_id": ref["revision_id"],
    }
    omitted = _update({**base, "profile": {
        "timezone": "Asia/Novokuznetsk", "birth_date": "1995-04-03"
    }}, key="profile-update-omitted", expected_status=400)
    assert omitted["error"]["code"] == "validation_error"

    complete = {**saved["profile"], "birth_date": "1995-04-03"}
    draft = _update({**base, "profile": complete}, key="profile-update-complete")["data"]
    preview = preview_and_show(draft, at=at, prefix="profile-update-complete")
    assert "Вымышленный зал" in preview["canonical_text"]
    assert confirm_preview(preview, draft, at=at, prefix="profile-update-complete")["data"]["status"] == "committed"
    current = post("/api/v1/queries/profile-context", {
        "context": context("profile-after", at), "purpose": "synthetic readback"
    })["data"]
    assert current["profile"]["birth_date"] == "1995-04-03"
    assert current["profile"]["desired_sessions_per_week"] == 4
    assert current["profile"]["usual_place_id"] == place_id
    assert current["profile"]["comment"] == "Вымышленный профиль"
    assert current["profile"]["started_on"] == "2024-02-03"
    print("PROFILE_UPDATE_FILLED_FIELDS_PASS")


if __name__ == "__main__":
    main()
