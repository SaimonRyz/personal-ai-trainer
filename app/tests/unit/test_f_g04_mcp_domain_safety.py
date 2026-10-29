"""Offline safety regression for the isolated F-G04 MCP/domain gate."""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

GATE_SCRIPT = Path(__file__).resolve().parents[1] / "integration" / "f_g04_mcp_domain.py"
VALID_PROJECT = "trainer-eg07-gate-2026101504"


@pytest.fixture
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("f_g04_mcp_domain_safety_target", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_rejected_before_mcp(gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    start = Mock(side_effect=AssertionError("MCP process must not start"))
    listener = Mock(side_effect=AssertionError("socket must not open"))
    monkeypatch.setattr(gate.subprocess, "Popen", start)
    monkeypatch.setattr(gate, "listener_is_open", listener)
    with pytest.raises(RuntimeError):
        asyncio.run(gate.main())
    start.assert_not_called()
    listener.assert_not_called()


@pytest.mark.parametrize("marker", [None, "trainer-eg07-gate-invalid"])
def test_missing_or_malformed_marker_rejected_before_mcp(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch, marker: str | None
) -> None:
    if marker is None:
        monkeypatch.delenv("TRAINER_FG04_ISOLATED_PROJECT", raising=False)
    else:
        monkeypatch.setenv("TRAINER_FG04_ISOLATED_PROJECT", marker)
    assert_rejected_before_mcp(gate, monkeypatch)


def test_missing_docker_marker_rejected_before_mcp(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAINER_FG04_ISOLATED_PROJECT", VALID_PROJECT)
    monkeypatch.setattr(gate, "Path", lambda _: SimpleNamespace(is_file=lambda: False))
    assert_rejected_before_mcp(gate, monkeypatch)


def test_hostname_mismatch_rejected_before_mcp(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAINER_FG04_ISOLATED_PROJECT", VALID_PROJECT)
    monkeypatch.setattr(gate, "Path", lambda _: SimpleNamespace(is_file=lambda: True))
    monkeypatch.setattr(gate.socket, "gethostname", lambda: "main-api-container")
    assert_rejected_before_mcp(gate, monkeypatch)


def test_matching_isolated_marker_is_accepted(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAINER_FG04_ISOLATED_PROJECT", VALID_PROJECT)
    monkeypatch.setattr(gate, "Path", lambda _: SimpleNamespace(is_file=lambda: True))
    monkeypatch.setattr(gate.socket, "gethostname", lambda: VALID_PROJECT)
    assert gate.require_isolated_gate() == VALID_PROJECT


class HangingProcess:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.killed = False

    def poll(self) -> int | None:
        return 0 if self.killed else None

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, timeout: int) -> int:
        self.calls.append(f"wait:{timeout}")
        if not self.killed:
            raise subprocess.TimeoutExpired("mcp", timeout)
        return 0

    def kill(self) -> None:
        self.calls.append("kill")
        self.killed = True


def test_mcp_timeout_forces_kill_then_wait_and_checks_port_release(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = HangingProcess()
    checks: list[int] = []
    open_states = iter([True, False, False])

    def listener(port: int) -> bool:
        checks.append(port)
        return next(open_states)

    monkeypatch.setattr(gate, "listener_is_open", listener)
    monkeypatch.setattr(gate, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _: None))
    gate.stop_mcp_process(process, 8766)

    assert process.calls == ["terminate", "wait:5", "kill", "wait:5"]
    assert checks == [8766, 8766, 8766]


def test_mcp_cleanup_rejects_listener_that_remains_open(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = HangingProcess()
    moments = iter([0.0, 6.0])
    monkeypatch.setattr(gate, "listener_is_open", lambda _: True)
    monkeypatch.setattr(
        gate, "time", SimpleNamespace(monotonic=lambda: next(moments), sleep=lambda _: None)
    )
    with pytest.raises(AssertionError, match="listener remains"):
        gate.stop_mcp_process(process, 8766)
    assert process.killed
