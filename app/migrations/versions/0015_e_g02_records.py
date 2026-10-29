"""Harden the existing record tables for E-G02."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_e_g02_records"
down_revision: str | None = "0014_d_g06_workouts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None
MIGRATION_CHECKSUM = "50560bdc9cb058dca8bd237c0bfc167088afd20545025acb55fa1b4495d6aaa9"
SEED_CHECKSUM = "fb6c73a6a0f75a91081848571afa3d5e7b3a8fa0f1a59d1792d6d7138f780add"


def upgrade() -> None:
    op.execute(
        f"""
        DO $preflight$ DECLARE v_workouts bigint; v_candidates bigint; v_records bigint; BEGIN
          SELECT count(*) INTO v_workouts FROM trainer.workout_sessions;
          SELECT count(*) INTO v_candidates FROM trainer.record_candidates;
          SELECT count(*) INTO v_records FROM trainer.actual_records;
          IF v_workouts > 0 OR v_candidates > 0 OR v_records > 0 THEN
            RAISE EXCEPTION 'E-G02 preflight failed; workouts=%, candidates=%, records=%; existing data requires owner plan',
              v_workouts, v_candidates, v_records USING ERRCODE='23514';
          END IF;
          IF EXISTS(SELECT 1 FROM trainer.metric_kind_catalog WHERE code LIKE 'record_%')
             OR EXISTS(SELECT 1 FROM trainer.intent_type_catalog WHERE code IN ('record_decision','historical_record'))
             OR EXISTS(SELECT 1 FROM trainer.domain_object_type_catalog WHERE code IN ('record_candidate','actual_record'))
          THEN RAISE EXCEPTION 'E-G02 seed code conflict' USING ERRCODE='23514'; END IF;
        END $preflight$;

        ALTER TABLE trainer.record_candidates ADD COLUMN exercise_revision_id uuid NOT NULL;
        ALTER TABLE trainer.record_candidates ADD COLUMN invalidated_reason text NULL
          CHECK(invalidated_reason IS NULL OR invalidated_reason IN
            ('source_corrected','source_revision_superseded'));
        ALTER TABLE trainer.actual_records ADD COLUMN exercise_revision_id uuid NOT NULL;
        ALTER TABLE trainer.actual_records ADD COLUMN candidate_id uuid NULL
          REFERENCES trainer.record_candidates(candidate_id) ON DELETE RESTRICT;
        ALTER TABLE trainer.actual_records ADD COLUMN source_fingerprint text NULL;
        ALTER TABLE trainer.record_candidates ADD CONSTRAINT fk_candidate_exact_exercise_revision
          FOREIGN KEY(exercise_id,exercise_revision_id)
          REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id) ON DELETE RESTRICT;
        ALTER TABLE trainer.actual_records ADD CONSTRAINT fk_record_exact_exercise_revision
          FOREIGN KEY(exercise_id,exercise_revision_id)
          REFERENCES trainer.exercise_revisions(exercise_id,exercise_revision_id) ON DELETE RESTRICT;
        ALTER TABLE trainer.record_candidates ADD CONSTRAINT uq_candidate_source_condition
          UNIQUE(session_revision_id,exercise_id,metric_kind,condition_id);
        ALTER TABLE trainer.actual_records ADD CONSTRAINT ck_record_source_shape
          CHECK ((is_historical_import AND session_id IS NULL AND session_revision_id IS NULL
                    AND actual_set_id IS NULL AND candidate_id IS NULL)
              OR (NOT is_historical_import AND session_id IS NOT NULL AND session_revision_id IS NOT NULL
                    AND actual_set_id IS NOT NULL AND candidate_id IS NOT NULL));
        ALTER TABLE trainer.actual_records ADD CONSTRAINT uq_actual_record_candidate UNIQUE(candidate_id);
        CREATE INDEX ix_candidates_source_order ON trainer.record_candidates(session_revision_id,created_at,candidate_id);
        CREATE INDEX ix_records_history ON trainer.actual_records(profile_id,achieved_on DESC,record_id DESC);
        CREATE INDEX ix_records_condition_history ON trainer.actual_records(profile_id,exercise_id,metric_kind,condition_id,is_current);

        CREATE FUNCTION trainer.guard_record_candidate_transition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
             OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
             OR NEW.exercise_id IS DISTINCT FROM OLD.exercise_id
             OR NEW.exercise_revision_id IS DISTINCT FROM OLD.exercise_revision_id
             OR NEW.metric_kind IS DISTINCT FROM OLD.metric_kind
             OR NEW.value IS DISTINCT FROM OLD.value
             OR NEW.unit_code IS DISTINCT FROM OLD.unit_code
             OR NEW.condition_id IS DISTINCT FROM OLD.condition_id
             OR NEW.session_revision_id IS DISTINCT FROM OLD.session_revision_id
             OR NEW.actual_set_id IS DISTINCT FROM OLD.actual_set_id
             OR NEW.technique_self_report IS DISTINCT FROM OLD.technique_self_report
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'record candidate content is immutable' USING ERRCODE='23514';
          END IF;
          IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
              (OLD.status='pending' AND NEW.status IN ('accepted','rejected','invalidated'))
              OR (OLD.status='accepted' AND NEW.status='invalidated')) THEN
            RAISE EXCEPTION 'invalid record candidate transition' USING ERRCODE='23514';
          END IF;
          IF NEW.status='invalidated' AND NEW.invalidated_reason IS NULL THEN
            RAISE EXCEPTION 'invalidated candidate requires a reason' USING ERRCODE='23514';
          END IF;
          IF NEW.status<>'invalidated' AND NEW.invalidated_reason IS NOT NULL THEN
            RAISE EXCEPTION 'only invalidated candidate has an invalidation reason' USING ERRCODE='23514';
          END IF;
          IF NEW.status IN ('accepted','rejected') AND NEW.decision_confirmation_id IS NULL THEN
            RAISE EXCEPTION 'decided candidate requires confirmation' USING ERRCODE='23514';
          END IF;
          IF NEW.status='pending' AND NEW.decision_confirmation_id IS NOT NULL THEN
            RAISE EXCEPTION 'pending candidate cannot have decision confirmation' USING ERRCODE='23514';
          END IF;
          IF OLD.decision_confirmation_id IS NOT NULL
             AND NEW.decision_confirmation_id IS DISTINCT FROM OLD.decision_confirmation_id THEN
            RAISE EXCEPTION 'candidate decision confirmation is immutable' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_record_candidate_transition BEFORE UPDATE ON trainer.record_candidates
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_record_candidate_transition();

        CREATE FUNCTION trainer.guard_actual_record_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.record_id IS DISTINCT FROM OLD.record_id
             OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
             OR NEW.exercise_id IS DISTINCT FROM OLD.exercise_id
             OR NEW.metric_kind IS DISTINCT FROM OLD.metric_kind
             OR NEW.value IS DISTINCT FROM OLD.value
             OR NEW.unit_code IS DISTINCT FROM OLD.unit_code
             OR NEW.achieved_on IS DISTINCT FROM OLD.achieved_on
             OR NEW.condition_id IS DISTINCT FROM OLD.condition_id
             OR NEW.session_id IS DISTINCT FROM OLD.session_id
             OR NEW.session_revision_id IS DISTINCT FROM OLD.session_revision_id
             OR NEW.exercise_revision_id IS DISTINCT FROM OLD.exercise_revision_id
             OR NEW.actual_set_id IS DISTINCT FROM OLD.actual_set_id
             OR NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
             OR NEW.weight_value IS DISTINCT FROM OLD.weight_value
             OR NEW.weight_unit IS DISTINCT FROM OLD.weight_unit
             OR NEW.reps IS DISTINCT FROM OLD.reps
             OR NEW.duration_seconds IS DISTINCT FROM OLD.duration_seconds
             OR NEW.body_mass IS DISTINCT FROM OLD.body_mass
             OR NEW.comment IS DISTINCT FROM OLD.comment
             OR NEW.is_historical_import IS DISTINCT FROM OLD.is_historical_import
             OR NEW.supersedes_record_id IS DISTINCT FROM OLD.supersedes_record_id
             OR NEW.confirmation_id IS DISTINCT FROM OLD.confirmation_id
             OR NEW.source_fingerprint IS DISTINCT FROM OLD.source_fingerprint
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'actual record content is immutable' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_actual_record_immutable BEFORE UPDATE ON trainer.actual_records
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_actual_record_immutable();

        CREATE FUNCTION trainer.verify_candidate_record_link() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE c trainer.record_candidates%ROWTYPE; v_session uuid;
        BEGIN
          IF NEW.is_historical_import THEN
            IF NEW.candidate_id IS NOT NULL OR NEW.session_id IS NOT NULL
               OR NEW.session_revision_id IS NOT NULL OR NEW.actual_set_id IS NOT NULL THEN
              RAISE EXCEPTION 'historical record cannot have workout source references' USING ERRCODE='23514';
            END IF;
          ELSE
            SELECT * INTO c FROM trainer.record_candidates WHERE candidate_id=NEW.candidate_id;
            SELECT session_id INTO v_session FROM trainer.workout_session_revisions
              WHERE session_revision_id=c.session_revision_id;
            IF c.candidate_id IS NULL OR NOT (
                 c.status='accepted'
                 OR (c.status='invalidated' AND c.invalidated_reason='source_corrected'
                     AND EXISTS (
                       SELECT 1 FROM trainer.domain_changes d
                       WHERE d.object_type='actual_record' AND d.object_id=NEW.record_id
                         AND d.after_snapshot->>'validity'='source_invalidated'
                     ))
               )
               OR c.decision_confirmation_id IS DISTINCT FROM NEW.confirmation_id
               OR c.profile_id IS DISTINCT FROM NEW.profile_id
               OR c.exercise_id IS DISTINCT FROM NEW.exercise_id
               OR c.exercise_revision_id IS DISTINCT FROM NEW.exercise_revision_id
               OR c.metric_kind IS DISTINCT FROM NEW.metric_kind
               OR c.value IS DISTINCT FROM NEW.value OR c.unit_code IS DISTINCT FROM NEW.unit_code
               OR c.condition_id IS DISTINCT FROM NEW.condition_id
               OR c.session_revision_id IS DISTINCT FROM NEW.session_revision_id
               OR c.actual_set_id IS DISTINCT FROM NEW.actual_set_id
               OR v_session IS DISTINCT FROM NEW.session_id THEN
              RAISE EXCEPTION 'actual record does not match its candidate' USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_record_candidate_link
          AFTER INSERT OR UPDATE
          ON trainer.actual_records DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION trainer.verify_candidate_record_link();

        CREATE FUNCTION trainer.verify_actual_record_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM trainer.domain_changes d
            WHERE d.object_type='actual_record' AND d.object_id=NEW.record_id
              AND d.after_snapshot->>'is_current'=NEW.is_current::text
          ) THEN
            RAISE EXCEPTION 'actual record change requires matching DomainChange' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER trg_actual_record_change
          AFTER INSERT OR UPDATE ON trainer.actual_records DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION trainer.verify_actual_record_change();

        GRANT UPDATE(status,decision_confirmation_id,invalidated_reason)
          ON trainer.record_candidates TO trainer_app;
        REVOKE UPDATE,DELETE ON trainer.comparison_conditions FROM trainer_app;

        INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at) VALUES
          ('record_full_reps','Record full repetitions',1,now()),
          ('record_external_load','Record external load',1,now()),
          ('record_hold_duration','Record hold duration',1,now()),
          ('record_cardio_time','Record cardio time',1,now());
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at) VALUES
          ('record_decision','Record candidate decision',1,now()),
          ('historical_record','Historical record',1,now());
        INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at) VALUES
          ('record_candidate','Record candidate',1,now()),
          ('actual_record','Actual record',1,now());
        INSERT INTO trainer.dependency_type_catalog(code,description,seed_version,created_at) VALUES
          ('record_candidate','Record candidate',1,now()),
          ('actual_record','Actual record',1,now());
        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('e_g02_records',1,'{SEED_CHECKSUM}',now(),now());
        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0015_e_g02_records','{MIGRATION_CHECKSUM}',now(),0,'15',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("E-G02 migration is forward-only")
