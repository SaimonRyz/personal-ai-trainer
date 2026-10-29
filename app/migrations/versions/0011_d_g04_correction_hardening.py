"""Forward-only hardening for D-G04 seeds and confirmation chronology."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_d_g04_correction_hardening"
down_revision: str | None = "0010_d_g04_programs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
MIGRATION_CHECKSUM = "27589f73cec9b8eff803370b318537af4483fd263220f5dc8db12c0e97a0ef1b"


def upgrade() -> None:
    # 0010 deliberately did not overwrite conflicting catalog rows.  Before this
    # correction becomes active, fail closed instead of silently accepting a
    # different meaning for one of its codes.  Every catalog has archived_at;
    # active seed rows must have the exact meaning, version and archive state.
    op.execute(
        f"""
        DO $preflight$ BEGIN
          IF EXISTS (SELECT 1 FROM trainer.program_status_catalog WHERE code='active' AND (description <> 'Active program' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR NOT EXISTS (SELECT 1 FROM trainer.program_status_catalog WHERE code='active' AND description='Active program' AND seed_version=1 AND archived_at IS NULL)
             OR EXISTS (SELECT 1 FROM trainer.day_type_catalog WHERE code='training' AND (description <> 'Training day' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR NOT EXISTS (SELECT 1 FROM trainer.day_type_catalog WHERE code='training' AND description='Training day' AND seed_version=1 AND archived_at IS NULL)
             OR EXISTS (SELECT 1 FROM trainer.recommendation_kind_catalog WHERE code='program_design' AND (description <> 'Program design' OR seed_version <> 1 OR archived_at IS NOT NULL))
             OR NOT EXISTS (SELECT 1 FROM trainer.recommendation_kind_catalog WHERE code='program_design' AND description='Program design' AND seed_version=1 AND archived_at IS NULL)
             OR EXISTS (SELECT 1 FROM trainer.block_type_catalog WHERE code IN ('straight','superset','circuit') AND (seed_version <> 1 OR archived_at IS NOT NULL))
             OR (SELECT count(*) FROM trainer.block_type_catalog WHERE code IN ('straight','superset','circuit') AND (code,description) IN (('straight','Straight sets'),('superset','Superset'),('circuit','Circuit')) AND seed_version=1 AND archived_at IS NULL) <> 3
             OR EXISTS (SELECT 1 FROM trainer.set_type_catalog WHERE code IN ('warmup','working') AND (seed_version <> 1 OR archived_at IS NOT NULL))
             OR (SELECT count(*) FROM trainer.set_type_catalog WHERE code IN ('warmup','working') AND (code,description) IN (('warmup','Warm-up'),('working','Working')) AND seed_version=1 AND archived_at IS NULL) <> 2
             OR EXISTS (SELECT 1 FROM trainer.unit_catalog WHERE code IN ('second','meter') AND (seed_version <> 1 OR archived_at IS NOT NULL))
             OR (SELECT count(*) FROM trainer.unit_catalog WHERE code IN ('second','meter') AND (code,description) IN (('second','Second'),('meter','Meter')) AND seed_version=1 AND archived_at IS NULL) <> 2
             OR EXISTS (SELECT 1 FROM trainer.intent_type_catalog WHERE code IN ('program','program_publication') AND (seed_version <> 1 OR archived_at IS NOT NULL))
             OR (SELECT count(*) FROM trainer.intent_type_catalog WHERE code IN ('program','program_publication') AND (code,description) IN (('program','Program draft'),('program_publication','Program publication')) AND seed_version=1 AND archived_at IS NULL) <> 2
             OR EXISTS (SELECT 1 FROM trainer.dependency_type_catalog WHERE code IN ('program','program_version','exercise_revision','equipment_availability','conclusion_version','derived_value') AND (seed_version <> 1 OR archived_at IS NOT NULL))
             OR (SELECT count(*) FROM trainer.dependency_type_catalog WHERE code IN ('program','program_version','exercise_revision','equipment_availability','conclusion_version','derived_value') AND seed_version=1 AND archived_at IS NULL) <> 6
             OR EXISTS (SELECT 1 FROM trainer.domain_object_type_catalog WHERE code IN ('program','program_version','day_template','template_block','template_exercise','template_set','recommendation') AND (seed_version <> 1 OR archived_at IS NOT NULL))
             OR (SELECT count(*) FROM trainer.domain_object_type_catalog WHERE code IN ('program','program_version','day_template','template_block','template_exercise','template_set','recommendation') AND seed_version=1 AND archived_at IS NULL) <> 7
             OR EXISTS (SELECT 1 FROM trainer.domain_operation_catalog WHERE code IN ('publish','retire') AND (seed_version <> 1 OR archived_at IS NOT NULL))
             OR (SELECT count(*) FROM trainer.domain_operation_catalog WHERE code IN ('publish','retire') AND (code,description) IN (('publish','Publish'),('retire','Retire')) AND seed_version=1 AND archived_at IS NULL) <> 2
          THEN RAISE EXCEPTION 'D-G04 seed correction requires exact active 0010 seed rows'; END IF;
        END $preflight$;

        ALTER TABLE trainer.previews ADD COLUMN shown_sequence bigint NULL CHECK (shown_sequence >= 1);
        ALTER TABLE trainer.preview_show_events ADD COLUMN shown_sequence bigint NULL CHECK (shown_sequence >= 1);
        ALTER TABLE trainer.confirmations ADD COLUMN confirmation_sequence bigint NULL CHECK (confirmation_sequence >= 1);
        GRANT UPDATE (shown_sequence) ON trainer.previews TO trainer_app;
        CREATE UNIQUE INDEX ux_preview_shown_sequence_per_conversation
          ON trainer.preview_show_events(conversation_id,shown_sequence) WHERE shown_sequence IS NOT NULL;
        CREATE UNIQUE INDEX ux_confirmation_sequence_per_conversation
          ON trainer.confirmations(conversation_id,confirmation_sequence) WHERE confirmation_sequence IS NOT NULL;
        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0011_d_g04_correction_hardening','{MIGRATION_CHECKSUM}',now(),0,'11',now());
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G04 correction migration is forward-only")
