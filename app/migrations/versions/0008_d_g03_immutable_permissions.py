"""Complete D-G03 immutable-tree indexes and grants.

Revision ID: 0008_d_g03_immutable_permissions
Revises: 0007_d_g03_exercise_catalog
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_d_g03_immutable_permissions"
down_revision: str | None = "0007_d_g03_exercise_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "945b8207188853f32744171b4140042724964c100a140b21017d13e59e594504"


def upgrade() -> None:
    op.execute(
        f"""
        DROP INDEX IF EXISTS trainer.ux_exercise_active_normalized;
        REVOKE UPDATE ON trainer.exercise_revisions,trainer.exercise_aliases,
            trainer.muscle_groups,trainer.exercise_muscle_roles,
            trainer.exercise_measurements,trainer.exercise_equipment_requirements
            FROM trainer_app;
        REVOKE UPDATE ON trainer.exercises FROM trainer_app;
        GRANT UPDATE(current_revision_id,active_normalized_name,archived_at,logically_deleted_at)
            ON trainer.exercises TO trainer_app;

        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at
        ) VALUES ('0008_d_g03_immutable_permissions','{MIGRATION_CHECKSUM}',now(),0,'8',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G03 migrations are forward-only; downgrade requires owner approval")
