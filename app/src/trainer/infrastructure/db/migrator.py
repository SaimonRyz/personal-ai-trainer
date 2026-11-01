from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

LOCK_KEY = 741852963


class SchemaCompatibilityError(RuntimeError):
    """The stored migration state is not safe for an automatic upgrade."""


class UnfinishedOperationError(SchemaCompatibilityError):
    """An interrupted or executing operation requires recovery before writes."""


@dataclass(frozen=True)
class MigrationSpec:
    revision: str
    checksum: str
    filename: str


MIGRATIONS = (
    MigrationSpec(
        "0001_technical_foundation",
        "e8d8a97493c24753a53b395f1727327543b075eb7a476e9a9b1debee24f78cf3",
        "0001_technical_foundation.py",
    ),
    MigrationSpec(
        "0002_domain_schema",
        "537dd916fa19962440b6a10c2d077f18f0a0c94c2f1d4af079787365105e70d8",
        "0002_domain_schema.py",
    ),
    MigrationSpec(
        "0003_schema_hardening",
        "7f935c96924978ed095bc92451dae2775a842c90e3420a2cd3b72f7b6bfcc843",
        "0003_schema_hardening.py",
    ),
    MigrationSpec(
        "0004_architecture_corrections",
        "32e46602d7cc5e5cd0cf05bfb345dd32f7f84599de7775b4880e3771ebefc022",
        "0004_architecture_corrections.py",
    ),
    MigrationSpec(
        "0005_concurrency_and_integrity",
        "87f4c01f47f1a8c0e82d4e159f1c73bd4b6acb659d6e148400054ae718a45300",
        "0005_concurrency_and_integrity.py",
    ),
    MigrationSpec(
        "0006_d_g02_context",
        "5a670d1247e3220a9da7afaf7d84d9ae18efee73377b142589b1f919ab05d380",
        "0006_d_g02_context.py",
    ),
    MigrationSpec(
        "0007_d_g03_exercise_catalog",
        "d78aa173fe182f1201a17dbfd1c90fe55e090448534392525f9e61b3da95f442",
        "0007_d_g03_exercise_catalog.py",
    ),
    MigrationSpec(
        "0008_d_g03_immutable_permissions",
        "945b8207188853f32744171b4140042724964c100a140b21017d13e59e594504",
        "0008_d_g03_immutable_permissions.py",
    ),
    MigrationSpec(
        "0009_d_g03_validation_hardening",
        "dcb543886d31207b746638243cba596040092b8083d2eea1b6b438c37d8df205",
        "0009_d_g03_validation_hardening.py",
    ),
    MigrationSpec(
        "0010_d_g04_programs",
        "1c0805bd61031d62044e72fc8d02724e13ac38b2f178b657b032c0a9a30d3e6a",
        "0010_d_g04_programs.py",
    ),
    MigrationSpec(
        "0011_d_g04_correction_hardening",
        "27589f73cec9b8eff803370b318537af4483fd263220f5dc8db12c0e97a0ef1b",
        "0011_d_g04_correction_hardening.py",
    ),
    MigrationSpec(
        "0012_d_g04_seed_exactness",
        "0401d1b8541d605518882858a50c6f877fa85d04782f74f56e18f8882a4bf083",
        "0012_d_g04_seed_exactness.py",
    ),
    MigrationSpec(
        "0013_d_g05_plans",
        "224fd19ca69cbde497fa9e7da9e05bf1dc7ce9ae7f884550d89c580043e8050e",
        "0013_d_g05_plans.py",
    ),
    MigrationSpec(
        "0014_d_g06_workouts",
        "926137b8805d032c7fd34d4397a38965fdcf1e5c337190f1662491a36639c72f",
        "0014_d_g06_workouts.py",
    ),
    MigrationSpec(
        "0015_e_g02_records",
        "50560bdc9cb058dca8bd237c0bfc167088afd20545025acb55fa1b4495d6aaa9",
        "0015_e_g02_records.py",
    ),
    MigrationSpec(
        "0016_e_g03_progress",
        "02f0d60e335c0ddb6c461cd5d15d7099043d7fee13dbef2b92c3e42593242353",
        "0016_e_g03_progress.py",
    ),
    MigrationSpec(
        "0017_e_g04_inventory",
        "a06eb8c7af11c6fd5a029f896b317ac862c2e9d60182793f8b1dafc03ec92225",
        "0017_e_g04_inventory.py",
    ),
    MigrationSpec(
        "0018_e_g06_measurements",
        "c5569fbe79f5662e7b50f03be185807b65b7283593eef896ff4f7ba121028f83",
        "0018_e_g06_measurements.py",
    ),
    MigrationSpec(
        "0019_e_g07_evidence",
        "24d47aa4ea9fbbec2a3714de75a8a9bb19e927de74205425a19dad41487313ac",
        "0019_e_g07_evidence.py",
    ),
    MigrationSpec(
        "0020_h03_exercise_category_seed",
        "4565b8c25208586fce3ac7da8d016472d9d41905480e87fcd72d67aa8fa610ea",
        "0020_h03_exercise_category_seed.py",
    ),
    MigrationSpec(
        "0021_source_availability",
        "70c6b69f62fcf38e4c8a0e13ad8a2c3a84f4c36e1446d9d1a4c9f71ab9b0307a",
        "0021_evidence_source_availability.py",
    ),
    MigrationSpec(
        "0022_profile_familiar_exercises",
        "74ecb287331236e9c39b53d13609175d7d6c38c5df953b8ccf7ded43929a919a",
        "0022_profile_familiar_exercises.py",
    ),
    MigrationSpec(
        "0023_familiar_exercise_batch",
        "d6f884130b50a2f750ab92f02ea7d5944fa92ca09d4ccbb239a73267abca319d",
        "0023_familiar_exercise_batch.py",
    ),
)


def validate_migration_files(migrations_dir: Path = Path("migrations")) -> None:
    manifest_path = migrations_dir / "checksums.json"
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SchemaCompatibilityError("migration file-checksum manifest is unavailable") from error
    if not isinstance(raw_manifest, dict):
        raise SchemaCompatibilityError("migration file-checksum manifest is invalid")
    expected_files = {migration.filename for migration in MIGRATIONS}
    if set(raw_manifest) != expected_files:
        raise SchemaCompatibilityError("migration file-checksum manifest set mismatch")
    for migration in MIGRATIONS:
        path = migrations_dir / "versions" / migration.filename
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise SchemaCompatibilityError(
                f"migration file is unavailable: {migration.filename}"
            ) from error
        if raw_manifest[migration.filename] != actual:
            raise SchemaCompatibilityError(
                f"migration file checksum mismatch: {migration.filename}"
            )


def migrator_dsn() -> str:
    value = os.environ.get("TRAINER_DATABASE_MIGRATOR_DSN")
    if not value:
        raise RuntimeError("TRAINER_DATABASE_MIGRATOR_DSN is required")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def table_exists(connection: psycopg.Connection[tuple[object, ...]], qualified_name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified_name,))
        row = cursor.fetchone()
        return bool(row and row[0])


def current_revision(
    connection: psycopg.Connection[tuple[object, ...]],
) -> str | None:
    if not table_exists(connection, "trainer.alembic_version"):
        return None
    with connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        row = cursor.fetchone()
        return str(row[0]) if row else None


def validate_ledger(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    revision = current_revision(connection)
    if revision is None:
        return
    expected = {item.revision: item.checksum for item in MIGRATIONS}
    if revision not in expected:
        raise SchemaCompatibilityError(f"unknown active schema revision: {revision}")
    if not table_exists(connection, "trainer.schema_migrations"):
        raise SchemaCompatibilityError("schema_migrations ledger is missing")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT migration_id,checksum FROM trainer.schema_migrations ORDER BY migration_id"
        )
        rows = [(str(row[0]), str(row[1])) for row in cursor.fetchall()]
    revision_index = next(
        index for index, item in enumerate(MIGRATIONS) if item.revision == revision
    )
    expected_rows = [(item.revision, item.checksum) for item in MIGRATIONS[: revision_index + 1]]
    if rows != expected_rows:
        raise SchemaCompatibilityError("migration ledger revision/checksum mismatch")


def validate_no_unfinished_operations(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    if not table_exists(connection, "trainer.idempotent_operations"):
        return
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM trainer.idempotent_operations
            WHERE state IN ('executing','recovery_required')
            """
        )
        row = cursor.fetchone()
    if row and row[0] != 0:
        raise UnfinishedOperationError("unfinished operations block schema migration")


def mark_interrupted_attempts(
    connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    if not table_exists(connection, "trainer.migration_attempts"):
        return
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE trainer.migration_attempts
            SET finished_at=now(),result='failed',error_code='interrupted_runner'
            WHERE result='running'
            """
        )
    connection.commit()


def record_attempt(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    attempt_id: str,
    migration: MigrationSpec,
    result: str,
    error_code: str | None,
) -> None:
    if not table_exists(connection, "trainer.migration_attempts"):
        return
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO trainer.migration_attempts(
                attempt_id,migration_id,checksum,started_at,finished_at,result,error_code,created_at
            ) VALUES (
                %s,%s,%s,now(),CASE WHEN %s='running' THEN NULL ELSE now() END,%s,%s,now()
            )
            ON CONFLICT (attempt_id) DO UPDATE SET
                finished_at=CASE WHEN EXCLUDED.result='running' THEN NULL ELSE now() END,
                result=EXCLUDED.result,error_code=EXCLUDED.error_code
            """,
            (
                attempt_id,
                migration.revision,
                migration.checksum,
                result,
                result,
                error_code,
            ),
        )
    connection.commit()


def pending_migrations(revision: str | None) -> tuple[MigrationSpec, ...]:
    if revision is None:
        return MIGRATIONS
    for index, migration in enumerate(MIGRATIONS):
        if migration.revision == revision:
            return MIGRATIONS[index + 1 :]
    raise SchemaCompatibilityError(f"unknown active schema revision: {revision}")


def migrate_to_head() -> None:
    validate_migration_files()
    with psycopg.connect(migrator_dsn()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            mark_interrupted_attempts(connection)
            validate_ledger(connection)
            validate_no_unfinished_operations(connection)
            for migration in pending_migrations(current_revision(connection)):
                attempt_id = str(uuid4())
                record_attempt(
                    connection,
                    attempt_id=attempt_id,
                    migration=migration,
                    result="running",
                    error_code=None,
                )
                started = time.monotonic()
                try:
                    command.upgrade(Config("alembic.ini"), migration.revision)
                    elapsed_ms = max(1, round((time.monotonic() - started) * 1000))
                    validate_ledger(connection)
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE trainer.schema_migrations SET duration_ms=%s
                            WHERE migration_id=%s AND checksum=%s
                            """,
                            (elapsed_ms, migration.revision, migration.checksum),
                        )
                        if cursor.rowcount != 1:
                            raise SchemaCompatibilityError(
                                f"missing ledger row for {migration.revision}"
                            )
                    connection.commit()
                except Exception as error:
                    connection.rollback()
                    record_attempt(
                        connection,
                        attempt_id=attempt_id,
                        migration=migration,
                        result="failed",
                        error_code=type(error).__name__,
                    )
                    raise
                record_attempt(
                    connection,
                    attempt_id=attempt_id,
                    migration=migration,
                    result="succeeded",
                    error_code=None,
                )
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
            connection.commit()
