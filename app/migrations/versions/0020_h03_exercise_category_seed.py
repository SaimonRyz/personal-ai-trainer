"""Provide the exercise category required by the existing exercise API."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_h03_exercise_category_seed"
down_revision: str | None = "0019_e_g07_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "4565b8c25208586fce3ac7da8d016472d9d41905480e87fcd72d67aa8fa610ea"


def upgrade() -> None:
    op.execute("""
        DO $seed$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.exercise_category_catalog
            WHERE code='strength'
              AND (description<>'Strength exercise' OR seed_version<>1
                   OR archived_at IS NOT NULL)
          ) THEN
            RAISE EXCEPTION 'H-03 exercise category seed conflict';
          END IF;
        END $seed$;
        INSERT INTO trainer.exercise_category_catalog
            (code,description,seed_version,created_at)
        VALUES ('strength','Strength exercise',1,now())
        ON CONFLICT (code) DO NOTHING;
    """)
    op.execute(f"""
        INSERT INTO trainer.schema_migrations
            (migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0020_h03_exercise_category_seed','{MIGRATION_CHECKSUM}',now(),0,'20',now());
    """)


def downgrade() -> None:
    raise RuntimeError("H-03 exercise category seed migration is forward-only")
