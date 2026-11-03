"""Store explicit program weekdays and dated cycle stages."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030_program_schedule"
down_revision: str | None = "0029_profile_update"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "48cbcabac33f6c83be226ec0e5454662f3ba2054d025a5655a0fca0f6d62b1e2"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE trainer.day_templates
          ADD COLUMN weekday_code text NULL
          CONSTRAINT ck_program_weekday CHECK (
            weekday_code IN ('monday','tuesday','wednesday','thursday',
                             'friday','saturday','sunday')
          );
        ALTER TABLE trainer.program_versions
          ADD COLUMN cycle_stages jsonb NOT NULL DEFAULT '[]'::jsonb
          CONSTRAINT ck_program_cycle_stages_array CHECK (jsonb_typeof(cycle_stages) = 'array');
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0030_program_schedule','{MIGRATION_CHECKSUM}',now(),0,'30',now());
    """)


def downgrade() -> None:
    raise RuntimeError("program weekday history is forward-only")
