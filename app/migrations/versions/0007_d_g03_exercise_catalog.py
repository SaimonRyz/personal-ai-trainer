"""Enable the approved D-G03 immutable exercise catalog.

Revision ID: 0007_d_g03_exercise_catalog
Revises: 0006_d_g02_context
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_d_g03_exercise_catalog"
down_revision: str | None = "0006_d_g02_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "d78aa173fe182f1201a17dbfd1c90fe55e090448534392525f9e61b3da95f442"
SEED_CHECKSUM = "62aa0c769f725d1b8487c9ab0468e0753dd59d89f561b46d9c5774b300881850"


def upgrade() -> None:
    op.execute(
        f"""
        DO $preflight$
        BEGIN
            IF (SELECT count(*) FROM trainer.exercises) <> 0
               OR (SELECT count(*) FROM trainer.exercise_revisions) <> 0
               OR (SELECT count(*) FROM trainer.exercise_aliases) <> 0
               OR (SELECT count(*) FROM trainer.muscle_groups) <> 0
               OR (SELECT count(*) FROM trainer.exercise_muscle_roles) <> 0
               OR (SELECT count(*) FROM trainer.exercise_measurements) <> 0
               OR (SELECT count(*) FROM trainer.exercise_equipment_requirements) <> 0
            THEN
                RAISE EXCEPTION 'D-G03 requires an empty legacy exercise schema';
            END IF;
        END
        $preflight$;

        DROP VIEW trainer.v_current_exercises;
        DROP TRIGGER trg_refresh_exercise_name_identity ON trainer.exercises;
        DROP TRIGGER trg_refresh_exercise_name_revision ON trainer.exercise_revisions;
        DROP FUNCTION trainer.refresh_exercise_name_from_identity();
        DROP FUNCTION trainer.refresh_exercise_name_from_revision();
        DROP FUNCTION trainer.refresh_exercise_current_name(uuid);
        DROP TABLE trainer.exercise_current_names;

        DROP TABLE trainer.exercise_equipment_requirements;
        DROP TABLE trainer.exercise_measurements;
        DROP TABLE trainer.exercise_muscle_roles;
        DROP TABLE trainer.exercise_aliases;

        ALTER TABLE trainer.exercises ADD COLUMN active_normalized_name text NOT NULL;
        ALTER TABLE trainer.exercises ADD CONSTRAINT uq_exercises_active_normalized_name
            UNIQUE(active_normalized_name);

        ALTER TABLE trainer.exercise_revisions RENAME COLUMN category TO category_code;
        ALTER TABLE trainer.exercise_revisions RENAME COLUMN status TO status_code;
        ALTER TABLE trainer.exercise_revisions ADD COLUMN normalization_version text NOT NULL;
        DROP INDEX IF EXISTS trainer.ux_exercise_active_normalized;
        DROP INDEX trainer.ix_exercise_normalized_pattern;
        CREATE INDEX ix_exercise_normalized_pattern
            ON trainer.exercise_revisions(normalized_name text_pattern_ops);

        ALTER TABLE trainer.muscle_groups RENAME COLUMN status TO status_code;
        ALTER TABLE trainer.muscle_groups ADD COLUMN normalization_version text NOT NULL;
        ALTER TABLE trainer.muscle_groups ADD COLUMN confirmation_id uuid NOT NULL
            REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT;

        CREATE TABLE trainer.unit_family_catalog (
            code text PRIMARY KEY,
            description text NOT NULL,
            seed_version integer NOT NULL CHECK(seed_version >= 1),
            archived_at timestamptz NULL,
            created_at timestamptz NOT NULL
        );

        CREATE TABLE trainer.exercise_aliases (
            exercise_alias_id uuid PRIMARY KEY,
            exercise_revision_id uuid NOT NULL
                REFERENCES trainer.exercise_revisions(exercise_revision_id) ON DELETE RESTRICT,
            alias text NOT NULL,
            normalized_alias text NOT NULL,
            locale text NULL,
            context text NULL,
            created_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX ux_exercise_alias_revision
            ON trainer.exercise_aliases(
                exercise_revision_id,normalized_alias,coalesce(locale,''),coalesce(context,'')
            );
        CREATE INDEX ix_exercise_alias_pattern
            ON trainer.exercise_aliases(normalized_alias text_pattern_ops);

        CREATE TABLE trainer.exercise_muscle_roles (
            exercise_revision_id uuid NOT NULL
                REFERENCES trainer.exercise_revisions(exercise_revision_id) ON DELETE RESTRICT,
            muscle_group_id uuid NOT NULL
                REFERENCES trainer.muscle_groups(muscle_group_id) ON DELETE RESTRICT,
            role text NOT NULL CHECK(role IN ('primary','secondary','stabilizer')),
            created_at timestamptz NOT NULL,
            PRIMARY KEY(exercise_revision_id,muscle_group_id,role)
        );

        CREATE TABLE trainer.exercise_measurements (
            exercise_measurement_id uuid PRIMARY KEY,
            exercise_revision_id uuid NOT NULL
                REFERENCES trainer.exercise_revisions(exercise_revision_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL
                REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            requiredness text NOT NULL CHECK(requiredness IN ('required','optional')),
            unit_family text NOT NULL
                REFERENCES trainer.unit_family_catalog(code) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL,
            UNIQUE(exercise_revision_id,metric_kind)
        );

        CREATE TABLE trainer.exercise_equipment_requirements (
            requirement_id uuid PRIMARY KEY,
            exercise_revision_id uuid NOT NULL
                REFERENCES trainer.exercise_revisions(exercise_revision_id) ON DELETE RESTRICT,
            equipment_type text NOT NULL
                REFERENCES trainer.equipment_type_catalog(code) ON DELETE RESTRICT,
            optionality text NOT NULL CHECK(optionality IN ('required','optional','alternative')),
            alternative_group text NULL,
            created_at timestamptz NOT NULL,
            UNIQUE(exercise_revision_id,equipment_type,optionality),
            CHECK(
                (optionality IN ('required','optional') AND alternative_group IS NULL)
                OR (optionality='alternative' AND length(btrim(alternative_group)) BETWEEN 1 AND 100)
            )
        );

        ALTER TABLE trainer.exercise_status_catalog
            ADD COLUMN planning_eligible boolean NOT NULL DEFAULT false;
        ALTER TABLE trainer.exercise_status_catalog ALTER COLUMN planning_eligible DROP DEFAULT;

        CREATE FUNCTION trainer.validate_exercise_revision(target_revision_id uuid)
        RETURNS void LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, trainer
        AS $body$
        DECLARE planning boolean;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM trainer.exercises e
                JOIN trainer.exercise_revisions r
                  ON r.exercise_revision_id=e.current_revision_id
                WHERE e.current_revision_id=target_revision_id
                  AND e.active_normalized_name<>r.normalized_name
            ) THEN
                RAISE EXCEPTION 'active exercise name does not match current revision';
            END IF;

            SELECT s.planning_eligible INTO planning
            FROM trainer.exercise_revisions r
            JOIN trainer.exercise_status_catalog s ON s.code=r.status_code
            WHERE r.exercise_revision_id=target_revision_id;
            IF planning AND NOT EXISTS (
                SELECT 1 FROM trainer.exercise_muscle_roles
                WHERE exercise_revision_id=target_revision_id AND role='primary'
            ) THEN
                RAISE EXCEPTION 'planning-eligible exercise requires a primary muscle group';
            END IF;

            IF EXISTS (
                SELECT 1 FROM trainer.exercise_equipment_requirements
                WHERE exercise_revision_id=target_revision_id AND optionality='alternative'
                GROUP BY alternative_group HAVING count(DISTINCT equipment_type)<2
            ) THEN
                RAISE EXCEPTION 'alternative equipment group requires two distinct types';
            END IF;
        END
        $body$;
        REVOKE ALL ON FUNCTION trainer.validate_exercise_revision(uuid) FROM PUBLIC;

        CREATE FUNCTION trainer.guard_exercise_identity_tree()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, trainer
        AS $body$
        BEGIN
            PERFORM trainer.validate_exercise_revision(NEW.current_revision_id);
            RETURN NEW;
        END
        $body$;
        REVOKE ALL ON FUNCTION trainer.guard_exercise_identity_tree() FROM PUBLIC;

        CREATE FUNCTION trainer.guard_exercise_child_tree()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, trainer
        AS $body$
        BEGIN
            IF TG_OP='DELETE' THEN
                PERFORM trainer.validate_exercise_revision(OLD.exercise_revision_id);
                RETURN OLD;
            END IF;
            PERFORM trainer.validate_exercise_revision(NEW.exercise_revision_id);
            RETURN NEW;
        END
        $body$;
        REVOKE ALL ON FUNCTION trainer.guard_exercise_child_tree() FROM PUBLIC;

        CREATE CONSTRAINT TRIGGER trg_exercises_current_tree
            AFTER INSERT OR UPDATE OF current_revision_id,active_normalized_name ON trainer.exercises
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION trainer.guard_exercise_identity_tree();
        CREATE CONSTRAINT TRIGGER trg_exercise_roles_tree
            AFTER INSERT OR UPDATE OR DELETE ON trainer.exercise_muscle_roles
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION trainer.guard_exercise_child_tree();
        CREATE CONSTRAINT TRIGGER trg_exercise_equipment_tree
            AFTER INSERT OR UPDATE OR DELETE ON trainer.exercise_equipment_requirements
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION trainer.guard_exercise_child_tree();

        CREATE VIEW trainer.v_current_exercises AS
            SELECT r.*,e.current_revision_id,e.active_normalized_name,
                   e.created_at AS identity_created_at,e.archived_at,e.logically_deleted_at
            FROM trainer.exercises e
            JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id
            WHERE e.archived_at IS NULL AND e.logically_deleted_at IS NULL;

        ALTER TABLE trainer.drafts DROP CONSTRAINT ck_drafts_owner_scope;
        ALTER TABLE trainer.drafts ADD CONSTRAINT ck_drafts_owner_scope CHECK(
            owner_profile_id IS NOT NULL OR intent_type IN ('onboarding','exercise_catalog')
        );

        INSERT INTO trainer.exercise_status_catalog(
            code,description,seed_version,planning_eligible,created_at
        ) VALUES
            ('draft','Draft exercise unavailable to planning',1,false,now()),
            ('active','Active exercise available to planning',1,true,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.muscle_group_status_catalog(code,description,seed_version,created_at)
        VALUES ('active','Active muscle group',1,now()) ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('exercise_catalog','Exercise catalog aggregate',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.dependency_type_catalog(code,description,seed_version,created_at) VALUES
            ('exercise','Exercise revision',1,now()),
            ('muscle_group','Muscle group snapshot',1,now()),
            ('exercise_catalog_rules','Exercise catalog and normalization rules',1,now()),
            ('active_exercise_name','Active normalized exercise name check',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at) VALUES
            ('exercise','Exercise',1,now()),('muscle_group','Muscle group',1,now())
        ON CONFLICT(code) DO NOTHING;

        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('d_g03_exercise_catalog',1,'{SEED_CHECKSUM}',now(),now())
        ON CONFLICT(seed_name,seed_version) DO NOTHING;
        DO $seed_validation$
        BEGIN
            IF (SELECT count(*) FROM trainer.exercise_status_catalog
                WHERE archived_at IS NULL) <> 2
               OR NOT EXISTS (SELECT 1 FROM trainer.exercise_status_catalog
                   WHERE code='draft' AND seed_version=1 AND planning_eligible=false)
               OR NOT EXISTS (SELECT 1 FROM trainer.exercise_status_catalog
                   WHERE code='active' AND seed_version=1 AND planning_eligible=true)
               OR (SELECT count(*) FROM trainer.muscle_group_status_catalog
                   WHERE archived_at IS NULL) <> 1
               OR NOT EXISTS (SELECT 1 FROM trainer.muscle_group_status_catalog
                   WHERE code='active' AND seed_version=1)
               OR NOT EXISTS (SELECT 1 FROM trainer.seed_versions
                   WHERE seed_name='d_g03_exercise_catalog' AND seed_version=1
                     AND checksum='{SEED_CHECKSUM}')
            THEN RAISE EXCEPTION 'D-G03 seed conflict'; END IF;
        END
        $seed_validation$;

        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at
        ) VALUES ('0007_d_g03_exercise_catalog','{MIGRATION_CHECKSUM}',now(),0,'7',now());

        GRANT SELECT,INSERT ON trainer.unit_family_catalog,trainer.exercise_aliases,
            trainer.exercise_muscle_roles,trainer.exercise_measurements,
            trainer.exercise_equipment_requirements TO trainer_app;
        REVOKE DELETE,TRUNCATE ON trainer.unit_family_catalog,trainer.exercise_aliases,
            trainer.exercise_muscle_roles,trainer.exercise_measurements,
            trainer.exercise_equipment_requirements FROM trainer_app;
        REVOKE UPDATE ON trainer.exercise_revisions,trainer.exercise_aliases,
            trainer.muscle_groups,trainer.exercise_muscle_roles,
            trainer.exercise_measurements,trainer.exercise_equipment_requirements
            FROM trainer_app;
        REVOKE UPDATE ON trainer.exercises FROM trainer_app;
        GRANT UPDATE(current_revision_id,active_normalized_name,archived_at,logically_deleted_at)
            ON trainer.exercises TO trainer_app;
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G03 migration is forward-only; downgrade requires owner approval")
