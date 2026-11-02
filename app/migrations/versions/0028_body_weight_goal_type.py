"""Add a body-weight goal type for dated, numeric training goals."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_body_weight_goal_type"
down_revision: str | None = "0027_workout_import_batch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "5fa2a01f7ea1a5b08fab85dacd29cdcfa20c536e115f588b2d6d691f836651bb"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.goal_type_catalog
            WHERE code='body_weight'
              AND (description <> 'Body weight goal' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'body weight goal type seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.goal_type_catalog(code,description,seed_version,created_at)
        VALUES ('body_weight','Body weight goal',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0028_body_weight_goal_type','{MIGRATION_CHECKSUM}',now(),0,'28',now());
    """)


def downgrade() -> None:
    raise RuntimeError("body weight goal type history is forward-only")
