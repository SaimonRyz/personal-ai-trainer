"""Harden D-G03 alias uniqueness.

Revision ID: 0009_d_g03_validation_hardening
Revises: 0008_d_g03_immutable_permissions
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_d_g03_validation_hardening"
down_revision: str | None = "0008_d_g03_immutable_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "dcb543886d31207b746638243cba596040092b8083d2eea1b6b438c37d8df205"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at)
        VALUES
            ('repetitions','Repetitions',1,now()),
            ('duration','Duration',1,now()),
            ('distance','Distance',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.unit_family_catalog(code,description,seed_version,created_at)
        VALUES
            ('count','Count',1,now()),
            ('time','Time',1,now()),
            ('length','Length',1,now())
        ON CONFLICT(code) DO NOTHING;

        DO $$
        BEGIN
            IF (SELECT count(*) FROM trainer.metric_kind_catalog
                WHERE (code,description,seed_version) IN (
                    ('repetitions','Repetitions',1),('duration','Duration',1),
                    ('distance','Distance',1)
                ) AND archived_at IS NULL) <> 3
               OR (SELECT count(*) FROM trainer.unit_family_catalog
                WHERE (code,description,seed_version) IN (
                    ('count','Count',1),('time','Time',1),('length','Length',1)
                ) AND archived_at IS NULL) <> 3 THEN
                RAISE EXCEPTION 'D-G03 measurement seed conflict';
            END IF;
        END $$;

        CREATE TABLE trainer.exercise_measurement_compatibility (
            metric_kind text NOT NULL
                REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            requiredness text NOT NULL CHECK(requiredness IN ('required','optional')),
            unit_family text NOT NULL
                REFERENCES trainer.unit_family_catalog(code) ON DELETE RESTRICT,
            PRIMARY KEY(metric_kind,requiredness,unit_family)
        );
        INSERT INTO trainer.exercise_measurement_compatibility(
            metric_kind,requiredness,unit_family
        ) VALUES
            ('repetitions','required','count'),
            ('repetitions','optional','count'),
            ('duration','required','time'),
            ('duration','optional','time'),
            ('distance','required','length'),
            ('distance','optional','length');

        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM trainer.exercise_aliases
                GROUP BY exercise_revision_id, normalized_alias
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'D-G03 alias hardening blocked: duplicate normalized aliases exist';
            END IF;
            IF EXISTS (
                SELECT 1 FROM trainer.muscle_groups
                WHERE length(btrim(normalized_name)) = 0
            ) THEN
                RAISE EXCEPTION
                    'D-G03 muscle-group hardening blocked: blank normalized name exists';
            END IF;
            IF EXISTS (
                SELECT 1 FROM trainer.exercise_measurements measurement
                LEFT JOIN trainer.exercise_measurement_compatibility compatibility
                  ON compatibility.metric_kind=measurement.metric_kind
                 AND compatibility.requiredness=measurement.requiredness
                 AND compatibility.unit_family=measurement.unit_family
                WHERE compatibility.metric_kind IS NULL
            ) THEN
                RAISE EXCEPTION
                    'D-G03 measurement hardening blocked: incompatible measurement exists';
            END IF;
        END $$;

        DROP INDEX trainer.ux_exercise_alias_revision;
        CREATE UNIQUE INDEX ux_exercise_alias_revision
            ON trainer.exercise_aliases(exercise_revision_id, normalized_alias);

        ALTER TABLE trainer.exercise_measurements
            ADD CONSTRAINT fk_exercise_measurement_compatibility
            FOREIGN KEY(metric_kind,requiredness,unit_family)
            REFERENCES trainer.exercise_measurement_compatibility(
                metric_kind,requiredness,unit_family
            ) ON DELETE RESTRICT;
        ALTER TABLE trainer.muscle_groups
            ADD CONSTRAINT ck_muscle_groups_normalized_name_nonblank
            CHECK(length(btrim(normalized_name)) > 0);

        GRANT SELECT ON trainer.exercise_measurement_compatibility TO trainer_app;
        REVOKE INSERT,UPDATE,DELETE,TRUNCATE
            ON trainer.exercise_measurement_compatibility FROM trainer_app;

        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at
        ) VALUES ('0009_d_g03_validation_hardening','{MIGRATION_CHECKSUM}',now(),0,'9',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G03 migrations are forward-only; downgrade requires owner approval")
