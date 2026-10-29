"""Persist exact exercise-familiarity selections on profile revisions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_profile_familiar_exercises"
down_revision: str | None = "0021_source_availability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "74ecb287331236e9c39b53d13609175d7d6c38c5df953b8ccf7ded43929a919a"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE trainer.profile_familiar_exercises (
            profile_revision_id uuid NOT NULL
                REFERENCES trainer.profile_revisions(profile_revision_id) ON DELETE RESTRICT,
            exercise_id uuid NOT NULL,
            exercise_revision_id uuid NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY(profile_revision_id,exercise_id),
            CONSTRAINT fk_profile_familiar_exact_exercise_revision
                FOREIGN KEY(exercise_id,exercise_revision_id)
                REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id)
                ON DELETE RESTRICT
        );
        CREATE INDEX ix_profile_familiar_exercise_revision
            ON trainer.profile_familiar_exercises(exercise_id,exercise_revision_id);
        GRANT SELECT,INSERT ON trainer.profile_familiar_exercises TO trainer_app;
        REVOKE UPDATE,DELETE,TRUNCATE ON trainer.profile_familiar_exercises FROM trainer_app;
        INSERT INTO trainer.schema_migrations
            (migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0022_profile_familiar_exercises','{MIGRATION_CHECKSUM}',now(),0,'22',now());
    """)


def downgrade() -> None:
    raise RuntimeError("familiarity history is forward-only")
