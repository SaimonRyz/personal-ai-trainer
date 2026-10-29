"""Synthetic F-G04 Streamable HTTP MCP to real API/PostgreSQL gate."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession

GATE_PROJECT_PATTERN = re.compile(r"trainer-eg07-gate-[1-9][0-9]{9,15}\Z")


def require_isolated_gate() -> str:
    project = os.environ.get("TRAINER_FG04_ISOLATED_PROJECT", "")
    if GATE_PROJECT_PATTERN.fullmatch(project) is None:
        raise RuntimeError("F-G04 requires a unique isolated Compose gate project marker")
    if not Path("/.dockerenv").is_file():
        raise RuntimeError("F-G04 domain test must run inside its isolated Docker container")
    if socket.gethostname() != project:
        raise RuntimeError("F-G04 gate marker does not match the isolated container hostname")
    return project


def listener_is_open(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.2)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def stop_mcp_process(process: subprocess.Popen[bytes], port: int) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            process.kill()
        process.wait(timeout=5)
    if process.poll() is None:
        raise AssertionError("F-G04 MCP process did not exit")
    deadline = time.monotonic() + 5
    while listener_is_open(port) and time.monotonic() < deadline:
        time.sleep(0.05)
    if listener_is_open(port):
        raise AssertionError("F-G04 MCP listener remains after process cleanup")


async def call(session: ClientSession, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    result = await session.call_tool(name, arguments=arguments)
    assert result.isError is False, f"{name} failed"
    assert result.structuredContent is not None, f"{name} returned no structured content"
    envelope = result.structuredContent
    data = envelope.get("data")
    assert isinstance(data, dict), f"{name} returned no structured data"
    return cast(dict[str, Any], data)


@asynccontextmanager
async def mcp_session() -> AsyncIterator[ClientSession]:
    require_isolated_gate()
    port = 8766
    if listener_is_open(port):
        raise RuntimeError("F-G04 MCP port is already occupied")
    process = subprocess.Popen(
        [sys.executable, "-m", "trainer_mcp.server"],
        env={
            **os.environ,
            "TRAINER_API_URL": "http://127.0.0.1:8765",
            "TRAINER_MCP_TRANSPORT": "streamable-http",
            "TRAINER_MCP_PORT": str(port),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, "MCP process exited before readiness"
            if listener_is_open(port):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("MCP process did not start")
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            yield session
    finally:
        stop_mcp_process(process, port)


async def main() -> None:
    require_isolated_gate()
    run_id = uuid4().hex
    conversation_id = f"synthetic-fg04-domain-{run_id}"
    correlation_id = str(uuid4())
    causation_id = str(uuid4())
    shared = {
        "conversation_id": conversation_id,
        "timezone": "Asia/Novokuznetsk",
        "correlation_id": correlation_id,
        "causation_id": causation_id,
    }
    async with mcp_session() as session:
        await session.initialize()

        history = await call(
            session,
            "get_workout_history",
            {
                **shared,
                "message_id": "history",
                "query": {
                    "mode": "range",
                    "date_from": "2026-09-01",
                    "date_to": "2026-10-31",
                    "exercise_id": None,
                    "lifecycle": [],
                    "limit": 100,
                    "cursor": None,
                },
            },
        )
        assert history["items"], "confirmed synthetic workout is absent from QRY-05"
        for item in history["items"]:
            assert item["session_ref"]["object_type"] == "workout_session"
            assert item["session_ref"]["object_id"]
            assert item["session_ref"]["revision_id"]
            assert item["confirmation_ref"]["object_type"] == "confirmation"
            assert item["confirmation_ref"]["object_id"]
            confirmed_at = datetime.fromisoformat(item["confirmed_at"])
            assert confirmed_at.tzinfo is not None
        source_items = [
            item
            for item in history["items"]
            if item["local_start_date"] == "2026-09-23"
            and item["workout"]["name"] == "Synthetic D-G06 planned workout"
            and str(item["workout"]["comment"]).startswith(
                "multiple loads, sides, skipped/interrupted sets and required unknown duration"
            )
        ]
        assert source_items, "the expected synthetic workout source is absent from QRY-05"
        assert any(
            item["current"]
            and item["workout"]["blocks"]
            and item["plan_ref"]
            and item["plan_content_digest"]
            for item in source_items
        )

        draft = await call(
            session,
            "create_measurement_session_draft",
            {
                **shared,
                "message_id": "measurement-draft",
                "measurement": {
                    "intent": "create",
                    "session": {
                        "measured_on": "2026-10-20",
                        "measured_time": "08:00:00",
                        "conditions": "Synthetic isolated PostgreSQL gate",
                        "comment": None,
                        "values": [
                            {
                                "measurement_kind": "body_mass",
                                "original_value": "99.7",
                                "original_unit": "kilogram",
                                "side": None,
                                "method": None,
                                "comment": None,
                            }
                        ],
                    },
                },
                "idempotency_key": f"fg04-measurement-draft-{run_id}",
            },
        )
        preview = await call(
            session,
            "render_preview",
            {
                **shared,
                "message_id": "measurement-preview",
                "revision_id": draft["revision_id"],
                "detail_level": "full",
                "idempotency_key": f"fg04-measurement-preview-{run_id}",
            },
        )
        assert preview["complete"] is True
        assert preview["revision_id"] == draft["revision_id"]
        assert preview["canonical_text"] and preview["digest"]

        await call(
            session,
            "acknowledge_preview_shown",
            {
                **shared,
                "preview_id": preview["preview_id"],
                "revision_id": draft["revision_id"],
                "preview_digest": preview["digest"],
                "canonical_text": preview["canonical_text"],
                "shown_message_id": "measurement-shown",
                "idempotency_key": f"fg04-measurement-shown-{run_id}",
                "cross_dialog": False,
            },
        )
        committed = await call(
            session,
            "confirm_preview",
            {
                **shared,
                "preview_id": preview["preview_id"],
                "revision_id": draft["revision_id"],
                "preview_digest": preview["digest"],
                "confirmation_message_id": "measurement-confirm",
                "idempotency_key": f"fg04-measurement-confirm-{run_id}",
                "explicit_intent": "Confirm this synthetic measurement preview only",
                "confirmation_mode": "same_dialog",
                "confirmation_kind": "normal",
            },
        )
        assert committed["status"] == "committed"
        created = committed["created_revisions"]
        assert len(created) == 1

        measurements = await call(
            session,
            "get_body_measurements",
            {
                **shared,
                "message_id": "measurement-readback",
                "query": {
                    "mode": "sessions",
                    "view": "current",
                    "date_from": "2026-10-20",
                    "date_to": "2026-10-20",
                    "measurement_kind": "body_mass",
                    "side": None,
                    "limit": 100,
                    "cursor": None,
                },
            },
        )
        saved = [
            item
            for item in measurements["items"]
            if item["session_id"] == created[0]["revision_id"]
        ]
        assert len(saved) == 1
        assert any(
            value["measurement_kind"] == "body_mass"
            and Decimal(value["original_value"]) == Decimal("99.7")
            for value in saved[0]["values"]
        )

    print(
        "PASS: F-G04 MCP Streamable HTTP to real API/PostgreSQL "
        "QRY-05, CMD-11 preview/confirm, QRY-13"
    )


if __name__ == "__main__":
    asyncio.run(main())
