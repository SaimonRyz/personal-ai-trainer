from uuid import uuid4

from fastapi.testclient import TestClient

from trainer.spike.app import FIXTURE_REVISION_ID, create_spike_app, digest, text_digest

TOKEN = "spike-local-token"


def headers(key: str = "key-1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
        "Idempotency-Key": key,
    }


def context(message_id: str) -> dict[str, str]:
    return {
        "conversation_id": "spike-conversation",
        "message_id": message_id,
        "effective_at": "2026-08-20T12:00:00+07:00",
        "timezone": "Asia/Novokuznetsk",
    }


def with_digest(body: dict[str, object]) -> dict[str, object]:
    return {**body, "request_digest": digest(body)}


def test_preview_shown_confirm_and_replay() -> None:
    client = TestClient(create_spike_app(TOKEN))
    render_body = with_digest(
        {"context": context("m1"), "revision_id": str(FIXTURE_REVISION_ID), "detail_level": "full"}
    )
    render = client.post("/api/v1/commands/previews", json=render_body, headers=headers("render"))
    assert render.status_code == 200
    preview = render.json()["data"]

    shown_body = with_digest(
        {
            "context": context("m2"),
            "revision_id": preview["revision_id"],
            "preview_digest": preview["digest"],
            "shown_message_id": "m2",
            "displayed_text_digest": text_digest(preview["canonical_text"]),
            "cross_dialog": False,
        }
    )
    shown = client.post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        json=shown_body,
        headers=headers("shown"),
    )
    assert shown.status_code == 200
    replay_shown = client.post(
        f"/api/v1/commands/previews/{preview['preview_id']}/shown",
        json=shown_body,
        headers=headers("shown"),
    )
    assert replay_shown.json()["data"]["shown_at"] == shown.json()["data"]["shown_at"]

    confirm_body = with_digest(
        {
            "context": context("m3"),
            "revision_id": preview["revision_id"],
            "preview_digest": preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": "m3",
            "explicit_intent": "confirm exactly the shown spike preview",
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        }
    )
    confirm_headers = headers("confirm")
    first = client.post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        json=confirm_body,
        headers=confirm_headers,
    )
    replay = client.post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        json=confirm_body,
        headers=confirm_headers,
    )
    assert first.json()["data"]["status"] == "committed"
    assert replay.json()["data"]["status"] == "already_committed"
    assert first.json()["data"]["commit_result_id"] == replay.json()["data"]["commit_result_id"]
    status_headers = {k: v for k, v in headers().items() if k != "Idempotency-Key"}
    status = client.post(
        "/api/v1/queries/operations/status",
        json={
            "context": {**context("m4"), "locale": "ru-RU"},
            "locator": {
                "kind": "correlation",
                "method_id": "CMD-00",
                "correlation_id": confirm_headers["X-Correlation-ID"],
            },
        },
        headers=status_headers,
    )
    assert status.status_code == 200
    assert status.json()["data"]["operation_id"] == first.json()["data"]["operation_id"]


def test_unshown_and_digest_conflict_are_rejected() -> None:
    client = TestClient(create_spike_app(TOKEN))
    body = with_digest(
        {"context": context("m1"), "revision_id": str(FIXTURE_REVISION_ID), "detail_level": "full"}
    )
    preview = client.post("/api/v1/commands/previews", json=body, headers=headers("render")).json()[
        "data"
    ]
    confirm = with_digest(
        {
            "context": context("m3"),
            "revision_id": preview["revision_id"],
            "preview_digest": preview["digest"],
            "confirmation_kind": "normal",
            "confirmation_message_id": "m3",
            "explicit_intent": "confirm exactly the shown spike preview",
            "confirmation_mode": "same_dialog",
            "referenced_preview_id": None,
            "reshown_message_id": None,
        }
    )
    response = client.post(
        f"/api/v1/commands/previews/{preview['preview_id']}/confirm",
        json=confirm,
        headers=headers("confirm"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "incomplete_preview"

    changed = dict(body)
    changed["detail_level"] = "short"
    changed["request_digest"] = digest({k: v for k, v in changed.items() if k != "request_digest"})
    conflict = client.post("/api/v1/commands/previews", json=changed, headers=headers("render"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_operation_locator_is_closed_discriminated_union() -> None:
    client = TestClient(create_spike_app(TOKEN))
    body = {
        "context": {**context("m1"), "locale": "ru-RU"},
        "locator": {"kind": "operation_id", "method_id": "CMD-00"},
    }
    response = client.post(
        "/api/v1/queries/operations/status",
        json=body,
        headers={k: v for k, v in headers().items() if k != "Idempotency-Key"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"

    null_locale = {
        "context": {**context("m2"), "locale": None},
        "locator": {"kind": "operation_id", "operation_id": str(uuid4())},
    }
    rejected_null = client.post(
        "/api/v1/queries/operations/status",
        json=null_locale,
        headers={k: v for k, v in headers().items() if k != "Idempotency-Key"},
    )
    assert rejected_null.status_code == 400
    assert rejected_null.json()["error"]["code"] == "validation_error"
