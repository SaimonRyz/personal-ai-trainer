"""Seed the recommendation kinds accepted by workout plans."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030_plan_recommendation_kinds"
down_revision: str | None = "0029_profile_update"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "dfe40e06835572b50328cfb33a22ae3c34c73e2f935e797f63bb75fea6ec6a8f"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.recommendation_kind_catalog
            WHERE code IN ('plan_structure', 'exercise_selection')
              AND (seed_version <> 1 OR archived_at IS NOT NULL
                   OR (code = 'plan_structure' AND description <> 'Plan structure')
                   OR (code = 'exercise_selection' AND description <> 'Exercise selection'))
          ) THEN RAISE EXCEPTION 'plan recommendation kind seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.recommendation_kind_catalog
            (code, description, seed_version, created_at)
        VALUES ('plan_structure', 'Plan structure', 1, now()),
               ('exercise_selection', 'Exercise selection', 1, now())
        ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.schema_migrations
            (migration_id, checksum, applied_at, duration_ms, app_schema_version, created_at)
        VALUES ('0030_plan_recommendation_kinds', '{MIGRATION_CHECKSUM}', now(), 0, '30', now());
    """)


def downgrade() -> None:
    raise RuntimeError("Plan recommendation kind seed migration is forward-only")
