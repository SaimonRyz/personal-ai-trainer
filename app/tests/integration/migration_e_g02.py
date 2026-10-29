"""E-G02 migration preflight, clean/repeat, rollback and permission checks."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head

E_G02_MIGRATION = next(item for item in MIGRATIONS if item.revision == "0015_e_g02_records")


def main() -> None:
    command.upgrade(Config("alembic.ini"), "0014_d_g06_workouts")
    admin_dsn = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
    session_id, profile_id, revision_id = uuid4(), uuid4(), uuid4()
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SET session_replication_role=replica")
        cursor.execute(
            """
            INSERT INTO trainer.workout_sessions(
              session_id,profile_id,current_revision_id,created_at
            ) VALUES (%s,%s,%s,now())
            """,
            (session_id, profile_id, revision_id),
        )
        cursor.execute("SET session_replication_role=origin")
    try:
        migrate_to_head()
    except Exception as error:
        assert "E-G02 preflight" in str(error)
    else:
        raise AssertionError("E-G02 accepted legacy confirmed workout data")
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0014_d_g06_workouts",)
        cursor.execute("""
            SELECT result,error_code FROM trainer.migration_attempts
            WHERE migration_id='0015_e_g02_records'
            ORDER BY started_at DESC LIMIT 1
        """)
        assert cursor.fetchone() == ("failed", "IntegrityError")
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0015_e_g02_records'"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("SET session_replication_role=replica")
        cursor.execute("DELETE FROM trainer.workout_sessions WHERE session_id=%s", (session_id,))
        cursor.execute("SET session_replication_role=origin")

    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at)
            VALUES ('record_full_reps','conflict',1,now())
            """
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "E-G02 seed code conflict" in str(error)
    else:
        raise AssertionError("E-G02 accepted conflicting record metric seed")
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0014_d_g06_workouts",)
        cursor.execute("DELETE FROM trainer.metric_kind_catalog WHERE code='record_full_reps'")

    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION trainer.synthetic_e_g02_migration_fault() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.seed_name='e_g02_records' THEN
                RAISE EXCEPTION 'synthetic E-G02 migration fault';
              END IF;
              RETURN NEW;
            END $$
        """)
        cursor.execute("""
            CREATE TRIGGER trg_synthetic_e_g02_migration_fault
            BEFORE INSERT ON trainer.seed_versions
            FOR EACH ROW EXECUTE FUNCTION trainer.synthetic_e_g02_migration_fault()
        """)
    try:
        migrate_to_head()
    except Exception as error:
        assert "synthetic E-G02 migration fault" in str(error)
    else:
        raise AssertionError("E-G02 transactional failure hook did not abort migration")
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0014_d_g06_workouts",)
        cursor.execute("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema='trainer' AND table_name='actual_records'
              AND column_name='exercise_revision_id'
        """)
        assert cursor.fetchone() == (0,)
        cursor.execute("DROP TRIGGER trg_synthetic_e_g02_migration_fault ON trainer.seed_versions")
        cursor.execute("DROP FUNCTION trainer.synthetic_e_g02_migration_fault()")

    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == (MIGRATIONS[-1].revision,)
        cursor.execute(
            """
            SELECT migration_id,checksum,app_schema_version,duration_ms >= 1
            FROM trainer.schema_migrations WHERE migration_id='0015_e_g02_records'
            """
        )
        assert cursor.fetchone() == (
            E_G02_MIGRATION.revision,
            E_G02_MIGRATION.checksum,
            "15",
            True,
        )
        cursor.execute(
            """
            SELECT code,description FROM trainer.metric_kind_catalog
            WHERE code LIKE 'record_%' ORDER BY code
            """
        )
        assert cursor.fetchall() == [
            ("record_cardio_time", "Record cardio time"),
            ("record_external_load", "Record external load"),
            ("record_full_reps", "Record full repetitions"),
            ("record_hold_duration", "Record hold duration"),
        ]
        cursor.execute(
            """
            SELECT has_table_privilege('trainer_app','trainer.record_candidates','SELECT'),
              has_table_privilege('trainer_app','trainer.record_candidates','INSERT'),
              has_table_privilege('trainer_app','trainer.comparison_conditions','UPDATE'),
              has_table_privilege('trainer_app','trainer.comparison_conditions','DELETE'),
              has_table_privilege('trainer_app','trainer.actual_records','DELETE')
            """
        )
        assert cursor.fetchone() == (True, True, False, False, False)
        cursor.execute(
            """
            SELECT has_schema_privilege('trainer_app','trainer','CREATE'),
              has_table_privilege('trainer_app','trainer.actual_records','UPDATE')
            """
        )
        assert cursor.fetchone() == (False, False)
        cursor.execute(
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema='trainer' AND table_name='actual_records'
              AND column_name='exercise_revision_id'
            """
        )
        assert cursor.fetchone() == ("NO",)
        cursor.execute(
            """
            SELECT count(*) FROM pg_constraint WHERE conname IN (
              'uq_candidate_source_condition','uq_actual_record_candidate',
              'fk_record_exact_exercise_revision','ck_record_source_shape'
            )
            """
        )
        assert cursor.fetchone() == (4,)
        cursor.execute(
            """
            SELECT count(*) FROM trainer.seed_versions WHERE seed_name='e_g02_records'
              AND seed_version=1
              AND checksum='fb6c73a6a0f75a91081848571afa3d5e7b3a8fa0f1a59d1792d6d7138f780add'
            """
        )
        assert cursor.fetchone() == (1,)
    print(
        "PASS: E-G02 preflight, seed conflict, transactional rollback, clean/repeat, "
        "constraints and grants"
    )


if __name__ == "__main__":
    main()
