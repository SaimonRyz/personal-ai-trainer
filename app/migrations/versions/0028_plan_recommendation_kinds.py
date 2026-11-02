"""Seed the recommendation kinds accepted by workout plans."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_plan_recommendation_kinds"
down_revision: str | None = "0027_workout_import_batch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "8743a58fe467b13510b07140f9516d97920d1d11f319b7b3509164eb8cf789df"


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
        VALUES ('0028_plan_recommendation_kinds', '{MIGRATION_CHECKSUM}', now(), 0, '28', now());
    """)


def downgrade() -> None:
    raise RuntimeError("Plan recommendation kind seed migration is forward-only")
