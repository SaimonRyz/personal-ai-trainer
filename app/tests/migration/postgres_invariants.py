"""Executable PostgreSQL invariant probe for the isolated Stage C test database."""

from __future__ import annotations

import os
import threading
import time
from queue import Queue
from uuid import UUID, uuid4

import psycopg
from psycopg.errors import UniqueViolation


def dsn() -> str:
    value = os.environ.get("TRAINER_TEST_ADMIN_DSN")
    if not value:
        raise RuntimeError("TRAINER_TEST_ADMIN_DSN is required")
    return value


def prepare_fixture(connection: psycopg.Connection[tuple[object, ...]]) -> UUID:
    confirmation_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute("SET session_replication_role=replica")
        cursor.execute(
            """
            INSERT INTO trainer.confirmations(
                confirmation_id,preview_id,draft_revision_id,conversation_id,
                user_message_id,kind,explicit_expression,confirmed_at,created_at
            ) VALUES (%s,%s,%s,'stage-c','stage-c','normal','yes',now(),now())
            """,
            (confirmation_id, uuid4(), uuid4()),
        )
        cursor.execute("SET session_replication_role=origin")
        cursor.execute(
            """
            INSERT INTO trainer.exercise_category_catalog(code,description,seed_version,created_at)
            VALUES ('stage_c_probe','Stage C synthetic probe',1,now())
            ON CONFLICT (code) DO NOTHING
            """
        )
        cursor.execute(
            """
            INSERT INTO trainer.exercise_status_catalog(code,description,seed_version,created_at)
            VALUES ('stage_c_active','Stage C synthetic probe',1,now())
            ON CONFLICT (code) DO NOTHING
            """
        )
    connection.commit()
    return confirmation_id


def insert_exercise(
    connection: psycopg.Connection[tuple[object, ...]],
    *,
    confirmation_id: UUID,
    normalized_name: str,
) -> tuple[UUID, UUID]:
    exercise_id = uuid4()
    revision_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        cursor.execute(
            """
            INSERT INTO trainer.exercises(
                exercise_id,current_revision_id,created_at
            ) VALUES (%s,%s,now())
            """,
            (exercise_id, revision_id),
        )
        cursor.execute(
            """
            INSERT INTO trainer.exercise_revisions(
                exercise_revision_id,exercise_id,revision_no,canonical_name,
                normalized_name,category,unilateral,status,confirmation_id,
                confirmed_at,created_at
            ) VALUES (%s,%s,1,%s,%s,'stage_c_probe',false,'stage_c_active',%s,now(),now())
            """,
            (
                revision_id,
                exercise_id,
                normalized_name,
                normalized_name,
                confirmation_id,
            ),
        )
    return exercise_id, revision_id


def verify_serial_behaviour(
    connection: psycopg.Connection[tuple[object, ...]], confirmation_id: UUID
) -> None:
    first_id, first_revision = insert_exercise(
        connection, confirmation_id=confirmation_id, normalized_name="stage-c-squat"
    )
    connection.commit()

    next_revision = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO trainer.exercise_revisions(
                exercise_revision_id,exercise_id,revision_no,canonical_name,
                normalized_name,category,unilateral,status,supersedes_revision_id,
                confirmation_id,confirmed_at,created_at
            ) VALUES (%s,%s,2,'stage-c-squat','stage-c-squat','stage_c_probe',false,
                      'stage_c_active',%s,%s,now(),now())
            """,
            (next_revision, first_id, first_revision, confirmation_id),
        )
        cursor.execute(
            "UPDATE trainer.exercises SET current_revision_id=%s WHERE exercise_id=%s",
            (next_revision, first_id),
        )
    connection.commit()

    try:
        insert_exercise(
            connection, confirmation_id=confirmation_id, normalized_name="stage-c-squat"
        )
        connection.commit()
    except UniqueViolation:
        connection.rollback()
    else:
        raise RuntimeError("duplicate active current exercise name was accepted")

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE trainer.exercises SET archived_at=now() WHERE exercise_id=%s",
            (first_id,),
        )
    connection.commit()
    second_id, second_revision = insert_exercise(
        connection, confirmation_id=confirmation_id, normalized_name="stage-c-squat"
    )
    connection.commit()

    renamed_revision = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO trainer.exercise_revisions(
                exercise_revision_id,exercise_id,revision_no,canonical_name,
                normalized_name,category,unilateral,status,supersedes_revision_id,
                confirmation_id,confirmed_at,created_at
            ) VALUES (%s,%s,2,'stage-c-front-squat','stage-c-front-squat','stage_c_probe',
                      false,'stage_c_active',%s,%s,now(),now())
            """,
            (renamed_revision, second_id, second_revision, confirmation_id),
        )
        cursor.execute(
            "UPDATE trainer.exercises SET current_revision_id=%s WHERE exercise_id=%s",
            (renamed_revision, second_id),
        )
    connection.commit()
    insert_exercise(connection, confirmation_id=confirmation_id, normalized_name="stage-c-squat")
    connection.commit()


def verify_concurrent_duplicate(confirmation_id: UUID) -> None:
    first_inserted = threading.Event()
    release_first = threading.Event()
    results: Queue[str] = Queue()

    def first() -> None:
        with psycopg.connect(dsn()) as connection:
            insert_exercise(
                connection,
                confirmation_id=confirmation_id,
                normalized_name="stage-c-concurrent",
            )
            first_inserted.set()
            release_first.wait(timeout=5)
            connection.commit()
            results.put("first_committed")

    def second() -> None:
        first_inserted.wait(timeout=5)
        try:
            with psycopg.connect(dsn()) as connection:
                insert_exercise(
                    connection,
                    confirmation_id=confirmation_id,
                    normalized_name="stage-c-concurrent",
                )
                connection.commit()
        except UniqueViolation:
            results.put("second_rejected")
        else:
            results.put("second_committed")

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    first_inserted.wait(timeout=5)
    time.sleep(0.2)
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    observed = {results.get(timeout=1), results.get(timeout=1)}
    if observed != {"first_committed", "second_rejected"}:
        raise RuntimeError(f"concurrent uniqueness result was unsafe: {observed}")


def main() -> None:
    with psycopg.connect(dsn()) as connection:
        confirmation_id = prepare_fixture(connection)
        verify_serial_behaviour(connection, confirmation_id)
    verify_concurrent_duplicate(confirmation_id)
    print("postgres invariant probes passed")


if __name__ == "__main__":
    main()
