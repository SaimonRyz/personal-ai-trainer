"""Forward-only exact D-G04 seed semantic preflight."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_d_g04_seed_exactness"
down_revision: str | None = "0011_d_g04_correction_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
MIGRATION_CHECKSUM = "0401d1b8541d605518882858a50c6f877fa85d04782f74f56e18f8882a4bf083"


def upgrade() -> None:
    # Migration 0011 has already been applied on permitted databases and must
    # remain byte-for-byte stable.  Check the remaining semantic seed bundle in
    # a new transaction before advancing the ledger.
    op.execute(
        f"""
        DO $preflight$ BEGIN
          IF (SELECT count(*) FROM trainer.dependency_type_catalog
                WHERE code IN ('program','program_version','exercise_revision','equipment_availability','conclusion_version','derived_value')
                  AND (code,description) IN (('program','Program'),('program_version','Program version'),('exercise_revision','Exercise revision'),('equipment_availability','Equipment availability'),('conclusion_version','Conclusion version'),('derived_value','Derived value'))
                  AND seed_version=1 AND archived_at IS NULL) <> 6
             OR (SELECT count(*) FROM trainer.domain_object_type_catalog
                WHERE code IN ('program','program_version','day_template','template_block','template_exercise','template_set','recommendation')
                  AND (code,description) IN (('program','Program'),('program_version','Program version'),('day_template','Day template'),('template_block','Template block'),('template_exercise','Template exercise'),('template_set','Template set'),('recommendation','Recommendation'))
                  AND seed_version=1 AND archived_at IS NULL) <> 7
             OR (SELECT count(*) FROM trainer.unit_family_mapping) <> 3
             OR (SELECT count(*) FROM trainer.unit_family_mapping
                WHERE (unit_code,family_code) IN (('repetition','count'),('second','time'),('meter','length'))) <> 3
          THEN RAISE EXCEPTION 'D-G04 exact seed preflight requires original descriptions and unit family mappings'; END IF;

          INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
          VALUES ('0012_d_g04_seed_exactness','{MIGRATION_CHECKSUM}',now(),0,'12',now());
        END $preflight$;
        """
    )


def downgrade() -> None:
    raise RuntimeError("D-G04 seed exactness migration is forward-only")
