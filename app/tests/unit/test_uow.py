from __future__ import annotations

from unittest.mock import Mock

from sqlalchemy import Engine

from trainer.infrastructure.db.uow import SqlAlchemyUnitOfWork


def test_uow_requires_active_session() -> None:
    engine = Mock(spec=Engine)
    uow = SqlAlchemyUnitOfWork(engine)
    for operation in (uow.commit, uow.rollback):
        try:
            operation()
        except RuntimeError as error:
            assert str(error) == "unit of work is not active"
        else:
            raise AssertionError("inactive unit of work must fail")
