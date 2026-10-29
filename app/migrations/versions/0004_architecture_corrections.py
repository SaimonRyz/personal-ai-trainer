"""Correct ownership, lifecycle, and uniqueness invariants found by the C gate.

Revision ID: 0004_architecture_corrections
Revises: 0003_schema_hardening
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_architecture_corrections"
down_revision: str | None = "0003_schema_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "32e46602d7cc5e5cd0cf05bfb345dd32f7f84599de7775b4880e3771ebefc022"


def upgrade() -> None:
    op.execute(
        f"""
        DROP INDEX trainer.ux_exercise_active_normalized;

        CREATE FUNCTION trainer.guard_current_exercise_name_uniqueness()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM trainer.exercises current_exercise
                JOIN trainer.exercise_revisions current_revision
                  ON current_revision.exercise_revision_id = current_exercise.current_revision_id
                JOIN trainer.exercises other_exercise
                  ON other_exercise.exercise_id <> current_exercise.exercise_id
                 AND other_exercise.archived_at IS NULL
                 AND other_exercise.logically_deleted_at IS NULL
                JOIN trainer.exercise_revisions other_revision
                  ON other_revision.exercise_revision_id = other_exercise.current_revision_id
                 AND other_revision.normalized_name = current_revision.normalized_name
                WHERE current_exercise.exercise_id = NEW.exercise_id
                  AND current_exercise.archived_at IS NULL
                  AND current_exercise.logically_deleted_at IS NULL
            ) THEN
                RAISE EXCEPTION 'duplicate active current exercise normalized_name'
                    USING ERRCODE='23505';
            END IF;
            RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_current_exercise_name_uniqueness
            AFTER INSERT OR UPDATE OF current_revision_id,archived_at,logically_deleted_at
            ON trainer.exercises DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION trainer.guard_current_exercise_name_uniqueness();

        ALTER TABLE trainer.program_versions
            DROP CONSTRAINT program_versions_supersedes_version_id_fkey,
            ADD CONSTRAINT uq_program_versions_owner
                UNIQUE(program_id,program_version_id),
            ADD CONSTRAINT fk_program_versions_supersedes_owner
                FOREIGN KEY(program_id,supersedes_version_id)
                REFERENCES trainer.program_versions(program_id,program_version_id)
                ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

        DROP TRIGGER trg_program_publication ON trainer.program_versions;
        CREATE OR REPLACE FUNCTION trainer.guard_program_publication()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.publication_state = OLD.publication_state THEN
                IF NEW.published_at IS DISTINCT FROM OLD.published_at
                   OR NEW.confirmation_id IS DISTINCT FROM OLD.confirmation_id THEN
                    RAISE EXCEPTION 'program publication metadata is immutable'
                        USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.publication_state='composing' AND NEW.publication_state='published' THEN
                IF NEW.published_at IS NULL OR NEW.confirmation_id IS NULL THEN
                    RAISE EXCEPTION 'publication requires confirmation'
                        USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.publication_state='published' AND NEW.publication_state='retired' THEN
                IF NEW.published_at IS DISTINCT FROM OLD.published_at
                   OR NEW.confirmation_id IS DISTINCT FROM OLD.confirmation_id THEN
                    RAISE EXCEPTION 'program publication metadata is immutable'
                        USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid program publication transition'
                USING ERRCODE='23514';
        END $$;
        CREATE TRIGGER trg_program_publication
            BEFORE UPDATE OF publication_state,published_at,confirmation_id
            ON trainer.program_versions
            FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_publication();

        ALTER TABLE trainer.body_measurement_values
            DROP CONSTRAINT body_measurement_values_measurement_session_id_measurement__key,
            ADD CONSTRAINT uq_body_measurement_kind_side
                UNIQUE NULLS NOT DISTINCT(measurement_session_id,measurement_kind,side);

        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at
        ) VALUES (
            '0004_architecture_corrections','{MIGRATION_CHECKSUM}',now(),0,'4',now()
        );
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "architecture corrections are forward-only; destructive downgrade requires owner approval"
    )
