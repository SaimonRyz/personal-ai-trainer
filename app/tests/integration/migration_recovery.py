from __future__ import annotations

import os
import sys
from uuid import UUID

import psycopg

OPERATION_ID = UUID("00000000-0000-4000-8000-000000000602")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"inject", "clear"}:
        raise SystemExit("usage: migration_recovery.py inject|clear")
    dsn = os.environ["TRAINER_TEST_ADMIN_DSN"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        if sys.argv[1] == "inject":
            cursor.execute(
                """
                INSERT INTO trainer.idempotent_operations(
                    operation_id,scope,idempotency_key,command_kind,request_digest,state,
                    received_at,updated_at,created_at
                ) VALUES (%s,'synthetic:migration-recovery','unfinished','CMD-02',%s,
                    'executing',now(),now(),now())
                """,
                (OPERATION_ID, "0" * 64),
            )
        else:
            cursor.execute(
                "DELETE FROM trainer.idempotent_operations WHERE operation_id=%s",
                (OPERATION_ID,),
            )
        connection.commit()


if __name__ == "__main__":
    main()
