from __future__ import annotations

import uvicorn

from trainer.application.context import ContextService
from trainer.config.settings import load_settings
from trainer.infrastructure.db.engine import create_runtime_engine
from trainer.infrastructure.db.evidence import (
    EvidenceAwareProgressExecutor,
    EvidenceDatabaseExecutor,
)
from trainer.infrastructure.db.readiness import PostgresReadinessProbe
from trainer.transport.http.app import create_app


def run() -> None:
    """Runtime entrypoint is completed by C-030; no domain behavior is exposed."""
    settings = load_settings()
    readiness_probe = (
        PostgresReadinessProbe(settings.database_app_dsn, settings.database_connect_timeout_seconds)
        if settings.database_app_dsn is not None
        else None
    )
    context_service = None
    if settings.database_app_dsn is not None:
        engine = create_runtime_engine(settings)
        context_service = ContextService(
            EvidenceAwareProgressExecutor(
                engine,
                EvidenceDatabaseExecutor(engine),
                eligible_exercise_ids=settings.e1rm_bench_exercise_ids,
                eligible_grip_conditions=settings.e1rm_bench_grip_conditions,
            )
        )
    uvicorn.run(
        create_app(settings, readiness_probe, context_service),
        host=settings.api_host,
        port=settings.api_port,
        access_log=False,
        log_config=None,
    )
