"""Add the strength goal type accepted by onboarding and goal drafts."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025_strength_goal_type"
down_revision: str | None = "0024_parallel_bars_equipment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "ddfe1024d95cc4b558558f63e031f64e73969f069b081c2c85fe5e2e20573b6d"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.goal_type_catalog
            WHERE code='strength'
              AND (description <> 'Strength goal' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'strength goal type seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.goal_type_catalog(code,description,seed_version,created_at)
        VALUES ('strength','Strength goal',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0025_strength_goal_type','{MIGRATION_CHECKSUM}',now(),0,'25',now());
    """)


def downgrade() -> None:
    raise RuntimeError("strength goal type history is forward-only")
