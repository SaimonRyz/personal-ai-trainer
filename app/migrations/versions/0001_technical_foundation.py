"""Create migration ledgers, catalogs, and confirmation/idempotency foundation.

Revision ID: 0001_technical_foundation
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_technical_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "e8d8a97493c24753a53b395f1727327543b075eb7a476e9a9b1debee24f78cf3"
SEED_CHECKSUM = "7c9dc3bceb72d052d1656a5aff6754ccdd33d9b22db7013c17e6cd2a1b033b95"

CATALOGS = (
    "exercise_category",
    "metric_kind",
    "unit",
    "equipment_type",
    "load_kind",
    "load_direction",
    "set_type",
    "block_type",
    "product_category",
    "measurement_kind",
    "domain_object_type",
    "goal_type",
    "place_type",
    "equipment_condition",
    "equipment_availability",
    "equipment_event_kind",
    "exercise_status",
    "muscle_group_status",
    "program_status",
    "day_type",
    "plan_type",
    "session_type",
    "actual_set_status",
    "product_status",
    "scientific_source_type",
    "scientific_source_availability",
    "recommendation_kind",
    "domain_operation",
    "initiator_kind",
    "audit_result_kind",
    "derived_method",
    "derived_value_kind",
    "comparison_rule",
    "intent_type",
    "dependency_type",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trainer.schema_migrations (
            migration_id text PRIMARY KEY,
            checksum char(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
            applied_at timestamptz NOT NULL,
            duration_ms integer NOT NULL CHECK (duration_ms >= 0),
            app_schema_version text NOT NULL,
            created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.migration_attempts (
            attempt_id uuid PRIMARY KEY,
            migration_id text NOT NULL,
            checksum char(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
            started_at timestamptz NOT NULL,
            finished_at timestamptz NULL,
            result text NOT NULL CHECK (result IN ('running','succeeded','failed')),
            error_code text NULL,
            created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.seed_versions (
            seed_name text NOT NULL,
            seed_version integer NOT NULL CHECK (seed_version >= 1),
            checksum char(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
            applied_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (seed_name, seed_version)
        );
        """
    )
    for catalog in CATALOGS:
        op.execute(
            f"""
            CREATE TABLE trainer.{catalog}_catalog (
                code text PRIMARY KEY,
                description text NOT NULL,
                seed_version integer NOT NULL CHECK (seed_version >= 1),
                archived_at timestamptz NULL,
                created_at timestamptz NOT NULL
            )
            """
        )
    op.execute(
        """
        CREATE TABLE trainer.drafts (
            draft_id uuid PRIMARY KEY,
            intent_type text NOT NULL REFERENCES trainer.intent_type_catalog(code) ON DELETE RESTRICT,
            owner_profile_id uuid NOT NULL,
            state text NOT NULL CHECK (state IN ('open','confirmed','expired','cancelled')),
            latest_revision_id uuid NULL,
            expires_at timestamptz NOT NULL,
            cancelled_at timestamptz NULL,
            created_at timestamptz NOT NULL,
            CONSTRAINT ck_drafts_expiry CHECK (expires_at > created_at)
        );
        CREATE TABLE trainer.draft_revisions (
            draft_revision_id uuid PRIMARY KEY,
            draft_id uuid NOT NULL REFERENCES trainer.drafts(draft_id) ON DELETE RESTRICT,
            revision_no integer NOT NULL CHECK (revision_no >= 1),
            typed_payload jsonb NOT NULL,
            content_digest char(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
            trust_state text NOT NULL CHECK (trust_state IN ('draft','shown','superseded')),
            confirmation_kind text NOT NULL CHECK (
                confirmation_kind IN ('normal','publication','owner_verification','dangerous','batch')
            ),
            created_at timestamptz NOT NULL,
            UNIQUE (draft_id, revision_no),
            UNIQUE (draft_id, draft_revision_id)
        );
        ALTER TABLE trainer.drafts ADD CONSTRAINT fk_drafts_latest_revision
            FOREIGN KEY (draft_id, latest_revision_id)
            REFERENCES trainer.draft_revisions(draft_id, draft_revision_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE trainer.draft_dependencies (
            draft_dependency_id uuid PRIMARY KEY,
            draft_revision_id uuid NOT NULL REFERENCES trainer.draft_revisions(draft_revision_id) ON DELETE RESTRICT,
            dependency_type text NOT NULL REFERENCES trainer.dependency_type_catalog(code) ON DELETE RESTRICT,
            dependency_object_id uuid NOT NULL,
            dependency_revision_id uuid NOT NULL,
            dependency_digest char(64) NOT NULL CHECK (dependency_digest ~ '^[0-9a-f]{64}$'),
            reason text NOT NULL,
            created_at timestamptz NOT NULL,
            UNIQUE (draft_revision_id, dependency_type, dependency_object_id, dependency_revision_id)
        );
        CREATE TABLE trainer.previews (
            preview_id uuid PRIMARY KEY,
            draft_revision_id uuid NOT NULL REFERENCES trainer.draft_revisions(draft_revision_id) ON DELETE RESTRICT,
            canonical_payload jsonb NOT NULL,
            canonical_text text NOT NULL,
            content_digest char(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
            conversation_id text NOT NULL,
            shown_message_id text NULL,
            detail_level text NOT NULL,
            is_complete boolean NOT NULL,
            returned_as_shown_at timestamptz NULL,
            created_at timestamptz NOT NULL,
            CONSTRAINT ck_previews_shown_pair CHECK (
                (shown_message_id IS NULL) = (returned_as_shown_at IS NULL)
            )
        );
        CREATE TABLE trainer.operation_capabilities (
            capability_id uuid PRIMARY KEY,
            capability_digest char(64) NOT NULL UNIQUE CHECK (capability_digest ~ '^[0-9a-f]{64}$'),
            operation_kind text NOT NULL,
            preview_id uuid NOT NULL REFERENCES trainer.previews(preview_id) ON DELETE RESTRICT,
            request_digest char(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
            expires_at timestamptz NOT NULL,
            consumed_at timestamptz NULL,
            created_at timestamptz NOT NULL,
            CONSTRAINT ck_operation_capability_expiry CHECK (expires_at > created_at)
        );
        CREATE TABLE trainer.confirmations (
            confirmation_id uuid PRIMARY KEY,
            preview_id uuid NOT NULL UNIQUE REFERENCES trainer.previews(preview_id) ON DELETE RESTRICT,
            draft_revision_id uuid NOT NULL REFERENCES trainer.draft_revisions(draft_revision_id) ON DELETE RESTRICT,
            conversation_id text NOT NULL,
            user_message_id text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('normal','publication','owner_verification','dangerous','batch')),
            explicit_expression text NOT NULL,
            confirmed_at timestamptz NOT NULL,
            capability_id uuid NULL REFERENCES trainer.operation_capabilities(capability_id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL
        );
        CREATE TABLE trainer.idempotent_operations (
            operation_id uuid PRIMARY KEY,
            scope text NOT NULL,
            idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
            command_kind text NOT NULL,
            request_digest char(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
            state text NOT NULL CHECK (state IN ('received','executing','committed','failed','recovery_required')),
            lease_expires_at timestamptz NULL,
            commit_result_id uuid NULL UNIQUE,
            error_code text NULL,
            received_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            UNIQUE (scope, idempotency_key),
            CONSTRAINT ck_operation_commit_result CHECK (
                state <> 'committed' OR commit_result_id IS NOT NULL
            )
        );
        CREATE TABLE trainer.commit_results (
            commit_result_id uuid PRIMARY KEY,
            confirmation_id uuid NOT NULL UNIQUE REFERENCES trainer.confirmations(confirmation_id) ON DELETE RESTRICT,
            operation_id uuid NOT NULL UNIQUE REFERENCES trainer.idempotent_operations(operation_id) ON DELETE RESTRICT,
            result_payload jsonb NOT NULL,
            committed_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL
        );
        ALTER TABLE trainer.idempotent_operations ADD CONSTRAINT fk_operations_commit_result
            FOREIGN KEY (commit_result_id) REFERENCES trainer.commit_results(commit_result_id) ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE trainer.domain_changes (
            domain_change_id uuid PRIMARY KEY,
            commit_result_id uuid NOT NULL REFERENCES trainer.commit_results(commit_result_id) ON DELETE RESTRICT,
            object_type text NOT NULL REFERENCES trainer.domain_object_type_catalog(code) ON DELETE RESTRICT,
            object_id uuid NOT NULL,
            new_revision_id uuid NULL,
            operation text NOT NULL REFERENCES trainer.domain_operation_catalog(code) ON DELETE RESTRICT,
            before_snapshot jsonb NULL,
            after_snapshot jsonb NULL,
            initiator text NOT NULL REFERENCES trainer.initiator_kind_catalog(code) ON DELETE RESTRICT,
            reason text NULL,
            occurred_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            CONSTRAINT ck_domain_change_snapshot CHECK (
                before_snapshot IS NOT NULL OR after_snapshot IS NOT NULL
            )
        );
        CREATE TABLE trainer.agent_operation_audit (
            audit_id uuid PRIMARY KEY,
            operation_id uuid NULL REFERENCES trainer.idempotent_operations(operation_id) ON DELETE RESTRICT,
            method_id text NOT NULL,
            object_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            attempt_no integer NOT NULL CHECK (attempt_no >= 1),
            result_kind text NOT NULL REFERENCES trainer.audit_result_kind_catalog(code) ON DELETE RESTRICT,
            correlation_id uuid NOT NULL,
            causation_id uuid NULL,
            idempotency_key_digest char(64) NULL CHECK (
                idempotency_key_digest IS NULL OR idempotency_key_digest ~ '^[0-9a-f]{64}$'
            ),
            error_code text NULL,
            started_at timestamptz NOT NULL,
            finished_at timestamptz NULL,
            latency_ms integer NULL CHECK (latency_ms >= 0),
            safe_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL
        );
        CREATE INDEX ix_drafts_open_expiry ON trainer.drafts(state, expires_at) WHERE state = 'open';
        CREATE INDEX ix_previews_complete_conversation ON trainer.previews(conversation_id, created_at DESC)
            WHERE is_complete;
        CREATE INDEX ix_operations_recovery ON trainer.idempotent_operations(state, updated_at)
            WHERE state IN ('executing','recovery_required');
        CREATE INDEX ix_audit_correlation ON trainer.agent_operation_audit(correlation_id);
        CREATE INDEX ix_audit_method_time ON trainer.agent_operation_audit(method_id, started_at DESC);
        CREATE INDEX ix_domain_changes_object_time ON trainer.domain_changes(object_type, object_id, occurred_at DESC);

        CREATE FUNCTION trainer.guard_draft_trust_state() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.trust_state = OLD.trust_state THEN RETURN NEW; END IF;
            IF NOT ((OLD.trust_state = 'draft' AND NEW.trust_state = 'shown') OR
                    (OLD.trust_state = 'shown' AND NEW.trust_state = 'superseded')) THEN
                RAISE EXCEPTION 'invalid draft trust transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_draft_trust_state BEFORE UPDATE OF trust_state ON trainer.draft_revisions
            FOR EACH ROW EXECUTE FUNCTION trainer.guard_draft_trust_state();

        CREATE FUNCTION trainer.guard_preview_shown_once() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.shown_message_id IS NOT NULL OR OLD.returned_as_shown_at IS NOT NULL THEN
                RAISE EXCEPTION 'preview shown marker is immutable' USING ERRCODE = '23514';
            END IF;
            IF NEW.shown_message_id IS NULL OR NEW.returned_as_shown_at IS NULL THEN
                RAISE EXCEPTION 'preview shown fields change together' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_preview_shown_once
            BEFORE UPDATE OF shown_message_id, returned_as_shown_at ON trainer.previews
            FOR EACH ROW EXECUTE FUNCTION trainer.guard_preview_shown_once();

        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('catalog_schema',1,'7c9dc3bceb72d052d1656a5aff6754ccdd33d9b22db7013c17e6cd2a1b033b95',now(),now())
        ON CONFLICT (seed_name,seed_version) DO NOTHING;
        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0001_technical_foundation','e8d8a97493c24753a53b395f1727327543b075eb7a476e9a9b1debee24f78cf3',now(),0,'1',now());
        """
    )
    op.execute(
        """
        GRANT USAGE ON SCHEMA trainer TO trainer_app;
        GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA trainer TO trainer_app;
        REVOKE DELETE, TRUNCATE ON ALL TABLES IN SCHEMA trainer FROM trainer_app;
        GRANT UPDATE (state, latest_revision_id, cancelled_at) ON trainer.drafts TO trainer_app;
        GRANT UPDATE (trust_state) ON trainer.draft_revisions TO trainer_app;
        GRANT UPDATE (shown_message_id, returned_as_shown_at) ON trainer.previews TO trainer_app;
        GRANT UPDATE (state, lease_expires_at, commit_result_id, error_code, updated_at)
            ON trainer.idempotent_operations TO trainer_app;
        GRANT UPDATE (consumed_at) ON trainer.operation_capabilities TO trainer_app;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "baseline migration is forward-only; destructive downgrade requires owner approval"
    )
