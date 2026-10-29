from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine

from trainer.config.settings import Settings
from trainer.infrastructure.db.context_repository import ContextRepositoryError
from trainer.infrastructure.db.progress import ProgressAwareExecutor


def test_exercise_identity_configuration_is_empty_by_default_and_strictly_typed():
    settings = Settings(api_token=SecretStr("s" * 32))
    assert settings.e1rm_bench_exercise_ids == frozenset()
    assert settings.e1rm_bench_grip_conditions == frozenset()
    assert Settings(
        api_token=SecretStr("s" * 32),
        e1rm_bench_grip_conditions=["  Medium   Grip "],
    ).e1rm_bench_grip_conditions == frozenset({"medium grip"})
    with pytest.raises(ValidationError):
        Settings(api_token=SecretStr("s" * 32), e1rm_bench_exercise_ids=["жим лёжа"])
    with pytest.raises(ValidationError):
        Settings(api_token=SecretStr("s" * 32), e1rm_bench_grip_conditions=[" "])


def test_unconfigured_exercise_is_rejected_before_loading_any_name_or_source():
    approved, other = uuid4(), uuid4()
    executor = ProgressAwareExecutor(
        create_engine("sqlite://"), MagicMock(), eligible_exercise_ids=frozenset({approved})
    )
    session = MagicMock()
    with pytest.raises(ContextRepositoryError) as error:
        executor._load_e1rm(
            session,
            {
                "source": {"exercise_id": str(other)},
                "execution_attestation": {},
            },
        )
    assert error.value.code == "unsupported_exercise"
    session.execute.assert_not_called()


def test_grip_condition_requires_exact_local_allowlist_and_is_normalized():
    executor = ProgressAwareExecutor(
        create_engine("sqlite://"),
        MagicMock(),
        eligible_grip_conditions=frozenset({"medium grip"}),
    )
    assert executor._configured_grip_condition(" Medium   Grip ") == "medium grip"
    with pytest.raises(ContextRepositoryError) as error:
        executor._configured_grip_condition("wide grip")
    assert error.value.code == "unsupported_grip_condition"


def test_grip_condition_is_unavailable_without_local_configuration():
    executor = ProgressAwareExecutor(create_engine("sqlite://"), MagicMock())
    with pytest.raises(ContextRepositoryError) as error:
        executor._configured_grip_condition("medium grip")
    assert error.value.code == "unsupported_grip_condition"
