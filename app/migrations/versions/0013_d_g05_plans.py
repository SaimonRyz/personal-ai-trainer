"""Enable D-G05 plan snapshots, lifecycle transitions and exact plan type."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_d_g05_plans"
down_revision: str | None = "0012_d_g04_seed_exactness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
MIGRATION_CHECKSUM = "224fd19ca69cbde497fa9e7da9e05bf1dc7ce9ae7f884550d89c580043e8050e"
SEED_CHECKSUM = "5ae7a0797b9a2419c8da5b6edbcff727411a6294ff136b8892e1fed14c3fa036"


def upgrade() -> None:
    # Owner approved exactly one plan type: training / Training session plan.
    # Legacy plans cannot safely receive JCS digests without their original exact
    # dependency snapshots, so fail closed before any schema or catalog mutation.
    op.execute(
        f"""
        DO $preflight$ BEGIN
          IF EXISTS(SELECT 1 FROM trainer.workout_plans)
             OR EXISTS(SELECT 1 FROM trainer.workout_plan_revisions)
             OR EXISTS(SELECT 1 FROM trainer.plan_type_catalog WHERE archived_at IS NULL AND code <> 'training')
             OR EXISTS(SELECT 1 FROM trainer.plan_type_catalog WHERE code='training'
                       AND (description <> 'Training session plan' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.intent_type_catalog WHERE code IN ('plan','plan_lifecycle')
                       AND (seed_version <> 1 OR archived_at IS NOT NULL
                         OR (code='plan' AND description <> 'Training plan draft')
                         OR (code='plan_lifecycle' AND description <> 'Plan lifecycle draft')))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='workout_plan'
                       AND (description <> 'Workout plan' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.domain_operation_catalog WHERE code IN ('miss','reschedule')
                       AND (seed_version <> 1 OR archived_at IS NOT NULL
                         OR (code='miss' AND description <> 'Mark missed')
                         OR (code='reschedule' AND description <> 'Reschedule')))
             OR EXISTS(SELECT 1 FROM trainer.dependency_type_catalog WHERE code='workout_plan'
                       AND (description <> 'Workout plan revision' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.dependency_type_catalog WHERE code='equipment_item'
                       AND (description <> 'Equipment item revision' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR EXISTS(SELECT 1 FROM trainer.dependency_type_catalog WHERE code='training_place'
                       AND (description <> 'Training place revision' OR seed_version <> 1 OR archived_at IS NOT NULL))
          THEN RAISE EXCEPTION 'D-G05 requires empty legacy plan tables and exact compatible catalog rows'; END IF;
        END $preflight$;

        INSERT INTO trainer.plan_type_catalog(code,description,seed_version,created_at)
        VALUES ('training','Training session plan',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('plan','Training plan draft',1,now()),('plan_lifecycle','Plan lifecycle draft',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at)
        VALUES ('workout_plan','Workout plan',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.domain_operation_catalog(code,description,seed_version,created_at)
        VALUES ('miss','Mark missed',1,now()),('reschedule','Reschedule',1,now())
        ON CONFLICT DO NOTHING;
        INSERT INTO trainer.dependency_type_catalog(code,description,seed_version,created_at)
        VALUES ('workout_plan','Workout plan revision',1,now()),
               ('equipment_item','Equipment item revision',1,now()),
               ('training_place','Training place revision',1,now()) ON CONFLICT DO NOTHING;
        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('d_g05_plans',1,'{SEED_CHECKSUM}',now(),now()) ON CONFLICT DO NOTHING;

        DO $seed_check$ BEGIN
          IF (SELECT count(*) FROM trainer.plan_type_catalog WHERE archived_at IS NULL) <> 1
             OR NOT EXISTS(SELECT 1 FROM trainer.plan_type_catalog WHERE code='training'
                           AND description='Training session plan' AND seed_version=1 AND archived_at IS NULL)
             OR (SELECT count(*) FROM trainer.intent_type_catalog WHERE code IN ('plan','plan_lifecycle')
                 AND (code,description) IN (('plan','Training plan draft'),('plan_lifecycle','Plan lifecycle draft'))
                 AND seed_version=1 AND archived_at IS NULL) <> 2
             OR NOT EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code='workout_plan'
                           AND description='Workout plan' AND seed_version=1 AND archived_at IS NULL)
             OR (SELECT count(*) FROM trainer.domain_operation_catalog WHERE code IN ('miss','reschedule')
                 AND (code,description) IN (('miss','Mark missed'),('reschedule','Reschedule'))
                 AND seed_version=1 AND archived_at IS NULL) <> 2
             OR NOT EXISTS(SELECT 1 FROM trainer.dependency_type_catalog WHERE code='workout_plan'
                           AND description='Workout plan revision' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.dependency_type_catalog WHERE code='equipment_item'
                           AND description='Equipment item revision' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.dependency_type_catalog WHERE code='training_place'
                           AND description='Training place revision' AND seed_version=1 AND archived_at IS NULL)
             OR NOT EXISTS(SELECT 1 FROM trainer.seed_versions WHERE seed_name='d_g05_plans'
                           AND seed_version=1 AND checksum='{SEED_CHECKSUM}')
          THEN RAISE EXCEPTION 'D-G05 exact seed verification failed'; END IF;
        END $seed_check$;

        ALTER TABLE trainer.workout_plan_revisions
          ADD COLUMN context_snapshot_digest char(64) NOT NULL
            CHECK(context_snapshot_digest ~ '^[0-9a-f]{{64}}$'),
          ADD COLUMN content_digest char(64) NOT NULL
            CHECK(content_digest ~ '^[0-9a-f]{{64}}$');

        CREATE FUNCTION trainer.guard_plan_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE prior_lifecycle text; current_revision uuid; prior_revision_no integer;
        BEGIN
          IF NEW.supersedes_revision_id IS NULL THEN
            IF NEW.lifecycle <> 'planned' THEN
              RAISE EXCEPTION 'new plan must begin planned' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
          END IF;
          SELECT p.current_revision_id,r.lifecycle,r.revision_no
            INTO current_revision,prior_lifecycle,prior_revision_no
            FROM trainer.workout_plans p
            JOIN trainer.workout_plan_revisions r ON r.plan_id=p.plan_id
              AND r.plan_revision_id=NEW.supersedes_revision_id
            WHERE p.plan_id=NEW.plan_id FOR UPDATE OF p;
          IF current_revision IS DISTINCT FROM NEW.supersedes_revision_id
             OR prior_lifecycle <> 'planned'
             OR NEW.revision_no <> prior_revision_no + 1
             OR NEW.lifecycle NOT IN ('rescheduled','cancelled','missed') THEN
            RAISE EXCEPTION 'plan lifecycle transition requires current planned revision'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_plan_revision_transition
          BEFORE INSERT ON trainer.workout_plan_revisions
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_revision_insert();

        CREATE FUNCTION trainer.guard_plan_current_revision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id AND NOT EXISTS(
            SELECT 1 FROM trainer.workout_plan_revisions r
            WHERE r.plan_id=NEW.plan_id AND r.plan_revision_id=NEW.current_revision_id
              AND r.supersedes_revision_id=OLD.current_revision_id
          ) THEN
            RAISE EXCEPTION 'plan current revision must advance one lifecycle revision'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_plan_current_revision
          BEFORE UPDATE OF current_revision_id ON trainer.workout_plans
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_current_revision();

        CREATE FUNCTION trainer.guard_plan_revision_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'confirmed plan revisions are immutable' USING ERRCODE='23514';
        END $$;
        CREATE TRIGGER trg_plan_revision_immutable
          BEFORE UPDATE OR DELETE ON trainer.workout_plan_revisions
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_revision_immutable();

        -- Confirmed plan descendants may only be inserted by the same
        -- confirmation transaction that creates their owning revision. The
        -- shared metric/recommendation tables remain writable for templates and
        -- programs under the existing D-G04 guard.
        CREATE FUNCTION trainer.guard_plan_tree_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE v_revision_id uuid; v_confirmation_id uuid; same_tx boolean;
        BEGIN
          CASE TG_TABLE_NAME
          WHEN 'planned_blocks' THEN v_revision_id := NEW.plan_revision_id;
          WHEN 'planned_exercises' THEN
            SELECT b.plan_revision_id INTO v_revision_id FROM trainer.planned_blocks b
              WHERE b.planned_block_id=NEW.planned_block_id;
          WHEN 'planned_sets' THEN
            SELECT b.plan_revision_id INTO v_revision_id FROM trainer.planned_exercises e
              JOIN trainer.planned_blocks b USING(planned_block_id)
              WHERE e.planned_exercise_id=NEW.planned_exercise_id;
          WHEN 'planned_metric_values' THEN
            IF NEW.planned_set_id IS NULL THEN RETURN NEW; END IF;
            SELECT b.plan_revision_id INTO v_revision_id FROM trainer.planned_sets s
              JOIN trainer.planned_exercises e USING(planned_exercise_id)
              JOIN trainer.planned_blocks b USING(planned_block_id)
              WHERE s.planned_set_id=NEW.planned_set_id;
          WHEN 'planned_load_components' THEN
            SELECT b.plan_revision_id INTO v_revision_id FROM trainer.planned_sets s
              JOIN trainer.planned_exercises e USING(planned_exercise_id)
              JOIN trainer.planned_blocks b USING(planned_block_id)
              WHERE s.planned_set_id=NEW.planned_set_id;
          WHEN 'recommendations' THEN
            IF NEW.plan_revision_id IS NULL THEN RETURN NEW; END IF;
            v_revision_id := NEW.plan_revision_id;
          ELSE
            SELECT r.plan_revision_id INTO v_revision_id FROM trainer.recommendations r
              WHERE r.recommendation_id=NEW.recommendation_id;
          END CASE;
          IF v_revision_id IS NULL THEN RETURN NEW; END IF;
          SELECT r.confirmation_id INTO v_confirmation_id FROM trainer.workout_plan_revisions r
            WHERE r.plan_revision_id=v_revision_id;
          SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
            INTO same_tx FROM trainer.confirmations c WHERE c.confirmation_id=v_confirmation_id;
          IF same_tx IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'confirmed plan tree cannot be extended' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.planned_blocks
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.planned_exercises
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.planned_sets
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.planned_metric_values
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.planned_load_components
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.recommendations
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.recommendation_fact_links
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.recommendation_calculation_links
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.recommendation_conclusion_links
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        CREATE TRIGGER trg_plan_tree_insert BEFORE INSERT ON trainer.recommendation_explanations
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_tree_insert();
        REVOKE UPDATE,DELETE ON trainer.planned_blocks,trainer.planned_exercises,trainer.planned_sets,
          trainer.planned_metric_values,trainer.planned_load_components,trainer.recommendations,
          trainer.recommendation_fact_links,trainer.recommendation_calculation_links,
          trainer.recommendation_conclusion_links,trainer.recommendation_explanations FROM trainer_app;

        CREATE FUNCTION trainer.guard_plan_reschedule_link() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_profile uuid; replacement_profile uuid; source_state text; replacement_state text;
        BEGIN
          IF NEW.source_plan_id=NEW.replacement_plan_id THEN
            RAISE EXCEPTION 'reschedule link cannot reference itself' USING ERRCODE='23514';
          END IF;
          IF EXISTS(SELECT 1 FROM trainer.plan_reschedule_links l
                    WHERE l.replacement_plan_id=NEW.replacement_plan_id) THEN
            RAISE EXCEPTION 'replacement plan is already linked' USING ERRCODE='23514';
          END IF;
          IF EXISTS(
            WITH RECURSIVE path(plan_id) AS (
              SELECT NEW.replacement_plan_id
              UNION
              SELECT l.replacement_plan_id FROM trainer.plan_reschedule_links l
                JOIN path p ON l.source_plan_id=p.plan_id
            ) SELECT 1 FROM path WHERE plan_id=NEW.source_plan_id
          ) THEN
            RAISE EXCEPTION 'reschedule link would create a cycle' USING ERRCODE='23514';
          END IF;
          SELECT p.profile_id,r.lifecycle INTO source_profile,source_state
            FROM trainer.workout_plans p JOIN trainer.workout_plan_revisions r
              ON r.plan_id=p.plan_id AND r.plan_revision_id=p.current_revision_id
            WHERE p.plan_id=NEW.source_plan_id FOR UPDATE OF p;
          SELECT p.profile_id,r.lifecycle INTO replacement_profile,replacement_state
            FROM trainer.workout_plans p JOIN trainer.workout_plan_revisions r
              ON r.plan_id=p.plan_id AND r.plan_revision_id=p.current_revision_id
            WHERE p.plan_id=NEW.replacement_plan_id FOR UPDATE OF p;
          IF source_profile IS NULL OR replacement_profile IS NULL
             OR source_profile <> replacement_profile OR source_state <> 'rescheduled'
             OR replacement_state <> 'planned' THEN
            RAISE EXCEPTION 'reschedule link must connect same-profile rescheduled and planned plans'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_plan_reschedule_link
          BEFORE INSERT ON trainer.plan_reschedule_links
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_plan_reschedule_link();

        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0013_d_g05_plans','{MIGRATION_CHECKSUM}',now(),0,'13',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G05 plan migration is forward-only")
