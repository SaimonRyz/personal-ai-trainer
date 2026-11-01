"""Name historical workout import and bind one confirmation to exact child revisions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_workout_import_batch"
down_revision: str | None = "0026_cardio_exercise_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "8bf04dd86bc9d83c1a2c7e471deb09bcde39b36b713f4a90aa162f6d90e429eb"


def upgrade() -> None:
    op.execute(f"""
        DO $preflight$ BEGIN
          IF EXISTS (
            SELECT 1 FROM trainer.intent_type_catalog
            WHERE code='workout_import_batch'
              AND (description <> 'Historical workout import batch' OR seed_version <> 1
                   OR archived_at IS NOT NULL)
          ) THEN RAISE EXCEPTION 'workout import batch seed conflict'; END IF;
        END $preflight$;
        INSERT INTO trainer.intent_type_catalog(code,description,seed_version,created_at)
        VALUES ('workout_import_batch','Historical workout import batch',1,now())
        ON CONFLICT(code) DO NOTHING;
        INSERT INTO trainer.schema_migrations(
            migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0027_workout_import_batch','{MIGRATION_CHECKSUM}',now(),0,'27',now());
    """)
    op.execute("""
        CREATE FUNCTION trainer.workout_confirmation_matches(
            p_confirmation_id uuid, p_revision_id uuid
        ) RETURNS boolean LANGUAGE sql STABLE AS $function$
          SELECT EXISTS (
            SELECT 1 FROM trainer.confirmations c
            JOIN trainer.draft_revisions dr ON dr.draft_revision_id=c.draft_revision_id
            JOIN trainer.drafts d ON d.draft_id=dr.draft_id
            WHERE c.confirmation_id=p_confirmation_id
              AND (
                c.draft_revision_id=p_revision_id
                OR (
                  d.intent_type='workout_import_batch'
                  AND EXISTS (
                    SELECT 1 FROM jsonb_array_elements(dr.typed_payload->'entries') entry
                    WHERE entry->>'decision' IN ('create','correct')
                      AND entry->>'report_draft_revision_id'=p_revision_id::text
                      AND entry->'report_draft'->>'session_revision_id'=p_revision_id::text
                      AND entry->'report_draft'->>'profile_id'=d.owner_profile_id::text
                  )
                )
              )
          )
        $function$;

        CREATE OR REPLACE FUNCTION trainer.guard_workout_session_insert()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE same_tx boolean; v_confirmation_id uuid; v_owner uuid;
        BEGIN
          IF TG_OP='INSERT' THEN
            SELECT EXISTS (
              SELECT 1 FROM trainer.confirmations c
              WHERE c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
                AND trainer.workout_confirmation_matches(c.confirmation_id,NEW.current_revision_id)
            ) INTO same_tx;
          ELSE
            SELECT r.confirmation_id,r.session_id INTO v_confirmation_id,v_owner
              FROM trainer.workout_session_revisions r
              WHERE r.session_revision_id=NEW.current_revision_id;
            IF v_owner IS DISTINCT FROM OLD.session_id OR NEW.session_id IS DISTINCT FROM OLD.session_id
               OR NEW.current_revision_id=OLD.current_revision_id THEN
              RAISE EXCEPTION 'workout current revision must advance within its owner' USING ERRCODE='23514';
            END IF;
            SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
              AND trainer.workout_confirmation_matches(c.confirmation_id,NEW.current_revision_id)
              INTO same_tx FROM trainer.confirmations c
              WHERE c.confirmation_id=v_confirmation_id;
          END IF;
          IF same_tx IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'workout session requires same-transaction confirmation' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $function$;

        CREATE OR REPLACE FUNCTION trainer.guard_workout_revision_insert()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE same_tx boolean;
        BEGIN
          SELECT c.xmin::text::bigint=(pg_current_xact_id()::text::bigint % 4294967296)
            AND trainer.workout_confirmation_matches(c.confirmation_id,NEW.session_revision_id)
            INTO same_tx FROM trainer.confirmations c
            WHERE c.confirmation_id=NEW.confirmation_id;
          IF same_tx IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'workout revision requires same-transaction confirmation' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $function$;
    """)


def downgrade() -> None:
    raise RuntimeError("workout import batch history is forward-only")
