from __future__ import annotations

from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trainer.domain.context import digest_payload
from trainer.infrastructure.db.context_repository import ContextRepository, ContextRepositoryError
from trainer.transport.http.contracts import PlanningFact


def test_planning_fact_digest_uses_exact_stored_revision() -> None:
    ref = {
        "object_type": "goal",
        "object_id": str(uuid4()),
        "revision_id": str(uuid4()),
    }
    stored_revision = {"goal_id": ref["object_id"], "goal_revision_id": ref["revision_id"]}
    session = Mock()
    session.execute.return_value.scalar_one_or_none.return_value = stored_revision

    result = ContextRepository(session)._planning_fact_digest(ref)

    assert result == digest_payload(stored_revision)
    parameters = session.execute.call_args.args[1]
    assert parameters == {"revision": ref["revision_id"], "object": ref["object_id"]}


def test_planning_fact_digest_fails_if_exact_revision_is_missing() -> None:
    session = Mock()
    session.execute.return_value.scalar_one_or_none.return_value = None
    ref = {"object_type": "goal", "object_id": str(uuid4()), "revision_id": str(uuid4())}

    with pytest.raises(ContextRepositoryError, match="planning fact revision is unavailable"):
        ContextRepository(session)._planning_fact_digest(ref)


def test_planning_fact_wire_requires_digest() -> None:
    fact = {
        "ref": {"object_type": "goal", "object_id": str(uuid4()), "revision_id": str(uuid4())},
        "priority": 10,
        "source_kind": "user",
    }

    with pytest.raises(ValidationError):
        PlanningFact.model_validate(fact)
    assert PlanningFact.model_validate({**fact, "snapshot_digest": "a" * 64}).snapshot_digest == (
        "a" * 64
    )
