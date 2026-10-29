from __future__ import annotations

from sqlalchemy import Engine, create_engine

from trainer.config.settings import Settings


def create_runtime_engine(settings: Settings) -> Engine:
    dsn = settings.database_app_dsn
    if dsn is None:
        raise RuntimeError("TRAINER_DATABASE_APP_DSN is required for database access")
    return create_engine(
        dsn.get_secret_value(),
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"connect_timeout": settings.database_connect_timeout_seconds},
    )
