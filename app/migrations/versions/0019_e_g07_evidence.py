"""Seed the evidence vocabulary and bind protocol decisions to exact versions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_e_g07_evidence"
down_revision: str | None = "0018_e_g06_measurements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

MIGRATION_CHECKSUM = "24d47aa4ea9fbbec2a3714de75a8a9bb19e927de74205425a19dad41487313ac"
SEEDS = {
    "scientific_source_type_catalog": (
        ("systematic_review", "Systematic review"),
        ("meta_analysis", "Meta-analysis"),
        ("position_statement", "Official position statement"),
        ("controlled_trial", "Controlled trial"),
        ("observational", "Observational study"),
        ("review", "Professional review"),
    ),
    "scientific_source_availability_catalog": (
        ("available", "Available"),
        ("unavailable", "Unavailable"),
    ),
    "intent_type_catalog": (
        ("evidence", "Scientific evidence draft"),
        ("conclusion_verification", "Owner conclusion verification"),
    ),
    "domain_object_type_catalog": (
        ("conclusion", "Scientific conclusion"),
        ("knowledge_status_revision", "Knowledge status revision"),
        ("scientific_source", "Scientific source"),
        ("verification_protocol_run", "Owner verification protocol run"),
    ),
}


def upgrade() -> None:
    for table, values in SEEDS.items():
        rows = ",".join(f"('{code}','{description}')" for code, description in values)
        op.execute(f"""
            DO $seed$ BEGIN
              IF EXISTS(SELECT 1 FROM trainer.{table} c
                JOIN (VALUES {rows}) s(code,description) ON s.code=c.code
                WHERE c.description<>s.description OR c.seed_version<>1 OR c.archived_at IS NOT NULL)
              THEN RAISE EXCEPTION 'E-G07 exact seed conflict'; END IF;
            END $seed$;
        """)
    op.execute("""
        DO $preflight$ BEGIN
          IF EXISTS(SELECT 1 FROM trainer.knowledge_status_revisions s
            JOIN trainer.verification_protocol_runs p USING(protocol_run_id)
            WHERE s.conclusion_version_id<>p.conclusion_version_id
              OR (s.status IN ('verified','rejected') AND s.status<>p.decision))
          THEN RAISE EXCEPTION 'E-G07 inconsistent protocol ownership'; END IF;
        END $preflight$;
        CREATE FUNCTION trainer.guard_knowledge_protocol() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.protocol_run_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM trainer.verification_protocol_runs p
            WHERE p.protocol_run_id=NEW.protocol_run_id
              AND p.conclusion_version_id=NEW.conclusion_version_id
              AND (NEW.status NOT IN ('verified','rejected') OR p.decision=NEW.status)
          ) THEN RAISE EXCEPTION 'invalid knowledge protocol ownership' USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_knowledge_protocol BEFORE INSERT ON trainer.knowledge_status_revisions
          FOR EACH ROW EXECUTE FUNCTION trainer.guard_knowledge_protocol();
    """)
    for table, values in SEEDS.items():
        rows = ",".join(f"('{code}','{description}',1,now())" for code, description in values)
        op.execute(
            f"INSERT INTO trainer.{table}(code,description,seed_version,created_at) VALUES {rows} ON CONFLICT DO NOTHING"
        )
    op.execute(f"""
        INSERT INTO trainer.schema_migrations(migration_id,checksum,applied_at,duration_ms,app_schema_version,created_at)
        VALUES ('0019_e_g07_evidence','{MIGRATION_CHECKSUM}',now(),0,'19',now());
    """)


def downgrade() -> None:
    raise RuntimeError("E-G07 migration is forward-only")
