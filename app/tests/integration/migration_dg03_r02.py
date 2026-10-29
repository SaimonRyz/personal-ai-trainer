"""Synthetic fail-closed and recovery scenario for D-G03 migration 0009."""

from __future__ import annotations

import os
from uuid import UUID

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head

ADMIN_DSN = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
REVISION_ID = UUID("00000000-0000-0000-0000-000000000901")
ALIAS_ONE_ID = UUID("00000000-0000-0000-0000-000000000902")
ALIAS_TWO_ID = UUID("00000000-0000-0000-0000-000000000903")
GROUP_ID = UUID("00000000-0000-0000-0000-000000000904")
MEASUREMENT_ID = UUID("00000000-0000-0000-0000-000000000905")


def upgrade_to_0008() -> None:
    command.upgrade(Config("alembic.ini"), "0008_d_g03_immutable_permissions")


def insert_conflicting_legacy_rows() -> None:
    with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SET session_replication_role=replica")
        cursor.execute(
            """
            INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at)
            VALUES ('duration','Duration',1,now())
            """
        )
        cursor.execute(
            """
            INSERT INTO trainer.unit_family_catalog(code,description,seed_version,created_at)
            VALUES ('count','Count',1,now())
            """
        )
        cursor.execute(
            """
            INSERT INTO trainer.exercise_aliases(
                exercise_alias_id,exercise_revision_id,alias,normalized_alias,
                locale,context,created_at
            ) VALUES
                (%s,%s,'Legacy one','legacy duplicate','ru','one',now()),
                (%s,%s,'Legacy two','legacy duplicate','en','two',now())
            """,
            (ALIAS_ONE_ID, REVISION_ID, ALIAS_TWO_ID, REVISION_ID),
        )
        cursor.execute(
            """
            INSERT INTO trainer.muscle_groups(
                muscle_group_id,name,normalized_name,status_code,
                normalization_version,confirmation_id,created_at
            ) VALUES (%s,'Legacy blank','   ','active','exercise-name-v1',%s,now())
            """,
            (GROUP_ID, UUID("00000000-0000-0000-0000-000000000906")),
        )
        cursor.execute(
            """
            INSERT INTO trainer.exercise_measurements(
                exercise_measurement_id,exercise_revision_id,metric_kind,
                requiredness,unit_family,created_at
            ) VALUES (%s,%s,'duration','required','count',now())
            """,
            (MEASUREMENT_ID, REVISION_ID),
        )
        cursor.execute("SET session_replication_role=origin")


def assert_failed_without_partial_effect() -> None:
    try:
        migrate_to_head()
    except Exception as error:
        if "D-G03" not in str(error):
            raise AssertionError(f"unexpected migration failure: {error}") from error
    else:
        raise AssertionError("migration 0009 accepted conflicting legacy rows")

    with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0008_d_g03_immutable_permissions",)
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id=%s",
            ("0009_d_g03_validation_hardening",),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("SELECT to_regclass('trainer.exercise_measurement_compatibility')")
        assert cursor.fetchone() == (None,)
        cursor.execute(
            "SELECT count(*) FROM trainer.exercise_aliases WHERE exercise_revision_id=%s",
            (REVISION_ID,),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            "SELECT normalized_name FROM trainer.muscle_groups WHERE muscle_group_id=%s",
            (GROUP_ID,),
        )
        assert cursor.fetchone() == ("   ",)
        cursor.execute(
            "SELECT metric_kind,requiredness,unit_family "
            "FROM trainer.exercise_measurements WHERE exercise_measurement_id=%s",
            (MEASUREMENT_ID,),
        )
        assert cursor.fetchone() == ("duration", "required", "count")


def correct_only_synthetic_conflicts() -> None:
    with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM trainer.exercise_aliases WHERE exercise_alias_id=%s", (ALIAS_TWO_ID,)
        )
        cursor.execute(
            "UPDATE trainer.muscle_groups SET normalized_name='legacy corrected' "
            "WHERE muscle_group_id=%s",
            (GROUP_ID,),
        )
        cursor.execute(
            """
            INSERT INTO trainer.metric_kind_catalog(code,description,seed_version,created_at)
            VALUES ('repetitions','Repetitions',1,now())
            """
        )
        cursor.execute(
            "UPDATE trainer.exercise_measurements "
            "SET metric_kind='repetitions' WHERE exercise_measurement_id=%s",
            (MEASUREMENT_ID,),
        )


def assert_recovery_and_repeat() -> None:
    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == (MIGRATIONS[-1].revision,)
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id=%s",
            ("0009_d_g03_validation_hardening",),
        )
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT count(*) FROM trainer.exercise_measurement_compatibility")
        assert cursor.fetchone() == (6,)
        cursor.execute(
            "SELECT count(*) FROM trainer.metric_kind_catalog "
            "WHERE code IN ('repetitions','duration','distance') AND archived_at IS NULL"
        )
        assert cursor.fetchone() == (3,)
        cursor.execute(
            "SELECT count(*) FROM trainer.unit_family_catalog "
            "WHERE code IN ('count','time','length') AND archived_at IS NULL"
        )
        assert cursor.fetchone() == (3,)
        cursor.execute(
            "SELECT has_table_privilege"
            "('trainer_app','trainer.exercise_measurement_compatibility','SELECT'),"
            "has_table_privilege"
            "('trainer_app','trainer.exercise_measurement_compatibility','INSERT')"
        )
        assert cursor.fetchone() == (True, False)
        cursor.execute(
            "SELECT count(*) FROM trainer.migration_attempts "
            "WHERE migration_id=%s AND result='failed'",
            ("0009_d_g03_validation_hardening",),
        )
        failed_attempts = cursor.fetchone()
        assert failed_attempts is not None and failed_attempts[0] >= 1


def remove_only_synthetic_domain_rows() -> None:
    with psycopg.connect(ADMIN_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM trainer.exercise_aliases WHERE exercise_alias_id=%s", (ALIAS_ONE_ID,)
        )
        cursor.execute(
            "DELETE FROM trainer.exercise_measurements WHERE exercise_measurement_id=%s",
            (MEASUREMENT_ID,),
        )
        cursor.execute("DELETE FROM trainer.muscle_groups WHERE muscle_group_id=%s", (GROUP_ID,))


def main() -> None:
    upgrade_to_0008()
    insert_conflicting_legacy_rows()
    assert_failed_without_partial_effect()
    correct_only_synthetic_conflicts()
    assert_recovery_and_repeat()
    remove_only_synthetic_domain_rows()
    print("PASS: D-G03-R02 migration 0008->0009 fail-closed/recovery/repeat")


if __name__ == "__main__":
    main()
