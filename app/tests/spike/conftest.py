from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def spike_api_url() -> Iterator[str]:
    yield from _serve_fixture("trainer.spike.app:app")


@pytest.fixture(scope="session")
def g04_api_url() -> Iterator[str]:
    yield from _serve_fixture("tests.spike.g04_app:app")


@pytest.fixture(scope="session")
def g05_api_url() -> Iterator[str]:
    yield from _serve_fixture("tests.spike.f_g05_app:app")


@pytest.fixture(scope="session")
def f_g04_lifecycle_api_url() -> Iterator[str]:
    yield from _serve_fixture("tests.spike.f_g04_lifecycle_app:app")


@pytest.fixture(scope="session")
def f_g03_workout_api_url() -> Iterator[str]:
    yield from _serve_fixture("tests.spike.f_g03_workout_app:app")


@pytest.fixture(scope="session")
def f_g02_plan_api_url() -> Iterator[str]:
    yield from _serve_fixture("tests.spike.f_g02_plan_app:app")


@pytest.fixture
def g04_mcp_url(g04_api_url: str) -> Iterator[str]:
    yield from _serve_mcp(g04_api_url, "G04")


@pytest.fixture
def g05_mcp_url(g05_api_url: str) -> Iterator[str]:
    yield from _serve_mcp(g05_api_url, "F-G05")


@pytest.fixture
def f_g04_lifecycle_mcp_url(f_g04_lifecycle_api_url: str) -> Iterator[str]:
    yield from _serve_mcp(f_g04_lifecycle_api_url, "F-G04 lifecycle")


@pytest.fixture
def f_g03_workout_mcp_url(f_g03_workout_api_url: str) -> Iterator[str]:
    yield from _serve_mcp(f_g03_workout_api_url, "F-G03 workout")


@pytest.fixture
def f_g02_plan_mcp_url(f_g02_plan_api_url: str) -> Iterator[str]:
    yield from _serve_mcp(f_g02_plan_api_url, "F-G02 plan")


def _serve_mcp(api_url: str, label: str) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "TRAINER_API_URL": api_url,
        "TRAINER_API_TOKEN": "spike-local-token",
        "TRAINER_MCP_TRANSPORT": "streamable-http",
        "TRAINER_MCP_PORT": str(port),
    }
    process = subprocess.Popen(
        [".venv/bin/python", "-m", "trainer_mcp.server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{label} synthetic MCP process exited before readiness")
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError(f"{label} synthetic MCP process did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def _serve_fixture(app_path: str) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            ".venv/bin/uvicorn",
            app_path,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError("spike API did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=5)
