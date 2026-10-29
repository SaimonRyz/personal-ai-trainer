from __future__ import annotations

import os

import psycopg
from psycopg import sql

APP_ROLE = "trainer_app"
MIGRATOR_ROLE = "trainer_migrator"


def required_secret(name: str) -> str:
    value = os.environ.get(name)
    if value is None or len(value) < 20:
        raise RuntimeError(f"{name} is required and must contain at least 20 characters")
    return value


def ensure_role(
    connection: psycopg.Connection[tuple[object, ...]], role: str, password: str
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        else:
            cursor.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )


def bootstrap_database() -> None:
    admin_dsn = required_secret("TRAINER_DATABASE_ADMIN_DSN")
    app_password = required_secret("TRAINER_DATABASE_APP_PASSWORD")
    migrator_password = required_secret("TRAINER_DATABASE_MIGRATOR_PASSWORD")
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        ensure_role(connection, APP_ROLE, app_password)
        ensure_role(connection, MIGRATOR_ROLE, migrator_password)
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    sql.Identifier(connection.info.dbname),
                    sql.Identifier(APP_ROLE),
                    sql.Identifier(MIGRATOR_ROLE),
                )
            )
            cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
            cursor.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS trainer AUTHORIZATION {}").format(
                    sql.Identifier(MIGRATOR_ROLE)
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA trainer TO {}").format(sql.Identifier(APP_ROLE))
            )
