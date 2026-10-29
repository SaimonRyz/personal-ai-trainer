"""Add confirmed E-G03 e1RM progress points; volume remains transient."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_e_g03_progress"
down_revision: str | None = "0015_e_g02_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "02f0d60e335c0ddb6c461cd5d15d7099043d7fee13dbef2b92c3e42593242353"
SEED_CHECKSUM = "d6a411bce6e8311fff6b8b02effebcb4dd2dbf46bb1c1c130b99612a103eebd6"


def upgrade() -> None:
    op.execute(
        f"""
        DO $preflight$ BEGIN
          IF EXISTS(SELECT 1 FROM trainer.derived_method_catalog WHERE code='e1rm_brzycki'
                    AND (description <> 'Brzycki estimated one-repetition maximum v1'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.metric_kind_catalog WHERE code='estimated_1rm'
                    AND (description <> 'Estimated one-repetition maximum'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='derived_progress_point'
                    AND (description <> 'Derived progress point'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='derived_value'
                    AND (description <> 'Derived calculation value'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='calculated_best'
                    AND (description <> 'Calculated best result'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='actual_set'
                    AND (description <> 'Actual set'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='actual_load_component'
                    AND (description <> 'Actual load component'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.intent_type_catalog WHERE code='calculation'
                    AND (description <> 'Confirmed calculation draft'
                         OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.unit_catalog WHERE code='kilogram'
                    AND (description <> 'Kilogram' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.unit_catalog WHERE code='pound'
                    AND (description <> 'Pound' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.unit_catalog WHERE code='kilogram_repetition'
                    AND (description <> 'Kilogram-repetition' OR seed_version <> 1 OR archived_at IS NOT NULL))
          THEN RAISE EXCEPTION 'E-G03 exact seed conflict'; END IF;
        END $preflight$;

        INSERT INTO trainer.derived_method_catalog(code,description,seed_version,created_at)
        VALUES ('e1rm_brzycki','Brzycki estimated one-repetition maximum v1',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at)
        VALUES ('estimated_1rm','Estimated one-repetition maximum',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at)
        VALUES ('derived_progress_point','Derived progress point',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at)
        VALUES ('derived_value','Derived calculation value',1,now()),
               ('calculated_best','Calculated best result',1,now()),
               ('actual_set','Actual set',1,now()),
               ('actual_load_component','Actual load component',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('calculation','Confirmed calculation draft',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.unit_catalog(code,description,seed_version,created_at)
        VALUES ('kilogram','Kilogram',1,now()),('pound','Pound',1,now()),
               ('kilogram_repetition','Kilogram-repetition',1,now()) ON CONFLICT DO NOTHING;

        DO $seed_check$ BEGIN
          IF NOT EXISTS(SELECT 1 FROM trainer.derived_method_catalog WHERE code='e1rm_brzycki'
                        AND description='Brzycki estimated one-repetition maximum v1'
                        AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.metric_kind_catalog WHERE code='estimated_1rm'
                        AND description='Estimated one-repetition maximum'
                        AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='derived_progress_point'
                        AND description='Derived progress point' AND seed_version=1 AND archived_at IS NULL)
             OR (SELECT count(*) FROM trainer.domain_object_type_catalog WHERE code IN
                        ('derived_value','calculated_best','actual_set','actual_load_component')
                        AND seed_version=1 AND archived_at IS NULL) <> 4
             OR NOT EXISTS(SELECT 1 FROM trainer.intent_type_catalog WHERE code='calculation'
                        AND description='Confirmed calculation draft' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.unit_catalog WHERE code='kilogram'
                        AND description='Kilogram' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.unit_catalog WHERE code='pound'
                        AND description='Pound' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.unit_catalog WHERE code='kilogram_repetition'
                        AND description='Kilogram-repetition' AND seed_version=1 AND archived_at IS NULL)
          THEN RAISE EXCEPTION 'E-G03 exact seed verification failed'; END IF;
        END $seed_check$;

        CREATE TABLE trainer.derived_progress_points (
          derived_value_id uuid PRIMARY KEY REFERENCES trainer.derived_values(derived_value_id) ON DELETE RESTRICT,
          method_id text NOT NULL REFERENCES trainer.derived_method_catalog(code) ON DELETE RESTRICT,
          method_version text NOT NULL CHECK(method_version='1'),
          metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT
            CHECK(metric_kind='estimated_1rm'),
          exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
          exercise_revision_id uuid NOT NULL,
          condition_id uuid NOT NULL REFERENCES trainer.comparison_conditions(condition_id) ON DELETE RESTRICT,
          session_id uuid NOT NULL REFERENCES trainer.workout_sessions(session_id) ON DELETE RESTRICT,
          session_revision_id uuid NOT NULL REFERENCES trainer.workout_session_revisions(session_revision_id) ON DELETE RESTRICT,
          source_actual_set_id uuid NOT NULL REFERENCES trainer.actual_sets(actual_set_id) ON DELETE RESTRICT,
          completeness text NOT NULL CHECK(completeness='complete'),
          included_count integer NOT NULL CHECK(included_count >= 1),
          excluded_count integer NOT NULL CHECK(excluded_count >= 0),
          input_digest char(64) NOT NULL CHECK(input_digest ~ '^[0-9a-f]{{64}}$'),
          result_digest char(64) NOT NULL CHECK(result_digest ~ '^[0-9a-f]{{64}}$'),
          created_at timestamptz NOT NULL,
          CONSTRAINT fk_progress_exact_exercise_revision FOREIGN KEY(exercise_id,exercise_revision_id)
            REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id) ON DELETE RESTRICT,
          CONSTRAINT fk_progress_exact_session_revision FOREIGN KEY(session_id,session_revision_id)
            REFERENCES trainer.workout_session_revisions(session_id,session_revision_id) ON DELETE RESTRICT,
          CONSTRAINT uq_progress_source_condition UNIQUE(method_id,method_version,source_actual_set_id,condition_id)
        );
        CREATE INDEX ix_progress_history ON trainer.derived_progress_points
          (exercise_id,metric_kind,condition_id,session_id,session_revision_id);
        CREATE INDEX ix_progress_source ON trainer.derived_progress_points
          (source_actual_set_id,session_revision_id);

        CREATE FUNCTION trainer.guard_derived_progress_point() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE v trainer.derived_values%ROWTYPE;
        BEGIN
          SELECT * INTO v FROM trainer.derived_values WHERE derived_value_id=NEW.derived_value_id;
          IF v.derived_value_id IS NULL OR v.method_id IS DISTINCT FROM NEW.method_id
             OR v.method_version IS DISTINCT FROM NEW.method_version
             OR v.unit_code IS DISTINCT FROM 'kilogram'
             OR v.persistence_reason IS DISTINCT FROM 'progress'
             OR v.state IS DISTINCT FROM 'current'
             OR v.assumptions->>'metric_kind' IS DISTINCT FROM NEW.metric_kind
             OR v.assumptions->>'input_digest' IS DISTINCT FROM NEW.input_digest
             OR v.assumptions->>'result_digest' IS DISTINCT FROM NEW.result_digest
          THEN RAISE EXCEPTION 'derived progress point does not match confirmed calculation'; END IF;
          IF NOT EXISTS(SELECT 1 FROM trainer.derived_value_inputs i
             WHERE i.derived_value_id=NEW.derived_value_id AND i.source_object_type='actual_set'
               AND i.source_object_id=NEW.source_actual_set_id
               AND i.source_revision_id=NEW.session_revision_id)
          THEN RAISE EXCEPTION 'derived progress point requires exact source input'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_derived_progress_point_guard BEFORE INSERT OR UPDATE
          ON trainer.derived_progress_points FOR EACH ROW
          EXECUTE FUNCTION trainer.guard_derived_progress_point();
        GRANT SELECT,INSERT ON trainer.derived_progress_points TO trainer_app;
        REVOKE UPDATE,DELETE ON trainer.derived_progress_points FROM trainer_app;

        CREATE OR REPLACE VIEW trainer.v_best_results AS
          SELECT r.profile_id,r.exercise_id,'actual_record'::text AS category,r.metric_kind,r.value,r.unit_code,
                 r.achieved_on AS result_date,r.session_id,r.session_revision_id,r.actual_set_id,
                 NULL::uuid AS source_derived_value_id,r.condition_id,r.is_current,
                 r.is_historical_import AS is_historical,r.record_id AS source_actual_record_id
          FROM trainer.actual_records r
          UNION ALL
          SELECT b.profile_id,b.exercise_id,'calculated_best'::text,b.metric_kind,b.value,b.unit_code,
                 b.achieved_on,b.source_session_id,b.source_session_revision_id,b.source_actual_set_id,
                 b.derived_value_id,b.condition_id,
                 (b.is_current AND d.state='current') AS is_current,
                 NOT (b.is_current AND d.state='current') AS is_historical,
                 NULL::uuid AS source_actual_record_id
          FROM trainer.calculated_bests b JOIN trainer.derived_values d
            ON d.derived_value_id=b.derived_value_id
          WHERE EXISTS(SELECT 1 FROM trainer.derived_progress_points p
                       WHERE p.derived_value_id=b.derived_value_id);

        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('e_g03_progress',1,'{SEED_CHECKSUM}',now(),now());
        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0016_e_g03_progress','{MIGRATION_CHECKSUM}',now(),0,'16',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("E-G03 migration is forward-only")
