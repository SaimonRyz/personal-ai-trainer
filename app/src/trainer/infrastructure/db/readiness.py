from __future__ import annotations

import psycopg
from pydantic import SecretStr

from trainer.application.readiness import ReadinessResult
from trainer.infrastructure.db.migrator import (
    MIGRATIONS,
    SchemaCompatibilityError,
    UnfinishedOperationError,
    current_revision,
    validate_ledger,
    validate_migration_files,
    validate_no_unfinished_operations,
)


class PostgresReadinessProbe:
    def __init__(self, dsn: SecretStr, connect_timeout_seconds: int) -> None:
        self.dsn = dsn
        self.connect_timeout_seconds = connect_timeout_seconds

    def check(self) -> ReadinessResult:
        try:
            validate_migration_files()
            with psycopg.connect(
                self.dsn.get_secret_value().replace("postgresql+psycopg://", "postgresql://", 1),
                connect_timeout=self.connect_timeout_seconds,
            ) as connection:
                validate_ledger(connection)
                if current_revision(connection) != MIGRATIONS[-1].revision:
                    raise SchemaCompatibilityError("active schema is not at approved head")
                validate_no_unfinished_operations(connection)
        except UnfinishedOperationError:
            return ReadinessResult(ready=False, reason="operation_recovery_required")
        except SchemaCompatibilityError:
            return ReadinessResult(ready=False, reason="schema_not_ready")
        except (psycopg.Error, OSError):
            return ReadinessResult(ready=False, reason="database_unavailable")
        return ReadinessResult(ready=True, reason="ready")
