from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from trainer.config.settings import load_settings
from trainer.infrastructure.db.engine import create_runtime_engine
from trainer.infrastructure.db.operations import (
    IdempotencyConflictError,
    TechnicalOperationRepository,
)
from trainer.infrastructure.db.uow import SqlAlchemyUnitOfWork


def verify_runtime_database_boundary() -> None:
    engine = create_runtime_engine(load_settings())
    attempt_id = uuid4()
    with SqlAlchemyUnitOfWork(engine) as uow:
        assert uow.session is not None
        uow.session.execute(
            text(
                """
                INSERT INTO trainer.migration_attempts(
                    attempt_id,migration_id,checksum,started_at,result,created_at
                ) VALUES (:attempt_id,'runtime-rollback-probe',:checksum,now(),'running',now())
                """
            ),
            {"attempt_id": attempt_id, "checksum": "0" * 64},
        )
        uow.rollback()

    with SqlAlchemyUnitOfWork(engine) as uow:
        assert uow.session is not None
        repository = TechnicalOperationRepository(uow.session)
        key = f"runtime-idempotency-{uuid4()}"
        first = repository.reserve(
            scope="stage-c:CMD-00",
            idempotency_key=key,
            command_kind="CMD-00",
            request_digest="1" * 64,
        )
        replay = repository.reserve(
            scope="stage-c:CMD-00",
            idempotency_key=key,
            command_kind="CMD-00",
            request_digest="1" * 64,
        )
        if replay.operation_id != first.operation_id:
            raise RuntimeError("same idempotency key did not return one logical operation")
        try:
            repository.reserve(
                scope="stage-c:CMD-00",
                idempotency_key=key,
                command_kind="CMD-00",
                request_digest="2" * 64,
            )
        except IdempotencyConflictError:
            pass
        else:
            raise RuntimeError("changed digest did not conflict")
        uow.rollback()
    with engine.connect() as connection:
        remaining = connection.execute(
            text("SELECT count(*) FROM trainer.migration_attempts WHERE attempt_id=:attempt_id"),
            {"attempt_id": attempt_id},
        ).scalar_one()
        if remaining != 0:
            raise RuntimeError("rollback probe persisted unexpectedly")

    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM trainer.migration_attempts WHERE false"))
    except ProgrammingError:
        pass
    else:
        raise RuntimeError("runtime app role unexpectedly has DELETE privilege")
    engine.dispose()
