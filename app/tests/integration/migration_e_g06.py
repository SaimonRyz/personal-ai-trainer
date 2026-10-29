"""Isolated E-G06 migration seed, rollback, repeat and privilege probe."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head

REVISION = "0018_e_g06_measurements"
BASELINE = "0017_e_g04_inventory"
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
        print("PASS: E-G06 migration repeat against an already upgraded database")
        return
    assert active == (BASELINE,), active

    def baseline() -> None:
        with psycopg.connect(dsn) as db, db.cursor() as cursor:
            cursor.execute("SELECT version_num FROM trainer.alembic_version")
            assert cursor.fetchone() == (BASELINE,)
            cursor.execute("SELECT to_regclass('trainer.body_measurement_heads')")
            assert cursor.fetchone() == (None,)
            cursor.execute(
                "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id=%s", (REVISION,)
            )
            assert cursor.fetchone() == (0,)

    baseline()
    # Deliberately malformed legacy fixtures, only in this disposable volume.
    # Replica mode follows the existing migration probes; no real facts are read.
    profile_a, profile_b, confirmation = uuid4(), uuid4(), uuid4()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO trainer.profiles(profile_id,created_at,archived_at) "
            "VALUES (%s,now(),now()),(%s,now(),now())",
            (profile_a, profile_b),
        )

    def insert_legacy(rows: list[tuple[object, object, object]]) -> None:
        with psycopg.connect(dsn) as db, db.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role=replica")
            for row_id, owner, predecessor in rows:
                cursor.execute(
                    "INSERT INTO trainer.body_measurement_sessions("
                    "measurement_session_id,profile_id,measured_on,supersedes_session_id,"
                    "confirmation_id,comment,created_at) VALUES (%s,%s,'2026-01-01',%s,%s,"
                    "'synthetic legacy',now())",
                    (row_id, owner, predecessor, confirmation),
                )

    def remove_legacy(ids: list[object]) -> None:
        with psycopg.connect(dsn) as db, db.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role=replica")
            cursor.execute(
                "DELETE FROM trainer.body_measurement_values WHERE measurement_session_id=ANY(%s)",
                (ids,),
            )
            cursor.execute(
                "DELETE FROM trainer.body_measurement_sessions WHERE "
                "measurement_session_id=ANY(%s)",
                (ids,),
            )

    for case in ("foreign_profile", "branch", "cycle", "missing_predecessor"):
        root, child, extra = uuid4(), uuid4(), uuid4()
        rows = [(root, profile_a, None), (child, profile_a, root)]
        if case == "foreign_profile":
            rows[1] = (child, profile_b, root)
        elif case == "branch":
            rows.append((extra, profile_a, root))
        elif case == "cycle":
            rows = [(root, profile_a, child), (child, profile_a, root)]
        else:
            rows[1] = (child, profile_a, extra)
        insert_legacy(rows)
        try:
            migrate_to_head()
        except Exception as error:
            assert "E-G06 ambiguous legacy chain" in str(error) or "E-G06 legacy cycle" in str(
                error
            )
        else:
            raise AssertionError(f"invalid legacy chain accepted: {case}")
        baseline()
        with psycopg.connect(dsn) as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM trainer.body_measurement_sessions "
                "WHERE measurement_session_id=ANY(%s)",
                ([row[0] for row in rows],),
            )
            assert cursor.fetchone() == (len(rows),)
        remove_legacy([row[0] for row in rows])

    legacy_root, legacy_leaf, legacy_value = uuid4(), uuid4(), uuid4()
    insert_legacy([(legacy_root, profile_a, None), (legacy_leaf, profile_a, legacy_root)])
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role=replica")
        cursor.execute(
            "INSERT INTO trainer.body_measurement_values(measurement_value_id,"
            "measurement_session_id,measurement_kind,original_value,original_unit,"
            "normalized_value,normalized_unit,comment,created_at) "
            "VALUES (%s,%s,'body_mass',80,'kilogram',NULL,NULL,'synthetic raw legacy',now())",
            (legacy_value, legacy_leaf),
        )
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO trainer.unit_catalog(code,description,seed_version,created_at) "
            "VALUES ('inch','synthetic conflicting inch',1,now())"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "E-G06 exact seed conflict" in str(error)
    else:
        raise AssertionError("E-G06 conflicting seed accepted")
    baseline()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("DELETE FROM trainer.unit_catalog WHERE code='inch'")
        cursor.execute("""
            CREATE FUNCTION trainer.synthetic_e_g06_fault() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.seed_name='e_g06_measurements' THEN
                RAISE EXCEPTION 'synthetic E-G06 migration fault';
              END IF;
              RETURN NEW;
            END $$
        """)
        cursor.execute("""
            CREATE TRIGGER trg_synthetic_e_g06_fault BEFORE INSERT ON trainer.seed_versions
            FOR EACH ROW EXECUTE FUNCTION trainer.synthetic_e_g06_fault()
        """)
    try:
        migrate_to_head()
    except Exception as error:
        assert "synthetic E-G06 migration fault" in str(error)
    else:
        raise AssertionError("E-G06 injected migration fault accepted")
    baseline()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("DROP TRIGGER trg_synthetic_e_g06_fault ON trainer.seed_versions")
        cursor.execute("DROP FUNCTION trainer.synthetic_e_g06_fault()")
    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == (MIGRATIONS[-1].revision,)
        cursor.execute(
            "SELECT checksum,app_schema_version,duration_ms>=1 "
            "FROM trainer.schema_migrations WHERE migration_id=%s",
            (REVISION,),
        )
        assert cursor.fetchone() == (SPEC.checksum, "18", True)
        cursor.execute("""
            SELECT has_table_privilege('trainer_app','trainer.body_measurement_heads','SELECT'),
                   has_table_privilege('trainer_app','trainer.body_measurement_heads','INSERT'),
                   has_column_privilege('trainer_app','trainer.body_measurement_heads','current_session_id','UPDATE'),
                   has_table_privilege('trainer_app','trainer.body_measurement_sessions','UPDATE'),
                   has_table_privilege('trainer_app','trainer.body_measurement_values','UPDATE')
        """)
        assert cursor.fetchone() == (True, True, True, False, False)
        cursor.execute("""
            SELECT has_table_privilege('trainer_app','trainer.unit_catalog','UPDATE'),
                   has_table_privilege('trainer_app','trainer.unit_catalog','DELETE'),
                   has_table_privilege('trainer_app','trainer.measurement_kind_catalog','UPDATE'),
                   has_table_privilege('trainer_app','trainer.measurement_kind_catalog','DELETE'),
                   has_function_privilege('trainer_app','trainer.lock_measurement_catalog()','EXECUTE')
        """)
        assert cursor.fetchone() == (False, False, False, False, True)
        cursor.execute("""
            SELECT pg_get_userbyid(proowner),prosecdef,provolatile,proconfig
            FROM pg_proc WHERE oid='trainer.lock_measurement_catalog()'::regprocedure
        """)
        assert cursor.fetchone() == (
            "trainer_migrator",
            True,
            "v",
            ["search_path=pg_catalog, trainer, pg_temp"],
        )
        cursor.execute("SELECT count(*) FROM trainer.measurement_kind_catalog")
        assert cursor.fetchone() == (9,)
        cursor.execute(
            "SELECT measurement_root_id FROM trainer.body_measurement_sessions "
            "WHERE measurement_session_id IN (%s,%s)",
            (legacy_root, legacy_leaf),
        )
        assert cursor.fetchall() == [(legacy_root,), (legacy_root,)]
        cursor.execute(
            "SELECT current_session_id FROM trainer.body_measurement_heads "
            "WHERE measurement_root_id=%s",
            (legacy_root,),
        )
        assert cursor.fetchone() == (legacy_leaf,)
        cursor.execute(
            "SELECT original_value,original_unit,normalized_value,normalized_unit,"
            "conversion_version,method_id,comment FROM trainer.body_measurement_values "
            "WHERE measurement_value_id=%s",
            (legacy_value,),
        )
        assert cursor.fetchone() == (80, "kilogram", None, None, None, None, "synthetic raw legacy")
        cursor.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            ("f" * 64, REVISION),
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "migration ledger revision/checksum mismatch" in str(error)
    else:
        raise AssertionError("E-G06 ledger conflict accepted")
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            (SPEC.checksum, REVISION),
        )
    migrate_to_head()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role=replica")
        cursor.execute(
            "DELETE FROM trainer.body_measurement_heads WHERE measurement_root_id=%s",
            (legacy_root,),
        )
    remove_legacy([legacy_root, legacy_leaf])
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM trainer.profiles WHERE profile_id IN (%s,%s)", (profile_a, profile_b)
        )
    print("PASS: E-G06 legacy backfill/rejections, exact seed, rollback, repeat, checksum, grants")


if __name__ == "__main__":
    main()
