"""Add exact exercise pins and workout report compatibility for D-G06."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_d_g06_workouts"
down_revision: str | None = "0013_d_g05_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
MIGRATION_CHECKSUM = "926137b8805d032c7fd34d4397a38965fdcf1e5c337190f1662491a36639c72f"
SEED_CHECKSUM = "2bf7c548c1b8b9feac4e98ec36c9b185445ae9cafec8d607022eebd296249cc4"


def upgrade() -> None:
    op.execute(
        f"""
        DO $preflight$ BEGIN
          IF EXISTS(SELECT 1 FROM trainer.workout_sessions)
             OR EXISTS(SELECT 1 FROM trainer.workout_session_revisions)
             OR EXISTS(SELECT 1 FROM trainer.actual_blocks)
             OR EXISTS(SELECT 1 FROM trainer.exercise_executions)
             OR EXISTS(SELECT 1 FROM trainer.replacement_links)
             OR EXISTS(SELECT 1 FROM trainer.actual_sets)
             OR EXISTS(SELECT 1 FROM trainer.actual_metric_values)
             OR EXISTS(SELECT 1 FROM trainer.actual_load_components)
             OR EXISTS(SELECT 1 FROM trainer.workout_metrics)
             OR EXISTS(SELECT 1 FROM trainer.actual_set_status_catalog
                       WHERE archived_at IS NULL AND code NOT IN ('performed','interrupted','skipped'))
             OR EXISTS(SELECT 1 FROM trainer.actual_set_status_catalog WHERE code IN ('performed','interrupted','skipped')
                       AND (archived_at IS NOT NULL OR seed_version <> 1
                         OR (code='performed' AND description <> 'Set performed')
                         OR (code='interrupted' AND description <> 'Set interrupted')
                         OR (code='skipped' AND description <> 'Skipped planned set')))
             OR EXISTS(SELECT 1 FROM trainer.session_type_catalog WHERE code='training'
                       AND (archived_at IS NOT NULL OR seed_version <> 1 OR description <> 'Training session'))
             OR EXISTS(SELECT 1 FROM trainer.session_type_catalog WHERE archived_at IS NULL
                       AND code <> 'training')
             OR EXISTS(SELECT 1 FROM trainer.intent_type_catalog WHERE code='workout_report'
                       AND (archived_at IS NOT NULL OR seed_version <> 1 OR description <> 'Workout report draft'))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='workout_session'
                       AND (archived_at IS NOT NULL OR seed_version <> 1 OR description <> 'Workout session'))
             OR EXISTS(SELECT 1 FROM trainer.unit_catalog WHERE code IN ('beats_per_minute','kilocalorie')
                       AND (archived_at IS NOT NULL OR seed_version <> 1
                         OR (code='beats_per_minute' AND description <> 'Beats per minute')
                         OR (code='kilocalorie' AND description <> 'Kilocalorie')))
             OR EXISTS(SELECT 1 FROM trainer.unit_family_catalog WHERE code IN ('frequency','energy')
                       AND (archived_at IS NOT NULL OR seed_version <> 1
                         OR (code='frequency' AND description <> 'Frequency')
                         OR (code='energy' AND description <> 'Energy')))
             OR EXISTS(SELECT 1 FROM trainer.metric_kind_catalog WHERE code IN
                         ('heart_rate_min','heart_rate_avg','heart_rate_max','calories_active','calories_total')
                       AND (archived_at IS NOT NULL OR seed_version <> 1
                         OR (code='heart_rate_min' AND description <> 'Minimum heart rate')
                         OR (code='heart_rate_avg' AND description <> 'Average heart rate')
                         OR (code='heart_rate_max' AND description <> 'Maximum heart rate')
                         OR (code='calories_active' AND description <> 'Active calories')
                         OR (code='calories_total' AND description <> 'Total calories')))
             OR EXISTS(SELECT 1 FROM trainer.unit_family_mapping WHERE unit_code IN ('beats_per_minute','kilocalorie')
                       AND ((unit_code='beats_per_minute' AND family_code <> 'frequency')
                         OR (unit_code='kilocalorie' AND family_code <> 'energy')))
             OR EXISTS(SELECT 1 FROM trainer.load_kind_catalog WHERE archived_at IS NULL
                       AND code NOT IN ('external','bodyweight','band'))
             OR EXISTS(SELECT 1 FROM trainer.load_kind_catalog WHERE code IN ('external','bodyweight','band')
                       AND (archived_at IS NOT NULL OR seed_version <> 1
                         OR (code='external' AND description <> 'External load')
                         OR (code='bodyweight' AND description <> 'Bodyweight')
                         OR (code='band' AND description <> 'Resistance band')))
             OR EXISTS(SELECT 1 FROM trainer.load_direction_catalog WHERE archived_at IS NULL
                       AND code NOT IN ('assistance','resistance'))
             OR EXISTS(SELECT 1 FROM trainer.load_direction_catalog WHERE code IN ('assistance','resistance')
                       AND (archived_at IS NOT NULL OR seed_version <> 1
                         OR (code='assistance' AND description <> 'Assistance')
                         OR (code='resistance' AND description <> 'Resistance')))
             OR EXISTS(SELECT 1 FROM trainer.planned_load_components
                       WHERE load_kind NOT IN ('external','bodyweight','band')
                          OR (load_kind='band' AND direction IS NULL)
                          OR (load_kind IN ('external','bodyweight') AND direction IS NOT NULL))
          THEN RAISE EXCEPTION 'D-G06 preflight or exact seed conflict'; END IF;
        END $preflight$;

        INSERT INTO trainer.actual_set_status_catalog(code,description,seed_version,created_at)
        VALUES ('performed','Set performed',1,now()),('interrupted','Set interrupted',1,now()),
               ('skipped','Skipped planned set',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.session_type_catalog(code,description,seed_version,created_at)
        VALUES ('training','Training session',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('workout_report','Workout report draft',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at)
        VALUES ('workout_session','Workout session',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.load_kind_catalog(code,description,seed_version,created_at)
        VALUES ('external','External load',1,now()),('bodyweight','Bodyweight',1,now()),
               ('band','Resistance band',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.load_direction_catalog(code,description,seed_version,created_at)
        VALUES ('assistance','Assistance',1,now()),('resistance','Resistance',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at)
        VALUES ('heart_rate_min','Minimum heart rate',1,now()),('heart_rate_avg','Average heart rate',1,now()),
               ('heart_rate_max','Maximum heart rate',1,now()),('calories_active','Active calories',1,now()),
               ('calories_total','Total calories',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.unit_family_catalog(code,description,seed_version,created_at)
        VALUES ('frequency','Frequency',1,now()),('energy','Energy',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.unit_catalog(code,description,seed_version,created_at)
        VALUES ('beats_per_minute','Beats per minute',1,now()),('kilocalorie','Kilocalorie',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.unit_family_mapping(unit_code,family_code)
        VALUES ('beats_per_minute','frequency'),('kilocalorie','energy') ON CONFLICT DO NOTHING;

        DO $seed_check$ BEGIN
          IF (SELECT count(*) FROM trainer.actual_set_status_catalog WHERE archived_at IS NULL) <> 3
             OR (SELECT count(*) FROM trainer.actual_set_status_catalog WHERE
                 (code,description,seed_version) IN (('performed','Set performed',1),
                   ('interrupted','Set interrupted',1),('skipped','Skipped planned set',1)) AND archived_at IS NULL) <> 3
             OR NOT EXISTS(SELECT 1 FROM trainer.session_type_catalog WHERE code='training'
                   AND description='Training session' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.intent_type_catalog WHERE code='workout_report'
                   AND description='Workout report draft' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='workout_session'
                   AND description='Workout session' AND seed_version=1 AND archived_at IS NULL)
             OR (SELECT count(*) FROM trainer.load_kind_catalog WHERE code IN ('external','bodyweight','band')
                   AND archived_at IS NULL AND seed_version=1) <> 3
             OR (SELECT count(*) FROM trainer.load_direction_catalog WHERE code IN ('assistance','resistance')
                   AND archived_at IS NULL AND seed_version=1) <> 2
             OR (SELECT count(*) FROM trainer.metric_kind_catalog WHERE code IN
                   ('heart_rate_min','heart_rate_avg','heart_rate_max','calories_active','calories_total')
                   AND archived_at IS NULL AND seed_version=1) <> 5
             OR (SELECT count(*) FROM trainer.unit_catalog WHERE code IN ('beats_per_minute','kilocalorie')
                   AND archived_at IS NULL AND seed_version=1) <> 2
             OR (SELECT count(*) FROM trainer.unit_family_catalog WHERE code IN ('frequency','energy')
                   AND archived_at IS NULL AND seed_version=1) <> 2
             OR NOT EXISTS(SELECT 1 FROM trainer.unit_family_mapping WHERE unit_code='beats_per_minute' AND family_code='frequency')
             OR NOT EXISTS(SELECT 1 FROM trainer.unit_family_mapping WHERE unit_code='kilocalorie' AND family_code='energy')
          THEN RAISE EXCEPTION 'D-G06 exact seed verification failed'; END IF;
        END $seed_check$;

        CREATE TABLE trainer.workout_metric_compatibility (
          metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
          unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
          created_at timestamptz NOT NULL,
          PRIMARY KEY(metric_kind,unit_code)
        );
        INSERT INTO trainer.workout_metric_compatibility(metric_kind,unit_code,created_at)
        VALUES ('heart_rate_min','beats_per_minute',now()),('heart_rate_avg','beats_per_minute',now()),
               ('heart_rate_max','beats_per_minute',now()),('calories_active','kilocalorie',now()),
               ('calories_total','kilocalorie',now()),('distance','meter',now());
        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('d_g06_workouts',1,'{SEED_CHECKSUM}',now(),now()) ON CONFLICT DO NOTHING;
        DO $seed_version_check$ BEGIN
          IF NOT EXISTS(SELECT 1 FROM trainer.seed_versions WHERE seed_name='d_g06_workouts'
                        AND seed_version=1 AND checksum='{SEED_CHECKSUM}')
             OR (SELECT count(*) FROM trainer.workout_metric_compatibility) <> 6
          THEN RAISE EXCEPTION 'D-G06 compatibility seed conflict'; END IF;
        END $seed_version_check$;

        ALTER TABLE trainer.exercise_executions ADD COLUMN exercise_revision_id uuid;
        ALTER TABLE trainer.exercise_executions ALTER COLUMN exercise_revision_id SET NOT NULL;
        ALTER TABLE trainer.exercise_executions ADD CONSTRAINT fk_execution_exercise_revision
          FOREIGN KEY(exercise_id,exercise_revision_id)
          REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id) ON DELETE RESTRICT;
        ALTER TABLE trainer.replacement_links DROP CONSTRAINT replacement_links_check;
        ALTER TABLE trainer.replacement_links DROP COLUMN source_planned_exercise_id;
        ALTER TABLE trainer.replacement_links ALTER COLUMN skipped_execution_id SET NOT NULL;
        ALTER TABLE trainer.workout_session_revisions ADD COLUMN report_gaps jsonb NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE trainer.workout_session_revisions ADD CONSTRAINT ck_workout_report_gaps_array
          CHECK(jsonb_typeof(report_gaps)='array');
        ALTER TABLE trainer.actual_metric_values DROP CONSTRAINT actual_metric_values_source_kind_check;
        ALTER TABLE trainer.actual_metric_values ADD CONSTRAINT ck_actual_metric_fact_source
          CHECK(source_kind IN ('user','system','curator','external_metadata'));
        ALTER TABLE trainer.workout_metrics ADD CONSTRAINT ck_workout_metric_source_derived
          CHECK((source_kind='calculation') = (derived_value_id IS NOT NULL));
        ALTER TABLE trainer.workout_metrics ADD CONSTRAINT fk_workout_metric_compatibility
          FOREIGN KEY(metric_kind,unit_code)
          REFERENCES trainer.workout_metric_compatibility(metric_kind,unit_code) ON DELETE RESTRICT;
        ALTER TABLE trainer.planned_load_components ADD CONSTRAINT ck_planned_load_kind_direction
          CHECK ((load_kind='band' AND direction IS NOT NULL
                  AND direction IN ('assistance','resistance'))
              OR (load_kind IN ('external','bodyweight') AND direction IS NULL));
        ALTER TABLE trainer.actual_load_components ADD CONSTRAINT ck_actual_load_kind_direction
          CHECK ((load_kind='band' AND direction IS NOT NULL
                  AND direction IN ('assistance','resistance'))
              OR (load_kind IN ('external','bodyweight') AND direction IS NULL));
        ALTER TABLE trainer.actual_sets ADD CONSTRAINT ck_actual_set_side
          CHECK(side IS NULL OR side IN ('left','right','both','not_applicable'));

        CREATE FUNCTION trainer.guard_workout_session_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE same_tx boolean; v_confirmation_id uuid; v_owner uuid;
        BEGIN
          IF TG_OP='INSERT' THEN
            SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
              INTO same_tx FROM trainer.confirmations c
              WHERE c.draft_revision_id=NEW.current_revision_id;
          ELSE
            SELECT r.confirmation_id,r.session_id INTO v_confirmation_id,v_owner
              FROM trainer.workout_session_revisions r
              WHERE r.session_revision_id=NEW.current_revision_id;
            IF v_owner IS DISTINCT FROM OLD.session_id OR NEW.session_id IS DISTINCT FROM OLD.session_id
               OR NEW.current_revision_id=OLD.current_revision_id THEN
              RAISE EXCEPTION 'workout current revision must advance within its owner' USING ERRCODE='23514';
            END IF;
            SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
              INTO same_tx FROM trainer.confirmations c
              WHERE c.confirmation_id=v_confirmation_id;
          END IF;
          IF same_tx IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'workout session requires same-transaction confirmation' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_workout_session_insert BEFORE INSERT ON trainer.workout_sessions
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_session_insert();
        CREATE TRIGGER trg_workout_session_pointer BEFORE UPDATE OF current_revision_id ON trainer.workout_sessions
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_session_insert();

        CREATE FUNCTION trainer.guard_workout_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE same_tx boolean;
        BEGIN
          SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
            INTO same_tx FROM trainer.confirmations c
            WHERE c.confirmation_id=NEW.confirmation_id AND c.draft_revision_id=NEW.session_revision_id;
          IF same_tx IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'workout revision requires same-transaction confirmation' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_workout_revision_insert BEFORE INSERT ON trainer.workout_session_revisions
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_revision_insert();
        REVOKE UPDATE,DELETE ON trainer.workout_session_revisions FROM trainer_app;

        CREATE FUNCTION trainer.guard_workout_tree_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE v_revision_id uuid; v_confirmation_id uuid; same_tx boolean;
        BEGIN
          CASE TG_TABLE_NAME
            WHEN 'actual_blocks' THEN v_revision_id := NEW.session_revision_id;
            WHEN 'workout_metrics' THEN v_revision_id := NEW.session_revision_id;
            WHEN 'exercise_executions' THEN
              SELECT session_revision_id INTO v_revision_id FROM trainer.actual_blocks
                WHERE actual_block_id=NEW.actual_block_id;
            WHEN 'replacement_links' THEN
              SELECT b.session_revision_id INTO v_revision_id FROM trainer.exercise_executions e
                JOIN trainer.actual_blocks b USING(actual_block_id) WHERE e.execution_id=NEW.added_execution_id;
            WHEN 'actual_sets' THEN
              SELECT b.session_revision_id INTO v_revision_id FROM trainer.exercise_executions e
                JOIN trainer.actual_blocks b USING(actual_block_id) WHERE e.execution_id=NEW.execution_id;
            WHEN 'actual_metric_values' THEN
              SELECT b.session_revision_id INTO v_revision_id FROM trainer.actual_sets s
                JOIN trainer.exercise_executions e USING(execution_id)
                JOIN trainer.actual_blocks b USING(actual_block_id) WHERE s.actual_set_id=NEW.actual_set_id;
            WHEN 'actual_load_components' THEN
              SELECT b.session_revision_id INTO v_revision_id FROM trainer.actual_sets s
                JOIN trainer.exercise_executions e USING(execution_id)
                JOIN trainer.actual_blocks b USING(actual_block_id) WHERE s.actual_set_id=NEW.actual_set_id;
            ELSE RAISE EXCEPTION 'unsupported workout tree table %',TG_TABLE_NAME USING ERRCODE='23514';
          END CASE;
          SELECT confirmation_id INTO v_confirmation_id FROM trainer.workout_session_revisions
            WHERE session_revision_id=v_revision_id;
          SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
            INTO same_tx FROM trainer.confirmations c WHERE c.confirmation_id=v_confirmation_id;
          IF same_tx IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'confirmed workout tree cannot be extended' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_workout_tree_insert BEFORE INSERT ON trainer.actual_blocks
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_tree_insert();
        CREATE TRIGGER trg_workout_tree_insert BEFORE INSERT ON trainer.exercise_executions
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_tree_insert();
        CREATE TRIGGER trg_workout_tree_insert BEFORE INSERT ON trainer.replacement_links
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_tree_insert();
        CREATE TRIGGER trg_workout_tree_insert BEFORE INSERT ON trainer.actual_sets
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_tree_insert();
        CREATE TRIGGER trg_workout_tree_insert BEFORE INSERT ON trainer.actual_metric_values
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_tree_insert();
        CREATE TRIGGER trg_workout_tree_insert BEFORE INSERT ON trainer.actual_load_components
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_tree_insert();
        CREATE TRIGGER trg_workout_tree_insert BEFORE INSERT ON trainer.workout_metrics
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_workout_tree_insert();
        REVOKE UPDATE,DELETE ON trainer.workout_sessions,trainer.actual_blocks,trainer.exercise_executions,
          trainer.replacement_links,trainer.actual_sets,trainer.actual_metric_values,
          trainer.actual_load_components,trainer.workout_metrics FROM trainer_app;
        GRANT UPDATE(current_revision_id,archived_at,logically_deleted_at)
          ON trainer.workout_sessions TO trainer_app;
        GRANT SELECT ON trainer.workout_metric_compatibility TO trainer_app;
        REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON trainer.workout_metric_compatibility FROM trainer_app;
        REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON trainer.load_kind_catalog,trainer.load_direction_catalog,
            trainer.session_type_catalog,trainer.actual_set_status_catalog FROM trainer_app;

        INSERT INTO trainer.schema_migrations(
          migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0014_d_g06_workouts','{MIGRATION_CHECKSUM}',now(),0,'14',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G06 migration is forward-only")
