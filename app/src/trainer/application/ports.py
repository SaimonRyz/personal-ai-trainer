from __future__ import annotations

from typing import Protocol, Self


class UnitOfWork(Protocol):
    """Transaction boundary exposed inward without an ORM dependency."""

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
