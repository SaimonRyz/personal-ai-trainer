# ruff: noqa: E501
"""D-G06 migration preflight, seed conflict, clean/repeat and role checks."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head


def migrate_from_0013() -> None:
    command.upgrade(Config("alembic.ini"), "0013_d_g05_plans")


def main() -> None:
    migrate_from_0013()
    admin_dsn = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
    session_id, profile_id, revision_id = uuid4(), uuid4(), uuid4()
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SET session_replication_role=replica")
        cursor.execute(
            "INSERT INTO trainer.workout_sessions(session_id,profile_id,current_revision_id,created_at) VALUES (%s,%s,%s,now())",
            (session_id, profile_id, revision_id),
        )
        cursor.execute("SET session_replication_role=origin")
    try:
        migrate_to_head()
    except Exception as error:
        assert "D-G06 preflight" in str(error)
    else:
        raise AssertionError("D-G06 accepted a legacy workout without exact ExerciseRevision")
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0013_d_g05_plans",)
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0014_d_g06_workouts'"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("SET session_replication_role=replica")
        cursor.execute("DELETE FROM trainer.workout_sessions WHERE session_id=%s", (session_id,))
        cursor.execute("SET session_replication_role=origin")

    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO trainer.session_type_catalog(code,description,seed_version,created_at) VALUES ('training','Conflicted session seed',1,now())"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "D-G06 preflight" in str(error)
    else:
        raise AssertionError("D-G06 accepted a conflicting session_type seed")
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0013_d_g05_plans",)
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0014_d_g06_workouts'"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("DELETE FROM trainer.session_type_catalog WHERE code='training'")

    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO trainer.load_kind_catalog(code,description,seed_version,created_at) VALUES ('external','Conflicted load kind',1,now())"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "D-G06 preflight" in str(error)
    else:
        raise AssertionError("D-G06 accepted a conflicting load-kind seed")
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0013_d_g05_plans",)
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0014_d_g06_workouts'"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("DELETE FROM trainer.load_kind_catalog WHERE code='external'")

    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(admin_dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == (MIGRATIONS[-1].revision,)
        cursor.execute(
            "SELECT code,description,seed_version,archived_at FROM trainer.session_type_catalog WHERE code='training'"
        )
        assert cursor.fetchone() == ("training", "Training session", 1, None)
        cursor.execute(
            "SELECT count(*) FROM trainer.actual_set_status_catalog WHERE archived_at IS NULL"
        )
        assert cursor.fetchone() == (3,)
        cursor.execute("SELECT count(*) FROM trainer.workout_metric_compatibility")
        assert cursor.fetchone() == (6,)
        cursor.execute(
            "SELECT code,description FROM trainer.load_kind_catalog WHERE archived_at IS NULL ORDER BY code"
        )
        assert cursor.fetchall() == [
            ("band", "Resistance band"),
            ("bodyweight", "Bodyweight"),
            ("external", "External load"),
        ]
        cursor.execute(
            "SELECT code,description FROM trainer.load_direction_catalog WHERE archived_at IS NULL ORDER BY code"
        )
        assert cursor.fetchall() == [("assistance", "Assistance"), ("resistance", "Resistance")]
        cursor.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname IN ('ck_planned_load_kind_direction','ck_actual_load_kind_direction') ORDER BY conname"
        )
        constraints = cursor.fetchall()
        assert len(constraints) == 2
        assert all(
            "direction" in definition and "band" in definition for _, definition in constraints
        )
        cursor.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_workout_metric_source_derived'"
        )
        assert "derived_value_id" in cursor.fetchone()[1]
        cursor.execute(
            "SELECT migration_id,checksum,app_schema_version,duration_ms >= 1 "
            "FROM trainer.schema_migrations WHERE migration_id='0014_d_g06_workouts'"
        )
        migration_0014 = next(item for item in MIGRATIONS if item.revision == "0014_d_g06_workouts")
        assert cursor.fetchall() == [(migration_0014.revision, migration_0014.checksum, "14", True)]
        cursor.execute(
            "SELECT count(*) FROM trainer.seed_versions WHERE seed_name='d_g06_workouts' AND seed_version=1 AND checksum='2bf7c548c1b8b9feac4e98ec36c9b185445ae9cafec8d607022eebd296249cc4'"
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT has_table_privilege('trainer_app','trainer.workout_metric_compatibility','SELECT'),has_table_privilege('trainer_app','trainer.workout_metric_compatibility','INSERT'),has_table_privilege('trainer_app','trainer.actual_sets','DELETE'),has_table_privilege('trainer_app','trainer.actual_sets','UPDATE')"
        )
        assert cursor.fetchone() == (True, False, False, False)
        cursor.execute(
            "SELECT has_table_privilege('trainer_app','trainer.load_kind_catalog','SELECT'),has_table_privilege('trainer_app','trainer.load_kind_catalog','INSERT'),has_table_privilege('trainer_app','trainer.load_direction_catalog','UPDATE')"
        )
        assert cursor.fetchone() == (True, False, False)
        cursor.execute(
            "SELECT has_table_privilege('trainer_app','trainer.session_type_catalog','INSERT'),has_table_privilege('trainer_app','trainer.actual_set_status_catalog','UPDATE')"
        )
        assert cursor.fetchone() == (False, False)
        cursor.execute(
            "SELECT has_table_privilege('trainer_app','trainer.actual_blocks','INSERT'),has_table_privilege('trainer_app','trainer.actual_blocks','UPDATE'),has_table_privilege('trainer_app','trainer.actual_sets','DELETE'),has_table_privilege('trainer_app','trainer.workout_session_revisions','UPDATE'),has_column_privilege('trainer_app','trainer.workout_sessions','current_revision_id','UPDATE'),has_column_privilege('trainer_app','trainer.workout_sessions','created_at','UPDATE')"
        )
        assert cursor.fetchone() == (True, False, False, False, True, False)
        cursor.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_actual_set_side'"
        )
        side_constraint = cursor.fetchone()
        assert side_constraint is not None and all(
            value in side_constraint[1] for value in ("left", "right", "both")
        )
        cursor.execute(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema='trainer' AND table_name='exercise_executions' AND column_name='exercise_revision_id'"
        )
        assert cursor.fetchone() == ("NO",)
        cursor.execute(
            "SELECT is_nullable,column_default FROM information_schema.columns WHERE table_schema='trainer' AND table_name='workout_session_revisions' AND column_name='report_gaps'"
        )
        report_gaps = cursor.fetchone()
        assert report_gaps is not None and report_gaps[0] == "NO" and "[]" in report_gaps[1]
        cursor.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_workout_report_gaps_array'"
        )
        report_gaps_constraint = cursor.fetchone()
        assert report_gaps_constraint is not None and "array" in report_gaps_constraint[1]
    print(
        "PASS: D-G06 migration preflight, seed conflict, clean/repeat, exact seed and runtime grants"
    )


if __name__ == "__main__":
    main()
