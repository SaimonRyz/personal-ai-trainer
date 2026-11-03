"""Join the program schedule and plan recommendation migration branches."""

from __future__ import annotations

from alembic import op

revision = "0031_merge_program_plan"
down_revision = ("0030_plan_recommendation_kinds", "0030_program_schedule")
branch_labels = None
depends_on = None

MIGRATION_CHECKSUM = "91ff617c95213867ae9fc13bbe395e15e0f1a150482c661c2c8541fd77773d61"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0031_merge_program_plan','{MIGRATION_CHECKSUM}',now(),0,'31',now());
    """)


def downgrade() -> None:
    raise RuntimeError("migration branch merge is forward-only")
