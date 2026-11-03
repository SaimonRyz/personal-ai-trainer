"""Resume a disposable database after both 0030 branches committed."""

from __future__ import annotations

import sys
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import (
    MIGRATIONS,
    SchemaCompatibilityError,
    current_revision,
    current_revisions,
    migrate_to_head,
    migrator_dsn,
    record_attempt,
    validate_ledger,
)


def main() -> None:
    branches = ("0030_plan_recommendation_kinds", "0030_program_schedule")
    if len(sys.argv) != 2 or sys.argv[1] not in {"plan_first", "schedule_first"}:
        raise ValueError("select plan_first or schedule_first")
    order = branches if sys.argv[1] == "plan_first" else branches[::-1]
    for revision in order:
        command.upgrade(Config("alembic.ini"), revision)

    interrupted_id = str(uuid4())
    with psycopg.connect(migrator_dsn()) as connection:
        assert current_revisions(connection) == branches
        try:
            current_revision(connection)
        except SchemaCompatibilityError as error:
            assert "multiple active schema revisions" in str(error)
        else:
            raise AssertionError("single-revision read accepted two active heads")
        validate_ledger(connection)
        record_attempt(
            connection, attempt_id=interrupted_id, migration=MIGRATIONS[-1],
            result="running", error_code=None,
        )

    migrate_to_head()
    migrate_to_head()  # A retry must not run either branch again.
    with psycopg.connect(migrator_dsn()) as connection:
        validate_ledger(connection)
        assert current_revisions(connection) == ("0031_merge_program_plan",)
        assert current_revision(connection) == "0031_merge_program_plan"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT result,error_code FROM trainer.migration_attempts WHERE attempt_id=%s",
                (interrupted_id,),
            )
            assert cursor.fetchone() == ("failed", "interrupted_runner")
            cursor.execute(
                "SELECT migration_id FROM trainer.schema_migrations "
                "WHERE migration_id LIKE '0030_%' OR migration_id='0031_merge_program_plan' "
                "ORDER BY migration_id"
            )
            assert [row[0] for row in cursor.fetchall()] == [*branches, "0031_merge_program_plan"]
    print(f"PASS: interrupted dual-head migration recovered ({sys.argv[1]})")


if __name__ == "__main__":
    main()
