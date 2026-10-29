"""E-G03 additive migration conflict, atomicity, repeat and grants checks."""

from __future__ import annotations

import os

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head

E_G03 = next(item for item in MIGRATIONS if item.revision == "0016_e_g03_progress")


def _revision(cursor: psycopg.Cursor[tuple[object, ...]]) -> str:
    cursor.execute("SELECT version_num FROM trainer.alembic_version")
    row = cursor.fetchone()
    assert row is not None
    return str(row[0])


def main() -> None:
    command.upgrade(Config("alembic.ini"), "0015_e_g02_records")
    dsn = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        active_revision = _revision(cursor)
    if active_revision in {E_G03.revision, MIGRATIONS[-1].revision}:
        migrate_to_head()
        migrate_to_head()
        with psycopg.connect(dsn) as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT checksum FROM trainer.schema_migrations WHERE migration_id=%s",
                (E_G03.revision,),
            )
            assert cursor.fetchone() == (E_G03.checksum,)
        print("PASS: E-G03 migration probe repeat against an already upgraded database")
        return
    assert active_revision == "0015_e_g02_records", active_revision
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT pg_get_viewdef('trainer.v_best_results'::regclass, true)")
        baseline_best_view = cursor.fetchone()[0]

    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "SELECT description,seed_version FROM trainer.metric_kind_catalog "
            "WHERE code='estimated_1rm'"
        )
        original_metric_seed = cursor.fetchone()
        if original_metric_seed is None:
            cursor.execute(
                """
                INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at)
                VALUES ('estimated_1rm','synthetic conflict',1,now())
                """
            )
        else:
            cursor.execute(
                "UPDATE trainer.metric_kind_catalog SET description='synthetic conflict' "
                "WHERE code='estimated_1rm'"
            )
    try:
        migrate_to_head()
    except Exception as error:
        assert "E-G03 exact seed conflict" in str(error)
    else:
        raise AssertionError("E-G03 accepted a conflicting seed code")
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        assert _revision(cursor) == "0015_e_g02_records"
        cursor.execute("SELECT to_regclass('trainer.derived_progress_points')")
        assert cursor.fetchone() == (None,)
        if original_metric_seed is None:
            cursor.execute("DELETE FROM trainer.metric_kind_catalog WHERE code='estimated_1rm'")
        else:
            cursor.execute(
                "UPDATE trainer.metric_kind_catalog SET description=%s,seed_version=%s "
                "WHERE code='estimated_1rm'",
                original_metric_seed,
            )

    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            """
            CREATE FUNCTION trainer.synthetic_e_g03_migration_fault() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.seed_name='e_g03_progress' THEN
                RAISE EXCEPTION 'synthetic E-G03 migration fault';
              END IF;
              RETURN NEW;
            END
            $$
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_synthetic_e_g03_migration_fault
            BEFORE INSERT ON trainer.seed_versions
            FOR EACH ROW EXECUTE FUNCTION trainer.synthetic_e_g03_migration_fault()
            """
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "synthetic E-G03 migration fault" in str(error)
    else:
        raise AssertionError("injected E-G03 migration fault did not abort")
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        assert _revision(cursor) == "0015_e_g02_records"
        for object_name in (
            "trainer.derived_progress_points",
            "trainer.ix_progress_history",
            "trainer.ix_progress_source",
        ):
            cursor.execute("SELECT to_regclass(%s)", (object_name,))
            assert cursor.fetchone() == (None,), object_name
        cursor.execute("SELECT pg_get_viewdef('trainer.v_best_results'::regclass, true)")
        assert cursor.fetchone() == (baseline_best_view,)
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id=%s",
            (E_G03.revision,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("DROP TRIGGER trg_synthetic_e_g03_migration_fault ON trainer.seed_versions")
        cursor.execute("DROP FUNCTION trainer.synthetic_e_g03_migration_fault()")

    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        assert _revision(cursor) == MIGRATIONS[-1].revision
        cursor.execute(
            """
            SELECT checksum,app_schema_version,duration_ms >= 1
            FROM trainer.schema_migrations WHERE migration_id=%s
            """,
            (E_G03.revision,),
        )
        assert cursor.fetchone() == (E_G03.checksum, "16", True)
        cursor.execute(
            """
            SELECT has_table_privilege('trainer_app','trainer.derived_progress_points','SELECT'),
                   has_table_privilege('trainer_app','trainer.derived_progress_points','INSERT'),
                   has_table_privilege('trainer_app','trainer.derived_progress_points','UPDATE'),
                   has_table_privilege('trainer_app','trainer.derived_progress_points','DELETE'),
                   has_table_privilege('trainer_app','trainer.v_best_results','SELECT'),
                   has_schema_privilege('trainer_app','trainer','CREATE')
            """
        )
        assert cursor.fetchone() == (True, True, False, False, True, False)
        cursor.execute(
            """
            SELECT count(*) FROM pg_constraint WHERE conname IN (
              'fk_progress_exact_exercise_revision','fk_progress_exact_session_revision',
              'uq_progress_source_condition'
            )
            """
        )
        assert cursor.fetchone() == (3,)
        cursor.execute("SELECT pg_get_viewdef('trainer.v_best_results'::regclass,true)")
        view_definition = str(cursor.fetchone()[0]).lower()
        assert "calculated_best" in view_definition
        assert "derived_progress_points" in view_definition
        assert "d.state = 'current'" in view_definition

        cursor.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            ("f" * 64, E_G03.revision),
        )
        db.commit()
        try:
            migrate_to_head()
        except Exception as error:
            assert "migration ledger revision/checksum mismatch" in str(error)
        else:
            raise AssertionError("E-G03 checksum conflict was accepted")
        cursor.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id=%s",
            (E_G03.checksum, E_G03.revision),
        )

    migrate_to_head()
    print("PASS: E-G03 seed conflict, transactional rollback, clean/repeat, checksum and grants")


if __name__ == "__main__":
    main()
