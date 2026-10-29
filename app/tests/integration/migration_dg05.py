# ruff: noqa: E501
"""D-G05 migration fail-closed, idempotency, seed, and role checks."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

from trainer.infrastructure.db.migrator import MIGRATIONS, migrate_to_head


def main() -> None:
    command.upgrade(Config("alembic.ini"), "0012_d_g04_seed_exactness")
    dsn = os.environ["TRAINER_DATABASE_ADMIN_DSN"]
    legacy_plan = uuid4()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SET session_replication_role=replica")
        cursor.execute(
            "INSERT INTO trainer.workout_plans(plan_id,profile_id,current_revision_id,created_at) VALUES (%s,%s,%s,now())",
            (legacy_plan, uuid4(), uuid4()),
        )
        cursor.execute("SET session_replication_role=origin")
    try:
        migrate_to_head()
    except Exception as error:
        assert "empty legacy plan tables" in str(error)
    else:
        raise AssertionError("D-G05 accepted a legacy plan without exact stored digests")
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0012_d_g04_seed_exactness",)
        cursor.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema='trainer' AND table_name='workout_plan_revisions' AND column_name='content_digest'"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("SET session_replication_role=replica")
        cursor.execute("DELETE FROM trainer.workout_plans WHERE plan_id=%s", (legacy_plan,))
        cursor.execute("SET session_replication_role=origin")

    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO trainer.plan_type_catalog(code,description,seed_version,created_at) VALUES ('training','conflicted synthetic seed',1,now())"
        )
    try:
        migrate_to_head()
    except Exception as error:
        assert "exact compatible catalog rows" in str(error)
    else:
        raise AssertionError("D-G05 accepted a conflicting plan_type seed")
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == ("0012_d_g04_seed_exactness",)
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0013_d_g05_plans'"
        )
        assert cursor.fetchone() == (0,)
        cursor.execute("DELETE FROM trainer.plan_type_catalog WHERE code='training'")

    migrate_to_head()
    migrate_to_head()
    with psycopg.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT version_num FROM trainer.alembic_version")
        assert cursor.fetchone() == (MIGRATIONS[-1].revision,)
        cursor.execute(
            "SELECT code,description,seed_version,archived_at FROM trainer.plan_type_catalog WHERE code='training'"
        )
        assert cursor.fetchone() == ("training", "Training session plan", 1, None)
        cursor.execute("SELECT count(*) FROM trainer.plan_type_catalog WHERE archived_at IS NULL")
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT has_column_privilege('trainer_app','trainer.workout_plans','current_revision_id','UPDATE'),has_column_privilege('trainer_app','trainer.workout_plans','profile_id','UPDATE'),has_table_privilege('trainer_app','trainer.workout_plan_revisions','DELETE'),has_table_privilege('trainer_app','trainer.workout_plan_revisions','UPDATE')"
        )
        assert cursor.fetchone() == (True, False, False, False)
        for table in (
            "planned_blocks",
            "planned_exercises",
            "planned_sets",
            "planned_metric_values",
            "planned_load_components",
            "recommendations",
            "recommendation_fact_links",
            "recommendation_calculation_links",
            "recommendation_conclusion_links",
            "recommendation_explanations",
        ):
            cursor.execute(
                "SELECT has_table_privilege('trainer_app',%s,'UPDATE'),has_table_privilege('trainer_app',%s,'DELETE')",
                (f"trainer.{table}", f"trainer.{table}"),
            )
            assert cursor.fetchone() == (False, False), table
        cursor.execute(
            "SELECT count(*) FROM trainer.schema_migrations WHERE migration_id='0013_d_g05_plans'"
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT count(*) FROM trainer.seed_versions WHERE seed_name='d_g05_plans' AND seed_version=1 AND checksum='5ae7a0797b9a2419c8da5b6edbcff727411a6294ff136b8892e1fed14c3fa036'"
        )
        assert cursor.fetchone() == (1,)
    print(
        "PASS: D-G05 migration legacy preflight, seed conflict, clean/repeat, exact seed, and immutable plan grants"
    )


if __name__ == "__main__":
    main()
