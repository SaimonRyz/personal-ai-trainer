from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from trainer.domain.evidence import PROTOCOL_CHECKS, PROTOCOL_VERSION
from trainer.infrastructure.db.context_repository import ContextRepositoryError
from trainer.infrastructure.db.evidence import EvidenceRepository


def test_expired_draft_cannot_be_verified_at_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = EvidenceRepository(Mock(), owner_verifier=True)
    profile, version = str(uuid4()), str(uuid4())
    # Status stays draft, and the saved snapshot is otherwise exactly unchanged.
    snapshot: dict[str, Any] = {
        "effective_status": "draft",
        "version": {"review_due_on": (datetime.now(UTC).date() - timedelta(days=1)).isoformat()},
    }
    monkeypatch.setattr(repository, "_profile", lambda: profile)
    monkeypatch.setattr(repository, "_knowledge_snapshot", lambda *_: deepcopy(snapshot))
    typed = {
        "owner_profile_id": profile,
        "evidence_change": "verification",
        "version_id": version,
        "snapshot": deepcopy(snapshot),
        "request": {
            "decision": "verified",
            "protocol_version": PROTOCOL_VERSION,
            "checklist": [{"check_id": c, "result": True} for c in PROTOCOL_CHECKS],
        },
    }
    with pytest.raises(ContextRepositoryError, match="review date expired") as error:
        repository._check_evidence_fresh(typed)
    assert error.value.code == "stale_preview"


def test_replacement_preview_includes_exact_sources_and_limitations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = EvidenceRepository(Mock())
    version = str(uuid4())
    snapshot = {
        "version": {"statement": "Saved exact statement"},
        "sources": [{"relation": "contradicts", "title": "Saved source title"}],
        "caveats": [{"description": "Saved conflict"}],
    }
    monkeypatch.setattr(repository, "_knowledge_snapshot", lambda *_: deepcopy(snapshot))
    payload = {
        "plan_lifecycle": {"intent": "reschedule"},
        "plan": {},
        "recommendations": [
            {
                "recommendation_id": str(uuid4()),
                "confidence": "low",
                "conclusion_links": [{"conclusion_version_id": version, "role": "supports"}],
            }
        ],
    }
    rendered = repository._render_preview(payload)
    assert all(
        s in rendered for s in ("Saved exact statement", "Saved source title", "Saved conflict")
    )
    assert "evidence_basis" not in payload  # rendering cannot mutate the canonical draft
