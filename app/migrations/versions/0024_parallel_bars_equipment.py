"""Add parallel bars to the approved equipment catalog."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_parallel_bars_equipment"
down_revision: str | None = "0023_familiar_exercise_batch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "6df12136df187dd34a41252318f3fd9468a009c691af89c10da07905da998bf1"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.equipment_type_catalog
            WHERE code='parallel_bars'
              AND (description <> 'Parallel bars' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'parallel bars equipment seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.equipment_type_catalog(code,description,seed_version,created_at)
        VALUES ('parallel_bars','Parallel bars',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0024_parallel_bars_equipment','{MIGRATION_CHECKSUM}',now(),0,'24',now());
    """)


def downgrade() -> None:
    raise RuntimeError("parallel bars equipment history is forward-only")
