"""Create the approved 67-entity physical schema and views.

Revision ID: 0002_domain_schema
Revises: 0001_technical_foundation
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_domain_schema"
down_revision: str | None = "0001_technical_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "537dd916fa19962440b6a10c2d077f18f0a0c94c2f1d4af079787365105e70d8"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trainer.profiles (
            profile_id uuid PRIMARY KEY, current_revision_id uuid UNIQUE NULL,
            created_at timestamptz NOT NULL, archived_at timestamptz NULL
        );
        CREATE UNIQUE INDEX ux_one_active_profile ON trainer.profiles ((true)) WHERE archived_at IS NULL;
        CREATE TABLE trainer.training_places (
            place_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            current_revision_id uuid UNIQUE NOT NULL, created_at timestamptz NOT NULL,
            archived_at timestamptz NULL, logically_deleted_at timestamptz NULL
        );
        CREATE TABLE trainer.profile_revisions (
            profile_revision_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK (revision_no >= 1), supersedes_revision_id uuid NULL,
            birth_date date NULL, sex text NULL CHECK (sex IN ('male','female','other','not_stated')),
            timezone text NOT NULL, started_on date NULL,
            desired_sessions_per_week smallint NULL CHECK (desired_sessions_per_week BETWEEN 1 AND 14),
            usual_place_id uuid NULL REFERENCES trainer.training_places(place_id) ON DELETE RESTRICT,
            comment text NULL, source_kind text NOT NULL CHECK (source_kind IN ('user','system','calculation','curator','external_metadata')),
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            confirmed_at timestamptz NOT NULL, created_at timestamptz NOT NULL,
            UNIQUE(profile_id,revision_no), UNIQUE(profile_id,profile_revision_id)
        );
        ALTER TABLE trainer.profiles ADD CONSTRAINT fk_profiles_current FOREIGN KEY(profile_id,current_revision_id)
            REFERENCES trainer.profile_revisions(profile_id,profile_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.profile_revisions ADD CONSTRAINT fk_profile_revision_supersedes
            FOREIGN KEY(profile_id,supersedes_revision_id) REFERENCES trainer.profile_revisions(profile_id,profile_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE trainer.goals (
            goal_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            parent_goal_id uuid NULL REFERENCES trainer.goals(goal_id) ON DELETE RESTRICT,
            current_revision_id uuid UNIQUE NOT NULL, created_at timestamptz NOT NULL,
            archived_at timestamptz NULL, logically_deleted_at timestamptz NULL,
            CHECK(parent_goal_id IS DISTINCT FROM goal_id)
        );
        CREATE TABLE trainer.goal_revisions (
            goal_revision_id uuid PRIMARY KEY, goal_id uuid NOT NULL REFERENCES trainer.goals(goal_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1), supersedes_revision_id uuid NULL,
            name text NOT NULL, description text NULL,
            goal_type text NOT NULL REFERENCES trainer.goal_type_catalog(code) ON DELETE RESTRICT,
            start_value numeric(18,6) NULL, target_value numeric(18,6) NULL,
            unit_code text NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            set_on date NOT NULL, target_date date NOT NULL, priority smallint NOT NULL CHECK(priority BETWEEN 1 AND 100),
            status text NOT NULL CHECK(status IN ('active','achieved','cancelled')), outcome_date date NULL,
            comment text NULL, source_kind text NOT NULL CHECK(source_kind IN ('user','system','calculation','curator','external_metadata')),
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            confirmed_at timestamptz NOT NULL, created_at timestamptz NOT NULL,
            CHECK(target_date >= set_on), CHECK(unit_code IS NOT NULL OR (start_value IS NULL AND target_value IS NULL)),
            UNIQUE(goal_id,revision_no), UNIQUE(goal_id,goal_revision_id)
        );
        ALTER TABLE trainer.goals ADD CONSTRAINT fk_goals_current FOREIGN KEY(goal_id,current_revision_id)
            REFERENCES trainer.goal_revisions(goal_id,goal_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.goal_revisions ADD CONSTRAINT fk_goal_revision_supersedes
            FOREIGN KEY(goal_id,supersedes_revision_id) REFERENCES trainer.goal_revisions(goal_id,goal_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE trainer.training_place_revisions (
            place_revision_id uuid PRIMARY KEY, place_id uuid NOT NULL REFERENCES trainer.training_places(place_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1), supersedes_revision_id uuid NULL,
            name text NOT NULL, place_type text NOT NULL REFERENCES trainer.place_type_catalog(code) ON DELETE RESTRICT,
            status text NOT NULL CHECK(status IN ('active','unavailable','archived')), comment text NULL,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            confirmed_at timestamptz NOT NULL, created_at timestamptz NOT NULL,
            UNIQUE(place_id,revision_no), UNIQUE(place_id,place_revision_id)
        );
        ALTER TABLE trainer.training_places ADD CONSTRAINT fk_places_current FOREIGN KEY(place_id,current_revision_id)
            REFERENCES trainer.training_place_revisions(place_id,place_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.training_place_revisions ADD CONSTRAINT fk_place_revision_supersedes
            FOREIGN KEY(place_id,supersedes_revision_id) REFERENCES trainer.training_place_revisions(place_id,place_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE trainer.equipment_items (
            equipment_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            current_revision_id uuid UNIQUE NOT NULL, created_at timestamptz NOT NULL,
            archived_at timestamptz NULL, logically_deleted_at timestamptz NULL
        );
        CREATE TABLE trainer.equipment_item_revisions (
            equipment_revision_id uuid PRIMARY KEY, equipment_id uuid NOT NULL REFERENCES trainer.equipment_items(equipment_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1),
            equipment_type text NOT NULL REFERENCES trainer.equipment_type_catalog(code) ON DELETE RESTRICT,
            name text NOT NULL, maker text NULL, brand text NULL, model text NULL, purchased_on date NULL,
            purchase_price_rub numeric(14,2) NULL CHECK(purchase_price_rub >= 0),
            condition text NOT NULL REFERENCES trainer.equipment_condition_catalog(code) ON DELETE RESTRICT,
            availability text NOT NULL REFERENCES trainer.equipment_availability_catalog(code) ON DELETE RESTRICT,
            rating smallint NULL CHECK(rating BETWEEN 1 AND 5), characteristics jsonb NOT NULL DEFAULT '{}'::jsonb,
            comment text NULL, supersedes_revision_id uuid NULL,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            confirmed_at timestamptz NOT NULL, created_at timestamptz NOT NULL,
            UNIQUE(equipment_id,revision_no), UNIQUE(equipment_id,equipment_revision_id)
        );
        ALTER TABLE trainer.equipment_items ADD CONSTRAINT fk_equipment_current FOREIGN KEY(equipment_id,current_revision_id)
            REFERENCES trainer.equipment_item_revisions(equipment_id,equipment_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.equipment_item_revisions ADD CONSTRAINT fk_equipment_revision_supersedes
            FOREIGN KEY(equipment_id,supersedes_revision_id) REFERENCES trainer.equipment_item_revisions(equipment_id,equipment_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE trainer.place_equipment_availability (
            availability_id uuid PRIMARY KEY, place_id uuid NOT NULL REFERENCES trainer.training_places(place_id) ON DELETE RESTRICT,
            equipment_id uuid NOT NULL REFERENCES trainer.equipment_items(equipment_id) ON DELETE RESTRICT,
            valid_from timestamptz NOT NULL, valid_to timestamptz NULL,
            quantity numeric(12,3) NULL CHECK(quantity >= 0), context text NULL,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            domain_change_id uuid NOT NULL REFERENCES trainer.domain_changes(domain_change_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK(valid_to IS NULL OR valid_to > valid_from),
            EXCLUDE USING gist (place_id WITH =,equipment_id WITH =,tstzrange(valid_from,valid_to,'[)') WITH &&)
        );
        CREATE TABLE trainer.equipment_events (
            equipment_event_id uuid PRIMARY KEY, equipment_id uuid NOT NULL REFERENCES trainer.equipment_items(equipment_id) ON DELETE RESTRICT,
            place_id uuid NULL REFERENCES trainer.training_places(place_id) ON DELETE RESTRICT, occurred_at timestamptz NOT NULL,
            event_kind text NOT NULL REFERENCES trainer.equipment_event_kind_catalog(code) ON DELETE RESTRICT,
            quantity_delta numeric(12,3) NULL, result_quantity numeric(12,3) NULL CHECK(result_quantity >= 0),
            result_condition text NULL REFERENCES trainer.equipment_condition_catalog(code) ON DELETE RESTRICT,
            reason text NULL, supersedes_event_id uuid NULL REFERENCES trainer.equipment_events(equipment_event_id) ON DELETE RESTRICT,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK(quantity_delta IS NOT NULL OR result_quantity IS NOT NULL OR result_condition IS NOT NULL)
        );

        CREATE TABLE trainer.exercises (
            exercise_id uuid PRIMARY KEY, current_revision_id uuid UNIQUE NOT NULL, created_at timestamptz NOT NULL,
            archived_at timestamptz NULL, logically_deleted_at timestamptz NULL
        );
        CREATE TABLE trainer.exercise_revisions (
            exercise_revision_id uuid PRIMARY KEY, exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1), canonical_name text NOT NULL, normalized_name text NOT NULL,
            description text NULL, category text NOT NULL REFERENCES trainer.exercise_category_catalog(code) ON DELETE RESTRICT,
            unilateral boolean NOT NULL, status text NOT NULL REFERENCES trainer.exercise_status_catalog(code) ON DELETE RESTRICT,
            comment text NULL, supersedes_revision_id uuid NULL,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            confirmed_at timestamptz NOT NULL, created_at timestamptz NOT NULL,
            UNIQUE(exercise_id,revision_no), UNIQUE(exercise_id,exercise_revision_id)
        );
        ALTER TABLE trainer.exercises ADD CONSTRAINT fk_exercises_current FOREIGN KEY(exercise_id,current_revision_id)
            REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.exercise_revisions ADD CONSTRAINT fk_exercise_revision_supersedes
            FOREIGN KEY(exercise_id,supersedes_revision_id) REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        CREATE UNIQUE INDEX ux_exercise_active_normalized ON trainer.exercise_revisions(normalized_name);
        CREATE INDEX ix_exercise_normalized_pattern ON trainer.exercise_revisions(normalized_name text_pattern_ops);
        CREATE TABLE trainer.exercise_aliases (
            exercise_alias_id uuid PRIMARY KEY, exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            alias text NOT NULL, normalized_alias text NOT NULL, locale text NULL, context text NULL,
            archived_at timestamptz NULL, created_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX ux_exercise_alias_active ON trainer.exercise_aliases
            (normalized_alias,coalesce(locale,''),coalesce(context,'')) WHERE archived_at IS NULL;
        CREATE INDEX ix_exercise_alias_pattern ON trainer.exercise_aliases(normalized_alias text_pattern_ops);
        CREATE TABLE trainer.muscle_groups (
            muscle_group_id uuid PRIMARY KEY, name text NOT NULL, normalized_name text NOT NULL UNIQUE,
            status text NOT NULL REFERENCES trainer.muscle_group_status_catalog(code) ON DELETE RESTRICT,
            archived_at timestamptz NULL, created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.exercise_muscle_roles (
            exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            muscle_group_id uuid NOT NULL REFERENCES trainer.muscle_groups(muscle_group_id) ON DELETE RESTRICT,
            role text NOT NULL CHECK(role IN ('primary','secondary','stabilizer')), created_at timestamptz NOT NULL,
            PRIMARY KEY(exercise_id,muscle_group_id,role)
        );
        CREATE TABLE trainer.exercise_measurements (
            exercise_measurement_id uuid PRIMARY KEY, exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            requiredness text NOT NULL CHECK(requiredness IN ('required','optional','conditional')),
            requiredness_rule jsonb NULL, unit_family text NOT NULL, archived_at timestamptz NULL, created_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX ux_exercise_measurement_active ON trainer.exercise_measurements(exercise_id,metric_kind) WHERE archived_at IS NULL;
        CREATE TABLE trainer.exercise_equipment_requirements (
            requirement_id uuid PRIMARY KEY, exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            equipment_type text NOT NULL REFERENCES trainer.equipment_type_catalog(code) ON DELETE RESTRICT,
            compatibility_rule jsonb NULL, optionality text NOT NULL CHECK(optionality IN ('required','optional','alternative')),
            archived_at timestamptz NULL, created_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX ux_exercise_equipment_active ON trainer.exercise_equipment_requirements
            (exercise_id,equipment_type,optionality) WHERE archived_at IS NULL;

        CREATE TABLE trainer.programs (
            program_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            goal_id uuid NOT NULL REFERENCES trainer.goals(goal_id) ON DELETE RESTRICT, name text NOT NULL, description text NULL,
            starts_on date NULL, ends_on date NULL, status text NOT NULL REFERENCES trainer.program_status_catalog(code) ON DELETE RESTRICT,
            comment text NULL, created_at timestamptz NOT NULL, archived_at timestamptz NULL, logically_deleted_at timestamptz NULL,
            CHECK(ends_on IS NULL OR starts_on IS NULL OR ends_on >= starts_on)
        );
        CREATE TABLE trainer.program_versions (
            program_version_id uuid PRIMARY KEY, program_id uuid NOT NULL REFERENCES trainer.programs(program_id) ON DELETE RESTRICT,
            version_no integer NOT NULL CHECK(version_no >= 1), version_label text NOT NULL,
            period_start date NOT NULL, period_end date NOT NULL, rationale text NOT NULL,
            publication_state text NOT NULL CHECK(publication_state IN ('composing','published','retired')),
            published_at timestamptz NULL, supersedes_version_id uuid NULL REFERENCES trainer.program_versions(program_version_id) ON DELETE RESTRICT,
            confirmation_id uuid NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, UNIQUE(program_id,version_no),
            CHECK(period_end >= period_start),
            CHECK(publication_state <> 'published' OR (published_at IS NOT NULL AND confirmation_id IS NOT NULL))
        );
        CREATE TABLE trainer.day_templates (
            day_template_id uuid PRIMARY KEY, program_version_id uuid NOT NULL REFERENCES trainer.program_versions(program_version_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), day_type text NOT NULL REFERENCES trainer.day_type_catalog(code) ON DELETE RESTRICT,
            purpose text NOT NULL, equipment_needs jsonb NOT NULL DEFAULT '[]'::jsonb, created_at timestamptz NOT NULL,
            UNIQUE(program_version_id,position)
        );
        CREATE TABLE trainer.template_blocks (
            template_block_id uuid PRIMARY KEY, day_template_id uuid NOT NULL REFERENCES trainer.day_templates(day_template_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), block_type text NOT NULL REFERENCES trainer.block_type_catalog(code) ON DELETE RESTRICT,
            name text NULL, declared_rounds integer NULL CHECK(declared_rounds > 0), rest_seconds integer NULL CHECK(rest_seconds >= 0),
            comment text NULL, created_at timestamptz NOT NULL, UNIQUE(day_template_id,position)
        );
        CREATE TABLE trainer.template_exercises (
            template_exercise_id uuid PRIMARY KEY, template_block_id uuid NOT NULL REFERENCES trainer.template_blocks(template_block_id) ON DELETE RESTRICT,
            exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), circuit_position integer NULL CHECK(circuit_position > 0),
            equipment_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb, comment text NULL, created_at timestamptz NOT NULL,
            UNIQUE(template_block_id,position)
        );
        CREATE TABLE trainer.template_sets (
            template_set_id uuid PRIMARY KEY, template_exercise_id uuid NOT NULL REFERENCES trainer.template_exercises(template_exercise_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), round_no integer NULL CHECK(round_no > 0),
            logical_set_no integer NOT NULL CHECK(logical_set_no >= 1), side text NULL CHECK(side IN ('left','right','both','not_applicable')),
            set_type text NOT NULL REFERENCES trainer.set_type_catalog(code) ON DELETE RESTRICT, tempo text NULL,
            pause_seconds numeric(8,3) NULL CHECK(pause_seconds >= 0), rest_after_seconds integer NULL CHECK(rest_after_seconds >= 0),
            created_at timestamptz NOT NULL, UNIQUE(template_exercise_id,position)
        );
        CREATE TABLE trainer.workout_plans (
            plan_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            current_revision_id uuid UNIQUE NOT NULL, created_at timestamptz NOT NULL,
            archived_at timestamptz NULL, logically_deleted_at timestamptz NULL
        );
        CREATE TABLE trainer.workout_plan_revisions (
            plan_revision_id uuid PRIMARY KEY, plan_id uuid NOT NULL REFERENCES trainer.workout_plans(plan_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1), program_id uuid NULL REFERENCES trainer.programs(program_id) ON DELETE RESTRICT,
            program_version_id uuid NULL REFERENCES trainer.program_versions(program_version_id) ON DELETE RESTRICT,
            day_template_id uuid NULL REFERENCES trainer.day_templates(day_template_id) ON DELETE RESTRICT,
            planned_date date NOT NULL, planned_time time NULL,
            plan_type text NOT NULL REFERENCES trainer.plan_type_catalog(code) ON DELETE RESTRICT,
            name text NOT NULL, purpose text NULL,
            lifecycle text NOT NULL CHECK(lifecycle IN ('draft','planned','rescheduled','cancelled','completed','missed')),
            context_snapshot jsonb NOT NULL, comment text NULL, supersedes_revision_id uuid NULL,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, UNIQUE(plan_id,revision_no), UNIQUE(plan_id,plan_revision_id)
        );
        ALTER TABLE trainer.workout_plans ADD CONSTRAINT fk_plans_current FOREIGN KEY(plan_id,current_revision_id)
            REFERENCES trainer.workout_plan_revisions(plan_id,plan_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.workout_plan_revisions ADD CONSTRAINT fk_plan_revision_supersedes
            FOREIGN KEY(plan_id,supersedes_revision_id) REFERENCES trainer.workout_plan_revisions(plan_id,plan_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        CREATE INDEX ix_plan_date ON trainer.workout_plan_revisions(planned_date,plan_id);
        CREATE TABLE trainer.planned_blocks (
            planned_block_id uuid PRIMARY KEY, plan_revision_id uuid NOT NULL REFERENCES trainer.workout_plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), block_type text NOT NULL REFERENCES trainer.block_type_catalog(code) ON DELETE RESTRICT,
            name text NULL, declared_rounds integer NULL CHECK(declared_rounds > 0), rest_seconds integer NULL CHECK(rest_seconds >= 0),
            comment text NULL, created_at timestamptz NOT NULL, UNIQUE(plan_revision_id,position)
        );
        CREATE TABLE trainer.planned_exercises (
            planned_exercise_id uuid PRIMARY KEY, planned_block_id uuid NOT NULL REFERENCES trainer.planned_blocks(planned_block_id) ON DELETE RESTRICT,
            exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), circuit_position integer NULL CHECK(circuit_position > 0),
            equipment_snapshot jsonb NOT NULL, comment text NULL, created_at timestamptz NOT NULL,
            UNIQUE(planned_block_id,position)
        );
        CREATE TABLE trainer.planned_sets (
            planned_set_id uuid PRIMARY KEY, planned_exercise_id uuid NOT NULL REFERENCES trainer.planned_exercises(planned_exercise_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), round_no integer NULL CHECK(round_no > 0),
            logical_set_no integer NOT NULL CHECK(logical_set_no >= 1), side text NULL,
            set_type text NOT NULL REFERENCES trainer.set_type_catalog(code) ON DELETE RESTRICT,
            target_rpe_min numeric(4,2) NULL CHECK(target_rpe_min BETWEEN 0 AND 10),
            target_rpe_max numeric(4,2) NULL CHECK(target_rpe_max BETWEEN 0 AND 10),
            target_rir_min numeric(4,2) NULL CHECK(target_rir_min BETWEEN 0 AND 10),
            target_rir_max numeric(4,2) NULL CHECK(target_rir_max BETWEEN 0 AND 10),
            tempo text NULL, pause_seconds numeric(8,3) NULL CHECK(pause_seconds >= 0),
            rest_after_seconds integer NULL CHECK(rest_after_seconds >= 0), created_at timestamptz NOT NULL,
            CHECK(target_rpe_min IS NULL OR target_rpe_max IS NULL OR target_rpe_min <= target_rpe_max),
            CHECK(target_rir_min IS NULL OR target_rir_max IS NULL OR target_rir_min <= target_rir_max),
            UNIQUE(planned_exercise_id,position)
        );
        CREATE TABLE trainer.planned_metric_values (
            planned_metric_value_id uuid PRIMARY KEY,
            template_set_id uuid NULL REFERENCES trainer.template_sets(template_set_id) ON DELETE RESTRICT,
            planned_set_id uuid NULL REFERENCES trainer.planned_sets(planned_set_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            exact_value numeric(18,6) NULL, min_value numeric(18,6) NULL, max_value numeric(18,6) NULL,
            unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            custom_definition jsonb NULL, created_at timestamptz NOT NULL,
            CHECK((template_set_id IS NULL) <> (planned_set_id IS NULL)),
            CHECK((exact_value IS NOT NULL AND min_value IS NULL AND max_value IS NULL) OR
                  (exact_value IS NULL AND min_value IS NOT NULL AND max_value IS NOT NULL AND min_value <= max_value))
        );
        CREATE UNIQUE INDEX ux_template_metric ON trainer.planned_metric_values(template_set_id,metric_kind) WHERE template_set_id IS NOT NULL;
        CREATE UNIQUE INDEX ux_planned_metric ON trainer.planned_metric_values(planned_set_id,metric_kind) WHERE planned_set_id IS NOT NULL;
        CREATE TABLE trainer.planned_load_components (
            planned_load_component_id uuid PRIMARY KEY, planned_set_id uuid NOT NULL REFERENCES trainer.planned_sets(planned_set_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), load_kind text NOT NULL REFERENCES trainer.load_kind_catalog(code) ON DELETE RESTRICT,
            equipment_id uuid NULL REFERENCES trainer.equipment_items(equipment_id) ON DELETE RESTRICT,
            equipment_snapshot jsonb NOT NULL, item_count numeric(12,3) NULL CHECK(item_count > 0),
            mass numeric(18,6) NULL CHECK(mass >= 0), unit_code text NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            mass_meaning text NULL, direction text NULL REFERENCES trainer.load_direction_catalog(code) ON DELETE RESTRICT,
            raw_ambiguous_text text NULL, created_at timestamptz NOT NULL,
            CHECK(mass IS NULL OR (unit_code IS NOT NULL AND mass_meaning IS NOT NULL)),
            CHECK(mass IS NULL OR raw_ambiguous_text IS NULL), UNIQUE(planned_set_id,position)
        );
        CREATE TABLE trainer.plan_reschedule_links (
            source_plan_id uuid PRIMARY KEY REFERENCES trainer.workout_plans(plan_id) ON DELETE RESTRICT,
            replacement_plan_id uuid NOT NULL UNIQUE REFERENCES trainer.workout_plans(plan_id) ON DELETE RESTRICT,
            reason text NULL, confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK(source_plan_id <> replacement_plan_id)
        );

        CREATE TABLE trainer.derived_values (
            derived_value_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            method_id text NOT NULL REFERENCES trainer.derived_method_catalog(code) ON DELETE RESTRICT,
            method_version text NOT NULL, result_value numeric(18,6) NOT NULL,
            unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            calculated_at timestamptz NOT NULL, assumptions jsonb NOT NULL,
            state text NOT NULL CHECK(state IN ('current','stale','replaced')), stale_reason text NULL,
            supersedes_derived_value_id uuid NULL REFERENCES trainer.derived_values(derived_value_id) ON DELETE RESTRICT,
            persistence_reason text NOT NULL CHECK(persistence_reason IN ('record','program','plan','recommendation','explanation','progress','history')),
            created_at timestamptz NOT NULL, CHECK(state <> 'stale' OR stale_reason IS NOT NULL)
        );
        CREATE TABLE trainer.derived_value_inputs (
            derived_value_id uuid NOT NULL REFERENCES trainer.derived_values(derived_value_id) ON DELETE RESTRICT,
            source_kind text NOT NULL CHECK(source_kind IN ('user','system','calculation','curator','external_metadata')),
            source_object_type text NOT NULL REFERENCES trainer.domain_object_type_catalog(code) ON DELETE RESTRICT,
            source_object_id uuid NOT NULL, source_revision_id uuid NOT NULL, input_role text NOT NULL,
            source_value numeric(18,6) NULL, source_unit text NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            snapshot jsonb NOT NULL, created_at timestamptz NOT NULL,
            PRIMARY KEY(derived_value_id,source_object_type,source_object_id,source_revision_id,input_role)
        );
        CREATE INDEX ix_derived_inputs_source ON trainer.derived_value_inputs(source_object_type,source_object_id,source_revision_id);

        CREATE TABLE trainer.workout_sessions (
            session_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            plan_id uuid NULL REFERENCES trainer.workout_plans(plan_id) ON DELETE RESTRICT,
            current_revision_id uuid UNIQUE NOT NULL, created_at timestamptz NOT NULL,
            archived_at timestamptz NULL, logically_deleted_at timestamptz NULL
        );
        CREATE TABLE trainer.workout_session_revisions (
            session_revision_id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES trainer.workout_sessions(session_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1),
            plan_revision_id uuid NULL REFERENCES trainer.workout_plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            started_at timestamptz NULL, local_start_date date NOT NULL, ended_at timestamptz NULL,
            duration_seconds integer NULL CHECK(duration_seconds >= 0),
            session_type text NOT NULL REFERENCES trainer.session_type_catalog(code) ON DELETE RESTRICT,
            name text NULL, lifecycle text NOT NULL CHECK(lifecycle IN ('in_progress','completed','interrupted')),
            effort numeric(4,2) NULL CHECK(effort BETWEEN 0 AND 10), comment text NULL, supersedes_revision_id uuid NULL,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK(ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at),
            UNIQUE(session_id,revision_no), UNIQUE(session_id,session_revision_id)
        );
        ALTER TABLE trainer.workout_sessions ADD CONSTRAINT fk_sessions_current FOREIGN KEY(session_id,current_revision_id)
            REFERENCES trainer.workout_session_revisions(session_id,session_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.workout_session_revisions ADD CONSTRAINT fk_session_revision_supersedes
            FOREIGN KEY(session_id,supersedes_revision_id) REFERENCES trainer.workout_session_revisions(session_id,session_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        CREATE INDEX ix_session_date ON trainer.workout_session_revisions(local_start_date,session_id);
        CREATE TABLE trainer.actual_blocks (
            actual_block_id uuid PRIMARY KEY, session_revision_id uuid NOT NULL REFERENCES trainer.workout_session_revisions(session_revision_id) ON DELETE RESTRICT,
            planned_block_id uuid NULL REFERENCES trainer.planned_blocks(planned_block_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), block_type text NOT NULL REFERENCES trainer.block_type_catalog(code) ON DELETE RESTRICT,
            declared_rounds integer NULL CHECK(declared_rounds > 0), rest_seconds integer NULL CHECK(rest_seconds >= 0),
            comment text NULL, created_at timestamptz NOT NULL, UNIQUE(session_revision_id,position)
        );
        CREATE TABLE trainer.exercise_executions (
            execution_id uuid PRIMARY KEY, actual_block_id uuid NOT NULL REFERENCES trainer.actual_blocks(actual_block_id) ON DELETE RESTRICT,
            exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            planned_exercise_id uuid NULL REFERENCES trainer.planned_exercises(planned_exercise_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1),
            result text NOT NULL CHECK(result IN ('performed','partial','skipped','added')),
            reason text NULL, created_at timestamptz NOT NULL, UNIQUE(actual_block_id,position)
        );
        CREATE TABLE trainer.replacement_links (
            added_execution_id uuid PRIMARY KEY REFERENCES trainer.exercise_executions(execution_id) ON DELETE RESTRICT,
            source_planned_exercise_id uuid NULL REFERENCES trainer.planned_exercises(planned_exercise_id) ON DELETE RESTRICT,
            skipped_execution_id uuid NULL REFERENCES trainer.exercise_executions(execution_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL,
            CHECK((source_planned_exercise_id IS NULL) <> (skipped_execution_id IS NULL)),
            CHECK(skipped_execution_id IS NULL OR skipped_execution_id <> added_execution_id)
        );
        CREATE TABLE trainer.actual_sets (
            actual_set_id uuid PRIMARY KEY, execution_id uuid NOT NULL REFERENCES trainer.exercise_executions(execution_id) ON DELETE RESTRICT,
            planned_set_id uuid NULL REFERENCES trainer.planned_sets(planned_set_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), round_no integer NULL CHECK(round_no > 0),
            logical_set_no integer NOT NULL CHECK(logical_set_no >= 1), side text NULL,
            set_type text NOT NULL REFERENCES trainer.set_type_catalog(code) ON DELETE RESTRICT,
            full_reps integer NULL CHECK(full_reps >= 0), partial_reps integer NULL CHECK(partial_reps >= 0),
            failed_attempts integer NULL CHECK(failed_attempts >= 0), reported_rpe numeric(4,2) NULL CHECK(reported_rpe BETWEEN 0 AND 10),
            reported_rir numeric(4,2) NULL CHECK(reported_rir BETWEEN 0 AND 10), failure_reached boolean NULL,
            rom_state text NULL, technique_state text NULL, tempo text NULL,
            pause_seconds numeric(8,3) NULL CHECK(pause_seconds >= 0), rest_after_seconds integer NULL CHECK(rest_after_seconds >= 0),
            status text NOT NULL REFERENCES trainer.actual_set_status_catalog(code) ON DELETE RESTRICT,
            comment text NULL, created_at timestamptz NOT NULL,
            CHECK(reported_rpe IS NULL OR reported_rir IS NULL), UNIQUE(execution_id,position)
        );
        CREATE INDEX ix_actual_sets_logical ON trainer.actual_sets(execution_id,logical_set_no,side);
        CREATE TABLE trainer.actual_metric_values (
            actual_metric_value_id uuid PRIMARY KEY, actual_set_id uuid NOT NULL REFERENCES trainer.actual_sets(actual_set_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            value numeric(18,6) NOT NULL, unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            custom_definition jsonb NULL,
            source_kind text NOT NULL CHECK(source_kind IN ('user','system','calculation','curator','external_metadata')),
            created_at timestamptz NOT NULL, UNIQUE(actual_set_id,metric_kind)
        );
        CREATE INDEX ix_actual_metric ON trainer.actual_metric_values(actual_set_id,metric_kind);
        CREATE TABLE trainer.actual_load_components (
            actual_load_component_id uuid PRIMARY KEY, actual_set_id uuid NOT NULL REFERENCES trainer.actual_sets(actual_set_id) ON DELETE RESTRICT,
            planned_load_component_id uuid NULL REFERENCES trainer.planned_load_components(planned_load_component_id) ON DELETE RESTRICT,
            position integer NOT NULL CHECK(position >= 1), load_kind text NOT NULL REFERENCES trainer.load_kind_catalog(code) ON DELETE RESTRICT,
            equipment_id uuid NULL REFERENCES trainer.equipment_items(equipment_id) ON DELETE RESTRICT,
            equipment_snapshot jsonb NOT NULL, item_count numeric(12,3) NULL CHECK(item_count > 0),
            mass numeric(18,6) NULL CHECK(mass >= 0), unit_code text NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            mass_meaning text NULL, direction text NULL REFERENCES trainer.load_direction_catalog(code) ON DELETE RESTRICT,
            raw_ambiguous_text text NULL, assembly_snapshot jsonb NULL, created_at timestamptz NOT NULL,
            CHECK(mass IS NULL OR (unit_code IS NOT NULL AND mass_meaning IS NOT NULL)),
            CHECK(mass IS NULL OR raw_ambiguous_text IS NULL), UNIQUE(actual_set_id,position)
        );
        CREATE TABLE trainer.workout_metrics (
            workout_metric_id uuid PRIMARY KEY, session_revision_id uuid NOT NULL REFERENCES trainer.workout_session_revisions(session_revision_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            value numeric(18,6) NOT NULL, unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            source_kind text NOT NULL CHECK(source_kind IN ('user','system','calculation','curator','external_metadata')),
            derived_value_id uuid NULL REFERENCES trainer.derived_values(derived_value_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK((source_kind = 'calculation') = (derived_value_id IS NOT NULL)),
            UNIQUE(session_revision_id,metric_kind)
        );

        CREATE TABLE trainer.comparison_conditions (
            condition_id uuid PRIMARY KEY, exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            rule_version text NOT NULL, normalized_key text NOT NULL, readable_snapshot jsonb NOT NULL,
            created_at timestamptz NOT NULL, UNIQUE(exercise_id,rule_version,normalized_key)
        );
        CREATE TABLE trainer.record_candidates (
            candidate_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            value numeric(18,6) NOT NULL, unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            condition_id uuid NOT NULL REFERENCES trainer.comparison_conditions(condition_id) ON DELETE RESTRICT,
            session_revision_id uuid NOT NULL REFERENCES trainer.workout_session_revisions(session_revision_id) ON DELETE RESTRICT,
            actual_set_id uuid NOT NULL REFERENCES trainer.actual_sets(actual_set_id) ON DELETE RESTRICT,
            technique_self_report text NULL,
            status text NOT NULL CHECK(status IN ('pending','accepted','rejected','invalidated')),
            decision_confirmation_id uuid NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL
        );
        CREATE INDEX ix_record_candidates_pending ON trainer.record_candidates(status,exercise_id,created_at DESC) WHERE status='pending';
        CREATE TABLE trainer.actual_records (
            record_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            value numeric(18,6) NOT NULL, unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            achieved_on date NOT NULL, condition_id uuid NOT NULL REFERENCES trainer.comparison_conditions(condition_id) ON DELETE RESTRICT,
            session_id uuid NULL REFERENCES trainer.workout_sessions(session_id) ON DELETE RESTRICT,
            session_revision_id uuid NULL REFERENCES trainer.workout_session_revisions(session_revision_id) ON DELETE RESTRICT,
            actual_set_id uuid NULL REFERENCES trainer.actual_sets(actual_set_id) ON DELETE RESTRICT,
            weight_value numeric(18,6) NULL, weight_unit text NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            reps integer NULL, duration_seconds integer NULL, body_mass numeric(18,6) NULL, comment text NULL,
            is_historical_import boolean NOT NULL, supersedes_record_id uuid NULL REFERENCES trainer.actual_records(record_id) ON DELETE RESTRICT,
            is_current boolean NOT NULL, confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL,
            CHECK(is_historical_import OR (session_id IS NOT NULL AND session_revision_id IS NOT NULL AND actual_set_id IS NOT NULL))
        );
        CREATE UNIQUE INDEX ux_actual_record_current ON trainer.actual_records(profile_id,exercise_id,metric_kind,condition_id) WHERE is_current;
        CREATE TABLE trainer.calculated_bests (
            calculated_best_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            exercise_id uuid NOT NULL REFERENCES trainer.exercises(exercise_id) ON DELETE RESTRICT,
            metric_kind text NOT NULL REFERENCES trainer.metric_kind_catalog(code) ON DELETE RESTRICT,
            value numeric(18,6) NOT NULL, unit_code text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            condition_id uuid NOT NULL REFERENCES trainer.comparison_conditions(condition_id) ON DELETE RESTRICT,
            derived_value_id uuid NOT NULL UNIQUE REFERENCES trainer.derived_values(derived_value_id) ON DELETE RESTRICT,
            achieved_on date NOT NULL, source_session_id uuid NULL REFERENCES trainer.workout_sessions(session_id) ON DELETE RESTRICT,
            source_session_revision_id uuid NULL REFERENCES trainer.workout_session_revisions(session_revision_id) ON DELETE RESTRICT,
            source_actual_set_id uuid NULL REFERENCES trainer.actual_sets(actual_set_id) ON DELETE RESTRICT,
            is_current boolean NOT NULL, supersedes_best_id uuid NULL REFERENCES trainer.calculated_bests(calculated_best_id) ON DELETE RESTRICT,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL,
            CHECK((source_session_id IS NULL AND source_session_revision_id IS NULL AND source_actual_set_id IS NULL) OR
                  (source_session_id IS NOT NULL AND source_session_revision_id IS NOT NULL AND source_actual_set_id IS NOT NULL))
        );
        CREATE UNIQUE INDEX ux_calculated_best_current ON trainer.calculated_bests(profile_id,exercise_id,metric_kind,condition_id) WHERE is_current;

        CREATE TABLE trainer.products (
            product_id uuid PRIMARY KEY, current_revision_id uuid UNIQUE NOT NULL,
            created_at timestamptz NOT NULL, archived_at timestamptz NULL, logically_deleted_at timestamptz NULL
        );
        CREATE TABLE trainer.product_revisions (
            product_revision_id uuid PRIMARY KEY, product_id uuid NOT NULL REFERENCES trainer.products(product_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1), name text NOT NULL,
            category text NOT NULL REFERENCES trainer.product_category_catalog(code) ON DELETE RESTRICT,
            maker text NULL, brand text NULL, form text NULL, description text NULL,
            rating smallint NULL CHECK(rating BETWEEN 1 AND 5), comment text NULL,
            status text NOT NULL REFERENCES trainer.product_status_catalog(code) ON DELETE RESTRICT,
            supersedes_revision_id uuid NULL, confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, UNIQUE(product_id,revision_no), UNIQUE(product_id,product_revision_id)
        );
        ALTER TABLE trainer.products ADD CONSTRAINT fk_products_current FOREIGN KEY(product_id,current_revision_id)
            REFERENCES trainer.product_revisions(product_id,product_revision_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.product_revisions ADD CONSTRAINT fk_product_revision_supersedes
            FOREIGN KEY(product_id,supersedes_revision_id) REFERENCES trainer.product_revisions(product_id,product_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE trainer.product_compositions (
            composition_id uuid PRIMARY KEY, product_revision_id uuid NOT NULL REFERENCES trainer.product_revisions(product_revision_id) ON DELETE RESTRICT,
            substance_code text NOT NULL, amount numeric(18,6) NOT NULL CHECK(amount > 0),
            amount_unit text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            per_product_amount numeric(18,6) NOT NULL CHECK(per_product_amount > 0),
            per_product_unit text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            source_text text NULL, created_at timestamptz NOT NULL,
            UNIQUE(product_revision_id,substance_code,amount_unit,per_product_unit)
        );
        CREATE TABLE trainer.purchases (
            purchase_id uuid PRIMARY KEY, product_id uuid NOT NULL REFERENCES trainer.products(product_id) ON DELETE RESTRICT,
            purchased_on date NOT NULL, package_count numeric(12,3) NULL CHECK(package_count > 0),
            unit_count numeric(12,3) NULL CHECK(unit_count > 0), price_rub numeric(14,2) NULL CHECK(price_rub >= 0),
            expires_on date NULL, comment text NULL,
            supersedes_purchase_id uuid NULL REFERENCES trainer.purchases(purchase_id) ON DELETE RESTRICT,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK(package_count IS NOT NULL OR unit_count IS NOT NULL)
        );
        CREATE TABLE trainer.intakes (
            intake_id uuid PRIMARY KEY, product_id uuid NOT NULL REFERENCES trainer.products(product_id) ON DELETE RESTRICT,
            session_id uuid NULL REFERENCES trainer.workout_sessions(session_id) ON DELETE RESTRICT,
            taken_at timestamptz NOT NULL, product_quantity numeric(18,6) NOT NULL CHECK(product_quantity > 0),
            product_unit text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            stated_dose numeric(18,6) NULL CHECK(stated_dose > 0), dose_unit text NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            purpose text NULL, comment text NULL, supersedes_intake_id uuid NULL REFERENCES trainer.intakes(intake_id) ON DELETE RESTRICT,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK((stated_dose IS NULL) = (dose_unit IS NULL))
        );
        CREATE TABLE trainer.body_measurement_sessions (
            measurement_session_id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            measured_on date NOT NULL, measured_time time NULL, conditions text NULL, comment text NULL,
            supersedes_session_id uuid NULL REFERENCES trainer.body_measurement_sessions(measurement_session_id) ON DELETE RESTRICT,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.body_measurement_values (
            measurement_value_id uuid PRIMARY KEY,
            measurement_session_id uuid NOT NULL REFERENCES trainer.body_measurement_sessions(measurement_session_id) ON DELETE RESTRICT,
            measurement_kind text NOT NULL REFERENCES trainer.measurement_kind_catalog(code) ON DELETE RESTRICT,
            original_value numeric(18,6) NOT NULL, original_unit text NOT NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            normalized_value numeric(18,6) NULL, normalized_unit text NULL REFERENCES trainer.unit_catalog(code) ON DELETE RESTRICT,
            side text NULL, comment text NULL, created_at timestamptz NOT NULL,
            CHECK((normalized_value IS NULL) = (normalized_unit IS NULL)), UNIQUE(measurement_session_id,measurement_kind,side)
        );

        CREATE TABLE trainer.scientific_sources (
            source_id uuid PRIMARY KEY, citation_key text NOT NULL UNIQUE, title text NOT NULL, authors_or_org text NOT NULL,
            publication_year smallint NOT NULL, source_type text NOT NULL REFERENCES trainer.scientific_source_type_catalog(code) ON DELETE RESTRICT,
            doi text NULL, pmid text NULL, url text NULL, metadata_snapshot jsonb NOT NULL,
            last_checked_at timestamptz NULL,
            availability text NOT NULL REFERENCES trainer.scientific_source_availability_catalog(code) ON DELETE RESTRICT,
            conflict_of_interest_note text NULL, created_at timestamptz NOT NULL, archived_at timestamptz NULL,
            CHECK(doi IS NOT NULL OR pmid IS NOT NULL OR url IS NOT NULL)
        );
        CREATE TABLE trainer.conclusions (
            conclusion_id uuid PRIMARY KEY, topic text NOT NULL, current_version_id uuid NULL UNIQUE,
            created_at timestamptz NOT NULL, archived_at timestamptz NULL
        );
        CREATE TABLE trainer.conclusion_versions (
            conclusion_version_id uuid PRIMARY KEY, conclusion_id uuid NOT NULL REFERENCES trainer.conclusions(conclusion_id) ON DELETE RESTRICT,
            version_no integer NOT NULL CHECK(version_no >= 1), statement text NOT NULL, population_conditions text NOT NULL,
            confidence text NOT NULL CHECK(confidence IN ('high','moderate','low','insufficient')),
            review_due_on date NOT NULL, supersedes_version_id uuid NULL, content_digest char(64) NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL, UNIQUE(conclusion_id,version_no), UNIQUE(conclusion_id,conclusion_version_id)
        );
        ALTER TABLE trainer.conclusions ADD CONSTRAINT fk_conclusions_current FOREIGN KEY(conclusion_id,current_version_id)
            REFERENCES trainer.conclusion_versions(conclusion_id,conclusion_version_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE trainer.conclusion_versions ADD CONSTRAINT fk_conclusion_version_supersedes
            FOREIGN KEY(conclusion_id,supersedes_version_id) REFERENCES trainer.conclusion_versions(conclusion_id,conclusion_version_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE trainer.conclusion_source_links (
            conclusion_version_id uuid NOT NULL REFERENCES trainer.conclusion_versions(conclusion_version_id) ON DELETE RESTRICT,
            source_id uuid NOT NULL REFERENCES trainer.scientific_sources(source_id) ON DELETE RESTRICT,
            relation text NOT NULL CHECK(relation IN ('supports','contradicts','contextualizes')),
            weight numeric(5,4) NULL CHECK(weight BETWEEN 0 AND 1), note text NULL, cited_locator text NULL,
            source_metadata_snapshot jsonb NOT NULL, created_at timestamptz NOT NULL,
            PRIMARY KEY(conclusion_version_id,source_id,relation)
        );
        CREATE INDEX ix_conclusion_source_source ON trainer.conclusion_source_links(source_id);
        CREATE TABLE trainer.applicability_scopes (
            scope_id uuid PRIMARY KEY, conclusion_version_id uuid NOT NULL REFERENCES trainer.conclusion_versions(conclusion_version_id) ON DELETE RESTRICT,
            population text NOT NULL, goal text NULL, activity_or_load text NULL, setting text NULL,
            inclusion jsonb NOT NULL, exclusion jsonb NOT NULL, readable_form text NOT NULL, created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.knowledge_caveats (
            caveat_id uuid PRIMARY KEY, conclusion_version_id uuid NOT NULL REFERENCES trainer.conclusion_versions(conclusion_version_id) ON DELETE RESTRICT,
            caveat_type text NOT NULL CHECK(caveat_type IN ('limitation','conflict')), description text NOT NULL,
            related_conclusion_version_id uuid NULL REFERENCES trainer.conclusion_versions(conclusion_version_id) ON DELETE RESTRICT,
            related_source_id uuid NULL REFERENCES trainer.scientific_sources(source_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.verification_protocol_runs (
            protocol_run_id uuid PRIMARY KEY, conclusion_version_id uuid NOT NULL REFERENCES trainer.conclusion_versions(conclusion_version_id) ON DELETE RESTRICT,
            reviewer_profile_id uuid NOT NULL REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT,
            protocol_version text NOT NULL, checklist_result jsonb NOT NULL,
            decision text NOT NULL CHECK(decision IN ('verified','rejected')), reason text NOT NULL,
            performed_at timestamptz NOT NULL, confirmation_id uuid NOT NULL UNIQUE REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.knowledge_status_revisions (
            status_revision_id uuid PRIMARY KEY,
            conclusion_version_id uuid NOT NULL REFERENCES trainer.conclusion_versions(conclusion_version_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK(revision_no >= 1),
            status text NOT NULL CHECK(status IN ('draft','verified','outdated','rejected')),
            effective_at timestamptz NOT NULL, reason text NOT NULL,
            protocol_run_id uuid NULL REFERENCES trainer.verification_protocol_runs(protocol_run_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL, CHECK(status <> 'verified' OR protocol_run_id IS NOT NULL),
            UNIQUE(conclusion_version_id,revision_no)
        );
        CREATE INDEX ix_knowledge_effective ON trainer.knowledge_status_revisions(conclusion_version_id,effective_at DESC,revision_no DESC);
        CREATE TABLE trainer.recommendations (
            recommendation_id uuid PRIMARY KEY, owner_type text NOT NULL CHECK(owner_type IN ('program_version','plan_revision')),
            program_version_id uuid NULL REFERENCES trainer.program_versions(program_version_id) ON DELETE RESTRICT,
            plan_revision_id uuid NULL REFERENCES trainer.workout_plan_revisions(plan_revision_id) ON DELETE RESTRICT,
            recommendation_kind text NOT NULL REFERENCES trainer.recommendation_kind_catalog(code) ON DELETE RESTRICT,
            decision_text text NOT NULL, confidence text NOT NULL CHECK(confidence IN ('high','moderate','low','insufficient')),
            assumptions jsonb NOT NULL, alternatives jsonb NOT NULL, revisit_condition text NOT NULL,
            confirmation_id uuid NOT NULL REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL,
            CHECK((program_version_id IS NULL) <> (plan_revision_id IS NULL)),
            CHECK((owner_type='program_version') = (program_version_id IS NOT NULL))
        );
        CREATE TABLE trainer.recommendation_fact_links (
            recommendation_id uuid NOT NULL REFERENCES trainer.recommendations(recommendation_id) ON DELETE RESTRICT,
            object_type text NOT NULL REFERENCES trainer.domain_object_type_catalog(code) ON DELETE RESTRICT,
            object_id uuid NOT NULL, revision_id uuid NOT NULL, role text NOT NULL, reason text NOT NULL,
            snapshot_digest char(64) NOT NULL CHECK(snapshot_digest ~ '^[0-9a-f]{64}$'), created_at timestamptz NOT NULL,
            PRIMARY KEY(recommendation_id,object_type,object_id,revision_id,role)
        );
        CREATE TABLE trainer.recommendation_calculation_links (
            recommendation_id uuid NOT NULL REFERENCES trainer.recommendations(recommendation_id) ON DELETE RESTRICT,
            derived_value_id uuid NOT NULL REFERENCES trainer.derived_values(derived_value_id) ON DELETE RESTRICT,
            role text NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY(recommendation_id,derived_value_id,role)
        );
        CREATE TABLE trainer.recommendation_conclusion_links (
            recommendation_id uuid NOT NULL REFERENCES trainer.recommendations(recommendation_id) ON DELETE RESTRICT,
            conclusion_version_id uuid NOT NULL REFERENCES trainer.conclusion_versions(conclusion_version_id) ON DELETE RESTRICT,
            role text NOT NULL, applicability_assessment jsonb NOT NULL,
            status_at_decision text NOT NULL CHECK(status_at_decision IN ('draft','verified','outdated','rejected')),
            created_at timestamptz NOT NULL, PRIMARY KEY(recommendation_id,conclusion_version_id,role)
        );
        CREATE TABLE trainer.recommendation_explanations (
            explanation_id uuid PRIMARY KEY, recommendation_id uuid NOT NULL REFERENCES trainer.recommendations(recommendation_id) ON DELETE RESTRICT,
            detail_level text NOT NULL CHECK(detail_level IN ('short','detailed')), content text NOT NULL,
            generated_at timestamptz NOT NULL, model_provider text NULL, model_name text NULL, is_reasoning boolean NOT NULL,
            shown_in_preview_id uuid NULL REFERENCES trainer.previews(preview_id) ON DELETE RESTRICT, created_at timestamptz NOT NULL
        );

        ALTER TABLE trainer.drafts ADD CONSTRAINT fk_drafts_owner_profile
            FOREIGN KEY(owner_profile_id) REFERENCES trainer.profiles(profile_id) ON DELETE RESTRICT;

        CREATE FUNCTION trainer.guard_program_publication() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.publication_state = OLD.publication_state THEN RETURN NEW; END IF;
            IF NOT ((OLD.publication_state='composing' AND NEW.publication_state='published') OR
                    (OLD.publication_state='published' AND NEW.publication_state='retired')) THEN
                RAISE EXCEPTION 'invalid program publication transition' USING ERRCODE='23514';
            END IF;
            IF NEW.publication_state='published' AND (NEW.published_at IS NULL OR NEW.confirmation_id IS NULL) THEN
                RAISE EXCEPTION 'publication requires confirmation' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_program_publication BEFORE UPDATE OF publication_state ON trainer.program_versions
            FOR EACH ROW EXECUTE FUNCTION trainer.guard_program_publication();

        CREATE VIEW trainer.v_current_profile AS
            SELECT r.*,p.current_revision_id,p.created_at AS identity_created_at,p.archived_at
            FROM trainer.profiles p JOIN trainer.profile_revisions r ON r.profile_revision_id=p.current_revision_id
            WHERE p.archived_at IS NULL;
        CREATE VIEW trainer.v_current_goals AS
            SELECT r.*,g.profile_id,g.parent_goal_id,g.current_revision_id,g.created_at AS identity_created_at,
                   g.archived_at,g.logically_deleted_at
            FROM trainer.goals g JOIN trainer.goal_revisions r ON r.goal_revision_id=g.current_revision_id
            WHERE g.archived_at IS NULL AND g.logically_deleted_at IS NULL;
        CREATE VIEW trainer.v_current_places AS
            SELECT r.*,p.profile_id,p.current_revision_id,p.created_at AS identity_created_at,
                   p.archived_at,p.logically_deleted_at
            FROM trainer.training_places p JOIN trainer.training_place_revisions r ON r.place_revision_id=p.current_revision_id
            WHERE p.archived_at IS NULL AND p.logically_deleted_at IS NULL;
        CREATE VIEW trainer.v_current_equipment AS
            SELECT r.*,e.profile_id,e.current_revision_id,e.created_at AS identity_created_at,
                   e.archived_at,e.logically_deleted_at
            FROM trainer.equipment_items e JOIN trainer.equipment_item_revisions r ON r.equipment_revision_id=e.current_revision_id
            WHERE e.archived_at IS NULL AND e.logically_deleted_at IS NULL;
        CREATE VIEW trainer.v_current_exercises AS
            SELECT r.*,e.current_revision_id,e.created_at AS identity_created_at,e.archived_at,e.logically_deleted_at
            FROM trainer.exercises e JOIN trainer.exercise_revisions r ON r.exercise_revision_id=e.current_revision_id
            WHERE e.archived_at IS NULL AND e.logically_deleted_at IS NULL;
        CREATE VIEW trainer.v_current_products AS
            SELECT r.*,p.current_revision_id,p.created_at AS identity_created_at,p.archived_at,p.logically_deleted_at
            FROM trainer.products p JOIN trainer.product_revisions r ON r.product_revision_id=p.current_revision_id
            WHERE p.archived_at IS NULL AND p.logically_deleted_at IS NULL;
        CREATE VIEW trainer.v_current_plans AS
            SELECT r.*,p.profile_id,p.current_revision_id,p.created_at AS identity_created_at,
                   p.archived_at,p.logically_deleted_at
            FROM trainer.workout_plans p JOIN trainer.workout_plan_revisions r ON r.plan_revision_id=p.current_revision_id
            WHERE p.archived_at IS NULL AND p.logically_deleted_at IS NULL;
        CREATE VIEW trainer.v_current_workout_sessions AS
            SELECT s.session_id,s.profile_id,s.plan_id,s.current_revision_id,s.created_at,s.archived_at,s.logically_deleted_at,
                   r.revision_no,r.plan_revision_id,r.started_at,r.local_start_date,r.ended_at,r.duration_seconds,
                   r.session_type,r.name,r.lifecycle,r.effort,r.comment,r.confirmation_id
            FROM trainer.workout_sessions s JOIN trainer.workout_session_revisions r ON r.session_revision_id=s.current_revision_id
            WHERE s.archived_at IS NULL AND s.logically_deleted_at IS NULL;
        CREATE VIEW trainer.v_effective_knowledge_status AS
            SELECT DISTINCT ON (conclusion_version_id) conclusion_version_id,status,effective_at,reason,protocol_run_id,status_revision_id
            FROM trainer.knowledge_status_revisions ORDER BY conclusion_version_id,effective_at DESC,revision_no DESC;
        CREATE VIEW trainer.v_eligible_conclusion_versions AS
            SELECT cv.*,ks.status,ks.effective_at AS status_effective_at
            FROM trainer.conclusions c JOIN trainer.conclusion_versions cv ON cv.conclusion_version_id=c.current_version_id
            JOIN trainer.v_effective_knowledge_status ks ON ks.conclusion_version_id=cv.conclusion_version_id
            WHERE c.archived_at IS NULL AND ks.status='verified' AND cv.review_due_on >= current_date;
        CREATE VIEW trainer.v_best_results AS
            SELECT r.profile_id,r.exercise_id,'actual_record'::text AS category,r.metric_kind,r.value,r.unit_code,
                   r.achieved_on AS result_date,r.session_id,r.session_revision_id,r.actual_set_id,
                   NULL::uuid AS source_derived_value_id,r.condition_id,r.is_current,r.is_historical_import AS is_historical
            FROM trainer.actual_records r
            UNION ALL
            SELECT b.profile_id,b.exercise_id,'calculated_best'::text,b.metric_kind,b.value,b.unit_code,
                   b.achieved_on,b.source_session_id,b.source_session_revision_id,b.source_actual_set_id,
                   b.derived_value_id,b.condition_id,b.is_current,false
            FROM trainer.calculated_bests b;

        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0002_domain_schema','537dd916fa19962440b6a10c2d077f18f0a0c94c2f1d4af079787365105e70d8',now(),0,'2',now());

        GRANT SELECT,INSERT ON ALL TABLES IN SCHEMA trainer TO trainer_app;
        REVOKE DELETE,TRUNCATE ON ALL TABLES IN SCHEMA trainer FROM trainer_app;
        GRANT UPDATE(current_revision_id,archived_at) ON trainer.profiles TO trainer_app;
        GRANT UPDATE(current_revision_id,archived_at,logically_deleted_at) ON trainer.goals,trainer.training_places,
            trainer.equipment_items,trainer.exercises,trainer.workout_plans,trainer.workout_sessions,trainer.products TO trainer_app;
        GRANT UPDATE(current_version_id,archived_at) ON trainer.conclusions TO trainer_app;
        GRANT UPDATE(publication_state,published_at,confirmation_id) ON trainer.program_versions TO trainer_app;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "domain baseline is forward-only; destructive downgrade requires owner approval"
    )
