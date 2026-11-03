"""Read a program published before migration 0030 after the additive upgrade."""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from trainer.infrastructure.db.context_repository import ContextRepository
from trainer.transport.http.contracts import ProgramResponse


def main() -> None:
    engine = create_engine(os.environ["TRAINER_DATABASE_APP_DSN"])
    with Session(engine) as session:
        program_id = session.execute(text("""
            SELECT program_id FROM trainer.programs
            WHERE current_version_id IS NOT NULL ORDER BY created_at LIMIT 1
        """)).scalar_one()
        result = ContextRepository(session).get_program(
            payload={"mode": "current", "program_id": str(program_id)}
        )
        saved = ProgramResponse.model_validate(result)
        assert saved.version.cycle_stages == []
        assert saved.version.templates
        assert all(day.weekday_code is None for day in saved.version.templates)
        assert saved.version.templates[0].blocks[0].exercises[0].sets
    print("PASS: pre-0030 program remains readable with unknown weekdays and empty stages")


if __name__ == "__main__":
    main()
