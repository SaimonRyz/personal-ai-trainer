"""E-G07 migration transactional seed, repeat, checksum and privilege probe."""

from __future__ import annotations

import os

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head

REVISION = "0019_e_g07_evidence"
BASELINE = "0018_e_g06_measurements"
SPEC = next(s for s in MIGRATIONS if s.revision == REVISION)


def main() -> None:
    command.upgrade(Config("alembic.ini"), BASELINE)
    dsn = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        active = c.fetchone()
    if active in {(REVISION,), (MIGRATIONS[-1].revision,)}:
        migrate_to_head()
        migrate_to_head()
        with psycopg.connect(dsn) as db, db.cursor() as c:
            c.execute("SELECT version_num FROM trainer.alembic_version")
            assert c.fetchone() == (MIGRATIONS[-1].revision,)
            c.execute(
                "SELECT checksum FROM trainer.schema_migrations WHERE migration_id=%s",
                (REVISION,),
            )
            assert c.fetchone() == (SPEC.checksum,)
        print("PASS: E-G07 migration repeat on already upgraded isolated database")
        return
    assert active == (BASELINE,), active
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute(
            "INSERT INTO trainer.scientific_source_type_catalog"
            "(code,description,seed_version,created_at) VALUES"
            "('controlled_trial','synthetic conflicting seed',1,now())"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "E-G07 exact seed conflict" in str(error)
    else:
        raise AssertionError("conflicting seed accepted")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == (BASELINE,)
        c.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id=%s", (REVISION,)
        )
        assert c.fetchone() == (0,)
        c.execute(
            "DELETE FROM trainer.scientific_source_type_catalog WHERE code='controlled_trial'"
        )
        c.execute("""
            CREATE FUNCTION trainer.synthetic_e_g07_fault() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.migration_id='0019_e_g07_evidence' THEN
                RAISE EXCEPTION 'synthetic E-G07 migration fault';
              END IF; RETURN NEW;
            END $$;
            CREATE TRIGGER trg_synthetic_e_g07_fault BEFORE INSERT ON trainer.schema_migrations
              FOR EACH ROW EXECUTE FUNCTION trainer.synthetic_e_g07_fault();
        """)
    try:
        migrate_to_head()
    except Exception as error:
        assert "synthetic E-G07 migration fault" in str(error)
    else:
        raise AssertionError("migration fault ignored")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == (BASELINE,)
        c.execute("SELECT to_regprocedure('trainer.guard_knowledge_protocol()')")
        assert c.fetchone() == (None,)
        c.execute(
            "SELECT count(*) FROM trainer.scientific_source_type_catalog "
            "WHERE code='controlled_trial'"
        )
        assert c.fetchone() == (0,)
        c.execute("DROP TRIGGER trg_synthetic_e_g07_fault ON trainer.schema_migrations")
        c.execute("DROP FUNCTION trainer.synthetic_e_g07_fault()")
    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == (MIGRATIONS[-1].revision,)
        c.execute(
            "SELECT checksum,app_schema_version,duration_ms>=1 "
            "FROM trainer.schema_migrations WHERE migration_id=%s",
            (REVISION,),
        )
        assert c.fetchone() == (SPEC.checksum, "19", True)
        for table in (
            "scientific_sources",
            "conclusion_versions",
            "verification_protocol_runs",
            "knowledge_status_revisions",
        ):
            c.execute(
                "SELECT has_table_privilege('trainer_app',%s,'SELECT'),"
                "has_table_privilege('trainer_app',%s,'INSERT'),"
                "has_table_privilege('trainer_app',%s,'UPDATE'),"
                "has_table_privilege('trainer_app',%s,'DELETE')",
                ("trainer." + table,) * 4,
            )
            assert c.fetchone() == (True, True, False, False), table
        c.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            ("f" * 64, REVISION),
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "migration ledger revision/checksum mismatch" in str(error)
    else:
        raise AssertionError("checksum conflict accepted")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            (SPEC.checksum, REVISION),
        )
    migrate_to_head()
    print("PASS: E-G07 exact seed, transactional rollback, repeat, checksum, append-only grants")


if __name__ == "__main__":
    main()
