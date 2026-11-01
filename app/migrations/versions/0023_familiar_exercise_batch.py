"""Name the atomic familiar-exercise batch separately from catalog card drafts."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_familiar_exercise_batch"
down_revision: str | None = "0022_profile_familiar_exercises"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "d6f884130b50a2f750ab92f02ea7d5944fa92ca09d4ccbb239a73267abca319d"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.intent_type_catalog
            WHERE code='familiar_exercise_batch'
              AND (description <> 'Familiar exercise batch' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'familiar exercise batch seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('familiar_exercise_batch','Familiar exercise batch',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0023_familiar_exercise_batch','{MIGRATION_CHECKSUM}',now(),0,'23',now());
    """)


def downgrade() -> None:
    raise RuntimeError("familiarity batch history is forward-only")
