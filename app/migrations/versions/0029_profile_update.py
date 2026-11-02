"""Allow confirmed updates to an existing profile."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029_profile_update"
down_revision: str | None = "0028_body_weight_goal_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "f838ab035788566b27a3cbb41dd1ea63839adc02823ef4127f1462a9941f1a7c"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.intent_type_catalog
            WHERE code='profile_update'
              AND (description <> 'Existing profile update' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'profile update seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('profile_update','Existing profile update',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0029_profile_update','{MIGRATION_CHECKSUM}',now(),0,'29',now());
    """)


def downgrade() -> None:
    raise RuntimeError("profile update history is forward-only")
