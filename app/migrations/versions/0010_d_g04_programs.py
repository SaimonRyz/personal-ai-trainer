"""Enable the approved D-G04 immutable program aggregate."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_d_g04_programs"
down_revision: str | None = "0009_d_g03_validation_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
MIGRATION_CHECKSUM = "1c0805bd61031d62044e72fc8d02724e13ac38b2f178b657b032c0a9a30d3e6a"


def upgrade() -> None:
    op.execute(f"""
    DO $preflight$ BEGIN
      IF EXISTS(SELECT 1 FROM trainer.programs) OR EXISTS(SELECT 1 FROM trainer.program_versions)
         OR EXISTS(SELECT 1 FROM trainer.day_templates) OR EXISTS(SELECT 1 FROM trainer.template_blocks)
         OR EXISTS(SELECT 1 FROM trainer.template_exercises) OR EXISTS(SELECT 1 FROM trainer.template_sets)
      THEN RAISE EXCEPTION 'D-G04 requires empty legacy program schema'; END IF;
    END $preflight$;

    INSERT INTO trainer.program_status_catalog(code,description,seed_version,created_at) VALUES ('active','Active program',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.day_type_catalog(code,description,seed_version,created_at) VALUES ('training','Training day',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.block_type_catalog(code,description,seed_version,created_at) VALUES ('straight','Straight sets',1,now()),('superset','Superset',1,now()),('circuit','Circuit',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.set_type_catalog(code,description,seed_version,created_at) VALUES ('warmup','Warm-up',1,now()),('working','Working',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.recommendation_kind_catalog(code,description,seed_version,created_at) VALUES ('program_design','Program design',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.unit_catalog(code,description,seed_version,created_at) VALUES ('second','Second',1,now()),('meter','Meter',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at) VALUES ('program','Program draft',1,now()),('program_publication','Program publication',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.dependency_type_catalog(code,description,seed_version,created_at) VALUES ('program','Program',1,now()),('program_version','Program version',1,now()),('exercise_revision','Exercise revision',1,now()),('equipment_availability','Equipment availability',1,now()),('conclusion_version','Conclusion version',1,now()),('derived_value','Derived value',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at) VALUES ('program','Program',1,now()),('program_version','Program version',1,now()),('day_template','Day template',1,now()),('template_block','Template block',1,now()),('template_exercise','Template exercise',1,now()),('template_set','Template set',1,now()),('recommendation','Recommendation',1,now()) ON CONFLICT DO NOTHING;
    INSERT INTO trainer.domain_operation_catalog(code,description,seed_version,created_at) VALUES ('publish','Publish',1,now()),('retire','Retire',1,now()) ON CONFLICT DO NOTHING;

    ALTER TABLE trainer.programs ADD COLUMN current_version_id uuid NULL;
    ALTER TABLE trainer.programs ALTER COLUMN starts_on SET NOT NULL;
    ALTER TABLE trainer.programs ALTER COLUMN ends_on SET NOT NULL;
    ALTER TABLE trainer.programs ADD CONSTRAINT uq_program_goal UNIQUE(program_id,goal_id);
    ALTER TABLE trainer.programs ADD CONSTRAINT uq_program_current UNIQUE(program_id,current_version_id);
    ALTER TABLE trainer.program_versions ADD COLUMN goal_id uuid NOT NULL;
    ALTER TABLE trainer.program_versions ADD COLUMN goal_revision_id uuid NOT NULL;
    ALTER TABLE trainer.program_versions ALTER COLUMN published_at SET NOT NULL;
    ALTER TABLE trainer.program_versions ALTER COLUMN confirmation_id SET NOT NULL;
    ALTER TABLE trainer.program_versions DROP CONSTRAINT program_versions_publication_state_check;
    ALTER TABLE trainer.program_versions ADD CONSTRAINT ck_program_version_state CHECK(publication_state IN ('published','retired'));
    ALTER TABLE trainer.program_versions ADD CONSTRAINT fk_program_goal FOREIGN KEY(program_id,goal_id) REFERENCES trainer.programs(program_id,goal_id) DEFERRABLE INITIALLY DEFERRED;
    ALTER TABLE trainer.program_versions ADD CONSTRAINT fk_program_goal_revision FOREIGN KEY(goal_id,goal_revision_id) REFERENCES trainer.goal_revisions(goal_id,goal_revision_id) DEFERRABLE INITIALLY DEFERRED;
    ALTER TABLE trainer.programs ADD CONSTRAINT fk_program_current FOREIGN KEY(program_id,current_version_id) REFERENCES trainer.program_versions(program_id,program_version_id) DEFERRABLE INITIALLY DEFERRED;
    CREATE UNIQUE INDEX ux_program_one_published ON trainer.program_versions(program_id) WHERE publication_state='published';
    ALTER TABLE trainer.template_exercises ADD COLUMN exercise_revision_id uuid NOT NULL;
    ALTER TABLE trainer.template_exercises ADD CONSTRAINT fk_template_exercise_revision FOREIGN KEY(exercise_id,exercise_revision_id) REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id);
    ALTER TABLE trainer.recommendation_conclusion_links ADD COLUMN status_revision_id uuid NOT NULL REFERENCES trainer.knowledge_status_revisions(status_revision_id) ON DELETE RESTRICT;
    ALTER TABLE trainer.knowledge_status_revisions ADD CONSTRAINT uq_knowledge_status_owner UNIQUE(conclusion_version_id,status_revision_id);
    ALTER TABLE trainer.recommendation_conclusion_links ADD CONSTRAINT fk_recommendation_status_owner FOREIGN KEY(conclusion_version_id,status_revision_id) REFERENCES trainer.knowledge_status_revisions(conclusion_version_id,status_revision_id);
    CREATE TABLE trainer.unit_family_mapping (
        unit_code text PRIMARY KEY REFERENCES trainer.unit_catalog(code),
        family_code text NOT NULL REFERENCES trainer.unit_family_catalog(code)
    );
    INSERT INTO trainer.unit_family_mapping(unit_code,family_code) VALUES ('repetition','count'),('second','time'),('meter','length');
    GRANT SELECT ON trainer.unit_family_mapping TO trainer_app;
    REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON trainer.unit_family_mapping FROM trainer_app;
    CREATE FUNCTION trainer.guard_program_bounds() RETURNS trigger LANGUAGE plpgsql AS $guard$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM trainer.programs p WHERE p.program_id=NEW.program_id AND p.goal_id=NEW.goal_id AND p.starts_on<=NEW.period_start AND p.ends_on>=NEW.period_end)
      THEN RAISE EXCEPTION 'program version period/goal ownership mismatch' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $guard$;
    CREATE TRIGGER trg_program_bounds BEFORE INSERT ON trainer.program_versions FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_bounds();
    CREATE FUNCTION trainer.guard_program_current() RETURNS trigger LANGUAGE plpgsql AS $guard$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM trainer.programs p JOIN trainer.program_versions v ON v.program_id=p.program_id AND v.program_version_id=p.current_version_id WHERE p.program_id=NEW.program_id AND v.publication_state='published')
      THEN RAISE EXCEPTION 'program current must be published' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $guard$;
    CREATE CONSTRAINT TRIGGER trg_program_current AFTER INSERT OR UPDATE ON trainer.programs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_current();
    CREATE FUNCTION trainer.guard_template_metric() RETURNS trigger LANGUAGE plpgsql AS $guard$
    BEGIN
      IF NEW.template_set_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM trainer.template_sets s JOIN trainer.template_exercises e USING(template_exercise_id)
        JOIN trainer.exercise_measurements m ON m.exercise_revision_id=e.exercise_revision_id AND m.metric_kind=NEW.metric_kind
        JOIN trainer.unit_family_mapping u ON u.unit_code=NEW.unit_code AND u.family_code=m.unit_family
        WHERE s.template_set_id=NEW.template_set_id
      ) THEN RAISE EXCEPTION 'incompatible template metric' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $guard$;
    CREATE TRIGGER trg_template_metric BEFORE INSERT ON trainer.planned_metric_values FOR EACH ROW EXECUTE FUNCTION trainer.guard_template_metric();
    CREATE FUNCTION trainer.guard_program_tree_insert() RETURNS trigger LANGUAGE plpgsql AS $guard$
    DECLARE version_id uuid; same_tx boolean;
    BEGIN
      CASE TG_TABLE_NAME
      WHEN 'day_templates' THEN version_id:=NEW.program_version_id;
      WHEN 'template_blocks' THEN SELECT d.program_version_id INTO version_id FROM trainer.day_templates d WHERE d.day_template_id=NEW.day_template_id;
      WHEN 'template_exercises' THEN SELECT d.program_version_id INTO version_id FROM trainer.template_blocks b JOIN trainer.day_templates d USING(day_template_id) WHERE b.template_block_id=NEW.template_block_id;
      WHEN 'template_sets' THEN SELECT d.program_version_id INTO version_id FROM trainer.template_exercises e JOIN trainer.template_blocks b USING(template_block_id) JOIN trainer.day_templates d USING(day_template_id) WHERE e.template_exercise_id=NEW.template_exercise_id;
      WHEN 'planned_metric_values' THEN
        IF NEW.template_set_id IS NULL THEN RETURN NEW; END IF;
        SELECT d.program_version_id INTO version_id FROM trainer.template_sets s JOIN trainer.template_exercises e USING(template_exercise_id) JOIN trainer.template_blocks b USING(template_block_id) JOIN trainer.day_templates d USING(day_template_id) WHERE s.template_set_id=NEW.template_set_id;
      WHEN 'recommendations' THEN
        IF NEW.program_version_id IS NULL THEN RETURN NEW; END IF;
        version_id:=NEW.program_version_id;
      ELSE SELECT r.program_version_id INTO version_id FROM trainer.recommendations r WHERE r.recommendation_id=NEW.recommendation_id;
      END CASE;
      IF version_id IS NULL THEN RETURN NEW; END IF;
      SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296) INTO same_tx FROM trainer.program_versions v JOIN trainer.confirmations c USING(confirmation_id) WHERE v.program_version_id=version_id;
      IF same_tx IS DISTINCT FROM true THEN RAISE EXCEPTION 'published program tree cannot be extended' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $guard$;
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.day_templates FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.template_blocks FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.template_exercises FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.template_sets FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.planned_metric_values FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.recommendations FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.recommendation_fact_links FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.recommendation_calculation_links FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.recommendation_conclusion_links FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();
    CREATE TRIGGER trg_program_tree_insert BEFORE INSERT ON trainer.recommendation_explanations FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_tree_insert();

    REVOKE UPDATE ON trainer.programs,trainer.program_versions,trainer.day_templates,trainer.template_blocks,trainer.template_exercises,trainer.template_sets,trainer.planned_metric_values,trainer.recommendations,trainer.recommendation_fact_links,trainer.recommendation_calculation_links,trainer.recommendation_conclusion_links,trainer.recommendation_explanations FROM trainer_app;
    REVOKE UPDATE(publication_state,published_at,confirmation_id) ON trainer.program_versions FROM trainer_app;
    GRANT UPDATE(current_version_id) ON trainer.programs TO trainer_app;
    GRANT UPDATE(publication_state) ON trainer.program_versions TO trainer_app;

    INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
    VALUES ('0010_d_g04_programs','{MIGRATION_CHECKSUM}',now(),0,'10',now());
    """)


def downgrade() -> None:
    raise RuntimeError("D-G04 migration is forward-only")
