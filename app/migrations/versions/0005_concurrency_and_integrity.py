"""Make current exercise-name uniqueness concurrency-safe and index ownership FK.

Revision ID: 0005_concurrency_and_integrity
Revises: 0004_architecture_corrections
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_concurrency_and_integrity"
down_revision: str | None = "0004_architecture_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "87f4c01f47f1a8c0e82d4e159f1c73bd4b6acb659d6e148400054ae718a45300"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX ix_program_versions_supersedes_owner
            ON trainer.program_versions(program_id,supersedes_version_id);

        DROP TRIGGER trg_current_exercise_name_uniqueness ON trainer.exercises;
        DROP FUNCTION trainer.guard_current_exercise_name_uniqueness();

        CREATE TABLE trainer.exercise_current_names (
            normalized_name text PRIMARY KEY,
            exercise_id uuid NOT NULL UNIQUE
                REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL
        );

        CREATE FUNCTION trainer.refresh_exercise_current_name(target_exercise_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, trainer
        AS $$
        BEGIN
            DELETE FROM trainer.exercise_current_names
            WHERE exercise_id = target_exercise_id;

            INSERT INTO trainer.exercise_current_names(
                normalized_name,exercise_id,created_at,updated_at
            )
            SELECT revision.normalized_name,exercise.exercise_id,now(),now()
            FROM trainer.exercises exercise
            JOIN trainer.exercise_revisions revision
              ON revision.exercise_revision_id=exercise.current_revision_id
            WHERE exercise.exercise_id=target_exercise_id
              AND exercise.archived_at IS NULL
              AND exercise.logically_deleted_at IS NULL;
        END $$;
        REVOKE ALL ON FUNCTION trainer.refresh_exercise_current_name(uuid) FROM PUBLIC;

        CREATE FUNCTION trainer.refresh_exercise_name_from_identity()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, trainer
        AS $$
        BEGIN
            PERFORM trainer.refresh_exercise_current_name(NEW.exercise_id);
            RETURN NEW;
        END $$;
        REVOKE ALL ON FUNCTION trainer.refresh_exercise_name_from_identity() FROM PUBLIC;

        CREATE FUNCTION trainer.refresh_exercise_name_from_revision()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, trainer
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM trainer.exercises
                WHERE exercise_id=NEW.exercise_id
                  AND current_revision_id=NEW.exercise_revision_id
            ) THEN
                PERFORM trainer.refresh_exercise_current_name(NEW.exercise_id);
            END IF;
            RETURN NEW;
        END $$;
        REVOKE ALL ON FUNCTION trainer.refresh_exercise_name_from_revision() FROM PUBLIC;

        CREATE TRIGGER trg_refresh_exercise_name_identity
            AFTER INSERT OR UPDATE OF current_revision_id,archived_at,logically_deleted_at
            ON trainer.exercises
            FOR EACH ROW EXECUTE FUNCTION trainer.refresh_exercise_name_from_identity();
        CREATE TRIGGER trg_refresh_exercise_name_revision
            AFTER INSERT OR UPDATE OF normalized_name
            ON trainer.exercise_revisions
            FOR EACH ROW EXECUTE FUNCTION trainer.refresh_exercise_name_from_revision();

        INSERT INTO trainer.exercise_current_names(
            normalized_name,exercise_id,created_at,updated_at
        )
        SELECT revision.normalized_name,exercise.exercise_id,now(),now()
        FROM trainer.exercises exercise
        JOIN trainer.exercise_revisions revision
          ON revision.exercise_revision_id=exercise.current_revision_id
        WHERE exercise.archived_at IS NULL
          AND exercise.logically_deleted_at IS NULL;

        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at
        ) VALUES (
            '0005_concurrency_and_integrity','{MIGRATION_CHECKSUM}',now(),0,'5',now()
        );
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "concurrency integrity migration is forward-only; downgrade requires owner approval"
    )
