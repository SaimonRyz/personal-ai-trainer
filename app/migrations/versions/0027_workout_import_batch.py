"""Name the narrow historical-workout import intent."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_workout_import_batch"
down_revision: str | None = "0026_cardio_exercise_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "f5f909e9113025955bd78a0795f6981e7a2e15cc1131e4d5f3f66ed971ee39d7"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.intent_type_catalog
            WHERE code='workout_import_batch'
              AND (description <> 'Historical workout import batch' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'workout import batch seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('workout_import_batch','Historical workout import batch',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0027_workout_import_batch','{MIGRATION_CHECKSUM}',now(),0,'27',now());
    """)


def downgrade() -> None:
    raise RuntimeError("workout import batch history is forward-only")
