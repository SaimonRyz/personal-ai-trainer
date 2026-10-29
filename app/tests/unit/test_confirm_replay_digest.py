"""CMD-00 binds idempotency to confirmation intent, not observation time."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from trainer.application.context import ContextApplicationError, ContextService
from trainer.domain.context import digest_payload


def confirmation(*, effective_at: str, intent: str = "подтверждаю") -> dict[str, Any]:
    body = {
        "context": {
            "conversation_id": "synthetic-chat",
            "message_id": "synthetic-confirmation",
            "effective_at": effective_at,
            "timezone": "Asia/Novokuznetsk",
            "locale": None,
        },
        "revision_id": "11111111-1111-4111-8111-111111111111",
        "preview_digest": "a" * 64,
        "confirmation_kind": "normal",
        "confirmation_message_id": "synthetic-confirmation",
        "explicit_intent": intent,
        "confirmation_mode": "same_dialog",
        "referenced_preview_id": None,
        "reshown_message_id": None,
    }
    body["request_digest"] = digest_payload(body)
    return body


def test_changed_effective_at_has_same_semantic_idempotency_digest() -> None:
    calls: list[dict[str, Any]] = []

    def executor(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "committed"}

    service = ContextService(executor)
    first = confirmation(effective_at="2026-10-27T09:00:00Z")
    retry = confirmation(effective_at="2026-10-27T09:02:00Z")
    original = deepcopy(first)
    assert first["request_digest"] != retry["request_digest"]
    service.execute("confirm", payload=first, key="same-key")
    service.execute("confirm", payload=retry, key="same-key")
    assert first == original
    assert calls[0]["payload"]["request_digest"] == calls[1]["payload"]["request_digest"]
    assert calls[0]["payload"]["context"]["effective_at"] != (
        calls[1]["payload"]["context"]["effective_at"]
    )


def test_changed_confirmation_intent_keeps_idempotency_conflict() -> None:
    digests: list[str] = []

    def executor(**kwargs: Any) -> dict[str, Any]:
        digests.append(kwargs["payload"]["request_digest"])
        return {"status": "committed"}

    service = ContextService(executor)
    service.execute("confirm", payload=confirmation(effective_at="2026-10-27T09:00:00Z"))
    service.execute(
        "confirm", payload=confirmation(effective_at="2026-10-27T09:01:00Z", intent="нет")
    )
    assert digests[0] != digests[1]


def test_invalid_wire_digest_is_rejected_before_executor() -> None:
    service = ContextService(lambda **kwargs: pytest.fail("executor must not be called"))
    body = confirmation(effective_at="2026-10-27T09:00:00Z")
    body["request_digest"] = "b" * 64
    with pytest.raises(ContextApplicationError, match="digest does not match"):
        service.execute("confirm", payload=body)
