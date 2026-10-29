"""Isolated E-G04 migration conflict, rollback, repeat and privilege gate."""

from __future__ import annotations

import os

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head

REVISION = "0017_e_g04_inventory"
BASELINE = "0016_e_g03_progress"
SPEC = next(item for item in MIGRATIONS if item.revision == REVISION)


def main() -> None:
    command.upgrade(Config("alembic.ini"), BASELINE)
    dsn = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        active = cursor.fetchone()
    if active == (REVISION,):
        migrate_to_head()
        migrate_to_head()
        print("PASS: E-G04 migration probe repeat against an already upgraded database")
        return
    assert active == (BASELINE,), active

    def assert_baseline() -> None:
        with psycopg.connect(dsn) as db, db.cursor() as cursor:
            cursor.execute("SELECT version_num FROM trainer.alembic_version")
            assert cursor.fetchone() == (BASELINE,)
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='trainer' AND table_name='equipment_item_revisions' "
                "AND column_name IN ('quantity','quantity_unit_code')"
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id=%s",
                (REVISION,),
            )
            assert cursor.fetchone() == (0,)

    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO trainer.equipment_type_catalog(code,description,seed_version,created_at) "
            "VALUES ('wrist_wraps','synthetic conflicting seed',1,now())"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "E-G04 exact seed conflict" in str(error)
    else:
        raise AssertionError("conflicting inventory seed was accepted")
    assert_baseline()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("DELETE FROM trainer.equipment_type_catalog WHERE code='wrist_wraps'")
        cursor.execute("""
            CREATE FUNCTION trainer.synthetic_e_g04_fault() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.seed_name='e_g04_inventory' THEN
                RAISE EXCEPTION 'synthetic E-G04 migration fault';
              END IF;
              RETURN NEW;
            END $$
        """)
        cursor.execute("""
            CREATE TRIGGER trg_synthetic_e_g04_fault BEFORE INSERT ON trainer.seed_versions
            FOR EACH ROW EXECUTE FUNCTION trainer.synthetic_e_g04_fault()
        """)
    try:
        migrate_to_head()
    except Exception as error:
        assert "synthetic E-G04 migration fault" in str(error)
    else:
        raise AssertionError("injected migration fault did not abort")
    assert_baseline()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("DROP TRIGGER trg_synthetic_e_g04_fault ON trainer.seed_versions")
        cursor.execute("DROP FUNCTION trainer.synthetic_e_g04_fault()")
        cursor.execute(
            "SELECT count(*) FROM trainer.equipment_type_catalog WHERE code='wrist_wraps'"
        )
        assert cursor.fetchone() == (0,)

    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == (MIGRATIONS[-1].revision,)
        cursor.execute(
            "SELECT checksum,app_schema_version,duration_ms >= 1 "
            "FROM trainer.schema_migrations WHERE migration_id=%s",
            (REVISION,),
        )
        assert cursor.fetchone() == (SPEC.checksum, "17", True)
        cursor.execute("""
            SELECT has_table_privilege('trainer_app','trainer.equipment_item_revisions','SELECT'),
                   has_table_privilege('trainer_app','trainer.equipment_item_revisions','INSERT'),
                   has_table_privilege('trainer_app','trainer.equipment_item_revisions','UPDATE'),
                   has_table_privilege('trainer_app','trainer.equipment_item_revisions','DELETE'),
                   has_schema_privilege('trainer_app','trainer','CREATE')
        """)
        assert cursor.fetchone() == (True, True, False, False, False)
        cursor.execute(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('ck_inventory_quantity','ck_inventory_quantity_unit')"
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            ("f" * 64, REVISION),
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "migration ledger revision/checksum mismatch" in str(error)
    else:
        raise AssertionError("conflicting ledger checksum was accepted")
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            (SPEC.checksum, REVISION),
        )
    migrate_to_head()
    print("PASS: E-G04 seed conflict, transactional rollback, clean/repeat, checksum and grants")


if __name__ == "__main__":
    main()
