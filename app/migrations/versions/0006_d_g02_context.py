"""Enable the approved D-G02 context slice and seed bundle.

Revision ID: 0006_d_g02_context
Revises: 0005_concurrency_and_integrity
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_d_g02_context"
down_revision: str | None = "0005_concurrency_and_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_CHECKSUM = "5a670d1247e3220a9da7afaf7d84d9ae18efee73377b142589b1f919ab05d380"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE trainer.drafts ALTER COLUMN owner_profile_id DROP NOT NULL;
        ALTER TABLE trainer.drafts ADD CONSTRAINT ck_drafts_owner_scope CHECK (
            owner_profile_id IS NOT NULL OR intent_type = 'onboarding'
        );
        ALTER TABLE trainer.equipment_item_revisions ALTER COLUMN condition DROP NOT NULL;
        ALTER TABLE trainer.equipment_item_revisions ALTER COLUMN availability DROP NOT NULL;
        ALTER TABLE trainer.idempotent_operations ADD COLUMN response_payload jsonb NULL;
        ALTER TABLE trainer.idempotent_operations DROP CONSTRAINT ck_operation_commit_result;
        ALTER TABLE trainer.idempotent_operations ADD CONSTRAINT ck_operation_commit_result CHECK (
            state <> 'committed' OR
            (command_kind = 'CMD-00' AND commit_result_id IS NOT NULL) OR
            (command_kind <> 'CMD-00' AND response_payload IS NOT NULL)
        );
        GRANT UPDATE (response_payload) ON trainer.idempotent_operations TO trainer_app;
        GRANT UPDATE (parent_goal_id) ON trainer.goals TO trainer_app;

        CREATE TABLE trainer.preview_show_events (
            show_event_id uuid PRIMARY KEY,
            preview_id uuid NOT NULL REFERENCES trainer.previews(preview_id) ON DELETE RESTRICT,
            draft_revision_id uuid NOT NULL REFERENCES trainer.draft_revisions(draft_revision_id) ON DELETE RESTRICT,
            preview_digest char(64) NOT NULL CHECK (preview_digest ~ '^[0-9a-f]{{64}}$'),
            displayed_text_digest char(64) NOT NULL CHECK (displayed_text_digest ~ '^[0-9a-f]{{64}}$'),
            conversation_id text NOT NULL,
            shown_message_id text NOT NULL,
            cross_dialog boolean NOT NULL,
            shown_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            UNIQUE(preview_id,conversation_id,shown_message_id)
        );
        CREATE INDEX ix_preview_show_events_lookup
            ON trainer.preview_show_events(preview_id,conversation_id,shown_at DESC);
        GRANT SELECT,INSERT ON trainer.preview_show_events TO trainer_app;

        INSERT INTO trainer.goal_type_catalog(code,description,seed_version,created_at)
        VALUES ('performance','Performance goal',1,now()) ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.place_type_catalog(code,description,seed_version,created_at)
        VALUES ('home_gym','Home gym',1,now()) ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.equipment_type_catalog(code,description,seed_version,created_at)
        VALUES ('barbell','Barbell',1,now()) ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.unit_catalog(code,description,seed_version,created_at) VALUES
            ('repetition','Repetition',1,now()),('kilogram','Kilogram',1,now())
        ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at) VALUES
            ('onboarding','Onboarding package',1,now()),('goal','Goal change',1,now())
        ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.dependency_type_catalog(code,description,seed_version,created_at) VALUES
            ('profile','Profile revision',1,now()),('goal','Goal revision',1,now())
        ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.domain_object_type_catalog(code,description,seed_version,created_at) VALUES
            ('profile','Profile',1,now()),('goal','Goal',1,now()),
            ('training_place','Training place',1,now()),
            ('equipment_item','Equipment item',1,now()),
            ('place_equipment_availability','Place equipment availability',1,now())
        ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.domain_operation_catalog(code,description,seed_version,created_at) VALUES
            ('create','Create',1,now()),('correct','Correct',1,now()),('cancel','Cancel',1,now())
        ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.initiator_kind_catalog(code,description,seed_version,created_at)
        VALUES ('ai_agent','AI agent acting through controlled API',1,now()) ON CONFLICT (code) DO NOTHING;
        INSERT INTO trainer.audit_result_kind_catalog(code,description,seed_version,created_at) VALUES
            ('succeeded','Succeeded',1,now()),('failed','Failed',1,now())
        ON CONFLICT (code) DO NOTHING;

        INSERT INTO trainer.seed_versions(seed_name,seed_version,checksum,applied_at,created_at)
        VALUES ('d_g02_context',1,'bd210320d6a4bd5b55b7eb2c42903b9d748ddfd581c31439ed0dc986f8c62953',now(),now())
        ON CONFLICT (seed_name,seed_version) DO NOTHING;
        DO $seed_validation$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM trainer.goal_type_catalog WHERE code='performance' AND description='Performance goal' AND seed_version=1)
               OR NOT EXISTS (SELECT 1 FROM trainer.place_type_catalog WHERE code='home_gym' AND description='Home gym' AND seed_version=1)
               OR NOT EXISTS (SELECT 1 FROM trainer.equipment_type_catalog WHERE code='barbell' AND description='Barbell' AND seed_version=1)
               OR (SELECT count(*) FROM trainer.unit_catalog WHERE (code,description,seed_version) IN (('repetition','Repetition',1),('kilogram','Kilogram',1))) <> 2
               OR (SELECT count(*) FROM trainer.intent_type_catalog WHERE (code,description,seed_version) IN (('onboarding','Onboarding package',1),('goal','Goal change',1))) <> 2
               OR (SELECT count(*) FROM trainer.dependency_type_catalog WHERE (code,description,seed_version) IN (('profile','Profile revision',1),('goal','Goal revision',1))) <> 2
               OR (SELECT count(*) FROM trainer.domain_object_type_catalog WHERE (code,description,seed_version) IN (('profile','Profile',1),('goal','Goal',1),('training_place','Training place',1),('equipment_item','Equipment item',1),('place_equipment_availability','Place equipment availability',1))) <> 5
               OR (SELECT count(*) FROM trainer.domain_operation_catalog WHERE (code,description,seed_version) IN (('create','Create',1),('correct','Correct',1),('cancel','Cancel',1))) <> 3
               OR NOT EXISTS (SELECT 1 FROM trainer.initiator_kind_catalog WHERE code='ai_agent' AND description='AI agent acting through controlled API' AND seed_version=1)
               OR (SELECT count(*) FROM trainer.audit_result_kind_catalog WHERE (code,description,seed_version) IN (('succeeded','Succeeded',1),('failed','Failed',1))) <> 2
               OR NOT EXISTS (SELECT 1 FROM trainer.seed_versions WHERE seed_name='d_g02_context' AND seed_version=1 AND checksum='bd210320d6a4bd5b55b7eb2c42903b9d748ddfd581c31439ed0dc986f8c62953')
            THEN
                RAISE EXCEPTION 'D-G02 seed conflict';
            END IF;
        END
        $seed_validation$;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at
        ) VALUES ('0006_d_g02_context','{MIGRATION_CHECKSUM}',now(),0,'6',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G02 migration is forward-only; downgrade requires owner approval")
