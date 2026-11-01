"""Add the cardio category for aerobic exercise cards."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_cardio_exercise_category"
down_revision: str | None = "0025_strength_goal_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "b3d559f62213c86a84c14898ea1660c7fdb547fde2919949e0a3a004b4322dea"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.exercise_category_catalog
            WHERE code='cardio'
              AND (description <> 'Cardio exercise' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'cardio exercise category seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.exercise_category_catalog(code,description,seed_version,created_at)
        VALUES ('cardio','Cardio exercise',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0026_cardio_exercise_category','{MIGRATION_CHECKSUM}',now(),0,'26',now());
    """)


def downgrade() -> None:
    raise RuntimeError("cardio exercise category history is forward-only")
