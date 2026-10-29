"""Allow the confirmed CMD-14 availability-only transition for one source."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_source_availability"
down_revision: str | None = "0020_h03_exercise_category_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "70c6b69f62fcf38e4c8a0e13ad8a2c3a84f4c36e1446d9d1a4c9f71ab9b0307a"


def upgrade() -> None:
    op.execute(f"""
        REVOKE UPDATE ON trainer.scientific_sources FROM trainer_app;

        CREATE FUNCTION trainer.apply_source_availability_update(
            p_source_id uuid,
            p_expected_availability text,
            p_expected_last_checked_at timestamptz,
            p_availability text,
            p_confirmation_id uuid
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,trainer
        AS $function$
        DECLARE
            v_payload jsonb;
            v_update jsonb;
            v_expected_checked_text text;
            v_checked_at timestamptz;
            v_confirmation_at timestamptz;
            v_current_availability text;
            v_current_checked_at timestamptz;
        BEGIN
            SELECT c.confirmed_at,dr.typed_payload
              INTO v_confirmation_at,v_payload
              FROM trainer.confirmations c
              JOIN trainer.previews p ON p.preview_id=c.preview_id
                                    AND p.draft_revision_id=c.draft_revision_id
              JOIN trainer.draft_revisions dr ON dr.draft_revision_id=c.draft_revision_id
              JOIN trainer.drafts d ON d.draft_id=dr.draft_id
              JOIN trainer.commit_results cr ON cr.confirmation_id=c.confirmation_id
              JOIN trainer.idempotent_operations io ON io.operation_id=cr.operation_id
             WHERE c.confirmation_id=p_confirmation_id
               AND c.kind='normal'
               AND io.command_kind='CMD-00'
               AND io.state='received'
               AND p.is_complete
               AND p.shown_message_id IS NOT NULL
               AND p.returned_as_shown_at IS NOT NULL
               AND dr.trust_state='shown'
               AND d.state='open'
             FOR UPDATE OF c,p,dr,d;

            IF NOT FOUND OR v_payload->>'evidence_change' IS DISTINCT FROM 'source_availability' THEN
                RAISE EXCEPTION 'confirmed availability draft required' USING ERRCODE='42501';
            END IF;

            v_update := v_payload->'source_availability_update';
            IF jsonb_typeof(v_update) IS DISTINCT FROM 'object'
               OR v_update->>'source_id' IS DISTINCT FROM p_source_id::text
               OR v_update->>'expected_availability' IS DISTINCT FROM p_expected_availability
               OR v_update->>'new_availability' IS DISTINCT FROM p_availability
               OR NOT (v_update ? 'expected_last_checked_at')
               OR NOT (v_update ? 'checked_at') THEN
                RAISE EXCEPTION 'availability payload does not match confirmation' USING ERRCODE='42501';
            END IF;

            v_checked_at := (v_update->>'checked_at')::timestamptz;
            IF v_checked_at IS NULL OR v_checked_at > v_confirmation_at THEN
                RAISE EXCEPTION 'invalid source check timestamp' USING ERRCODE='42501';
            END IF;

            v_expected_checked_text := v_update->>'expected_last_checked_at';
            IF v_expected_checked_text IS NULL THEN
                IF p_expected_last_checked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'availability check timestamp does not match confirmation' USING ERRCODE='42501';
                END IF;
            ELSIF p_expected_last_checked_at IS NULL
               OR v_expected_checked_text::timestamptz IS DISTINCT FROM p_expected_last_checked_at THEN
                RAISE EXCEPTION 'availability check timestamp does not match confirmation' USING ERRCODE='42501';
            END IF;

            IF p_availability NOT IN ('available','unavailable')
               OR NOT EXISTS (
                   SELECT 1 FROM trainer.scientific_source_availability_catalog c
                    WHERE c.code=p_availability AND c.archived_at IS NULL
               ) THEN
                RAISE EXCEPTION 'unknown source availability' USING ERRCODE='23514';
            END IF;

            SELECT s.availability,s.last_checked_at
              INTO v_current_availability,v_current_checked_at
              FROM trainer.scientific_sources s
             WHERE s.source_id=p_source_id AND s.archived_at IS NULL
             FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'scientific source not found' USING ERRCODE='P0002';
            END IF;
            IF v_current_availability IS DISTINCT FROM p_expected_availability
               OR v_current_checked_at IS DISTINCT FROM p_expected_last_checked_at THEN
                RAISE EXCEPTION 'source availability changed after preview' USING ERRCODE='40001';
            END IF;

            UPDATE trainer.scientific_sources
               SET availability=p_availability,last_checked_at=v_checked_at
             WHERE source_id=p_source_id;

            RETURN jsonb_build_object(
                'source_id',p_source_id,
                'before',jsonb_build_object(
                    'availability',v_current_availability,
                    'last_checked_at',v_current_checked_at
                ),
                'after',jsonb_build_object(
                    'availability',p_availability,
                    'last_checked_at',v_checked_at
                )
            );
        END
        $function$;

        REVOKE ALL ON FUNCTION trainer.apply_source_availability_update(
            uuid,text,timestamptz,text,uuid
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION trainer.apply_source_availability_update(
            uuid,text,timestamptz,text,uuid
        ) TO trainer_app;

        INSERT INTO trainer.schema_migrations
            (migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0021_source_availability','{MIGRATION_CHECKSUM}',now(),0,'21',now());
    """)


def downgrade() -> None:
    raise RuntimeError("EV-008 migration is forward-only")
