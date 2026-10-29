# ruff: noqa: E501
"""Run only against the gate's isolated synthetic PostgreSQL volume."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head


def main() -> None:
    command.upgrade(Config("alembic.ini"), "0009_d_g03_validation_hardening")
    legacy_id = uuid4()
    dsn = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SET session_replication_role=replica")
        c.execute(
            "INSERT INTO trainer.programs(program_id,profile_id,goal_id,name,status,created_at) VALUES (%s,%s,%s,'synthetic legacy','active',now())",
            (legacy_id, uuid4(), uuid4()),
        )
        c.execute("SET session_replication_role=origin")
    try:
        migrate_to_head()
    except Exception as error:
        assert "empty legacy program" in str(error)
    else:
        raise AssertionError("nonempty legacy program schema accepted")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == ("0009_d_g03_validation_hardening",)
        c.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0010_d_g04_programs'"
        )
        assert c.fetchone() == (0,)
        c.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema='trainer' AND table_name='programs' AND column_name='current_version_id'"
        )
        assert c.fetchone() == (0,)
        c.execute("DELETE FROM trainer.programs WHERE program_id=%s", (legacy_id,))
    command.upgrade(Config("alembic.ini"), "0010_d_g04_programs")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute(
            "UPDATE trainer.program_status_catalog SET description='conflicted synthetic seed' WHERE code='active'"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "exact active 0010 seed rows" in str(error)
    else:
        raise AssertionError("conflicting D-G04 seed was accepted")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == ("0010_d_g04_programs",)
        c.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0011_d_g04_correction_hardening'"
        )
        assert c.fetchone() == (0,)
        c.execute(
            "UPDATE trainer.program_status_catalog SET description='Active program' WHERE code='active'"
        )
    command.upgrade(Config("alembic.ini"), "0011_d_g04_correction_hardening")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute(
            "UPDATE trainer.dependency_type_catalog SET description='conflicted dependency' WHERE code='program'"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "exact seed preflight" in str(error)
    else:
        raise AssertionError("conflicting D-G04 dependency description was accepted")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == ("0011_d_g04_correction_hardening",)
        c.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0012_d_g04_seed_exactness'"
        )
        assert c.fetchone() == (0,)
        c.execute(
            "UPDATE trainer.dependency_type_catalog SET description='Program' WHERE code='program'"
        )
        c.execute(
            "UPDATE trainer.unit_family_mapping SET family_code='time' WHERE unit_code='repetition'"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "exact seed preflight" in str(error)
    else:
        raise AssertionError("conflicting D-G04 unit family mapping was accepted")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == ("0011_d_g04_correction_hardening",)
        c.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0012_d_g04_seed_exactness'"
        )
        assert c.fetchone() == (0,)
        c.execute(
            "UPDATE trainer.unit_family_mapping SET family_code='count' WHERE unit_code='repetition'"
        )
    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute("SELECT version_num FROM trainer.alembic_version")
        assert c.fetchone() == (MIGRATIONS[-1].revision,)
        c.execute("SELECT count(*) FROM trainer.unit_family_mapping")
        assert c.fetchone() == (3,)
        c.execute(
            "SELECT has_column_privilege('trainer_app','trainer.programs','current_version_id','UPDATE'),has_column_privilege('trainer_app','trainer.programs','name','UPDATE'),has_column_privilege('trainer_app','trainer.program_versions','publication_state','UPDATE'),has_column_privilege('trainer_app','trainer.program_versions','rationale','UPDATE')"
        )
        assert c.fetchone() == (True, False, True, False)
        c.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id='0010_d_g04_programs'",
            ("0" * 64,),
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "checksum" in str(error)
    else:
        raise AssertionError("modified ledger accepted")
    with psycopg.connect(dsn) as db, db.cursor() as c:
        c.execute(
            "UPDATE trainer.schema_migrations SET checksum=%s WHERE migration_id='0010_d_g04_programs'",
            (next(item.checksum for item in MIGRATIONS if item.revision == "0010_d_g04_programs"),),
        )
    migrate_to_head()
    print(
        "PASS: D-G04 migration clean/repeat/nonempty/exact-seed-conflict preflight/recovery/checksum/grants"
    )


if __name__ == "__main__":
    main()
