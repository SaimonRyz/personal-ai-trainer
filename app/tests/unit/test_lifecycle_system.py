from __future__ import annotations

import subprocess
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

from trainer.lifecycle.model import LifecycleReport, ProbeSnapshot
from trainer.lifecycle.system import LifecycleManager


def manager(tmp_path: Path) -> LifecycleManager:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# test\n", encoding="utf-8")
    return LifecycleManager(tmp_path)


def test_container_probe_requires_exact_compose_config_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    observed: list[list[str]] = []

    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    assert lifecycle._project_containers() == ()
    command = observed[-1]
    assert "label=com.docker.compose.project=personal-ai-trainer" in command
    assert f"label=com.docker.compose.project.config_files={tmp_path / 'compose.yaml'}" in command


def test_ambiguous_mcp_ownership_never_signals_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    lifecycle.runtime_dir.mkdir(parents=True)
    lifecycle.mcp_path.write_text(
        '{"pid":42,"started_signature":"old","port":8766}', encoding="utf-8"
    )
    monkeypatch.setattr(lifecycle, "_mcp_owned", lambda: False)
    monkeypatch.setattr(lifecycle, "_process_signature", lambda _: ("foreign-python", "new"))
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: (42,))

    def forbidden_kill(*_: object) -> None:
        raise AssertionError("ambiguous process must not be signalled")

    monkeypatch.setattr("trainer.lifecycle.system.os.kill", forbidden_kill)
    errors = lifecycle._stop_owned_mcp()
    assert errors == ["MCP metadata exists but ownership cannot be proven"]
    assert lifecycle.mcp_path.exists()


def test_stale_owned_metadata_is_cleaned_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    lifecycle.runtime_dir.mkdir(parents=True)
    lifecycle.mcp_path.write_text(
        '{"pid":42,"started_signature":"old","port":8766}', encoding="utf-8"
    )
    monkeypatch.setattr(lifecycle, "_mcp_owned", lambda: False)
    monkeypatch.setattr(lifecycle, "_process_signature", lambda _: None)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    monkeypatch.setattr(
        "trainer.lifecycle.system.os.kill",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not signal stale PID")),
    )
    assert lifecycle._stop_owned_mcp() == []
    assert not lifecycle.mcp_path.exists()


def test_stale_transition_state_becomes_partially_stopped(tmp_path: Path) -> None:
    lifecycle = manager(tmp_path)
    lifecycle.runtime_dir.mkdir(parents=True)
    lifecycle.state_path.write_text(
        '{"state":"STARTING","operation_pid":999999,"operation_started_signature":"old"}',
        encoding="utf-8",
    )
    assert lifecycle._operation_state() == "PARTIALLY_STOPPED"


def test_full_stop_inventory_refuses_foreign_changed_or_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    foreign_values: Iterator[tuple[str, ...] | None] = iter([("foreign:a",), ("foreign:a",)])
    monkeypatch.setattr(lifecycle, "_running_container_inventory", lambda: next(foreign_values))
    assert lifecycle._full_stop_inventory_errors() == ("foreign:a",)

    changed_values: Iterator[tuple[str, ...] | None] = iter([(), ("foreign:b",)])
    monkeypatch.setattr(lifecycle, "_running_container_inventory", lambda: next(changed_values))
    assert lifecycle._full_stop_inventory_errors() == (
        "docker_inventory_changed",
        "foreign:b",
    )

    unknown_values: Iterator[tuple[str, ...] | None] = iter([None, None])
    monkeypatch.setattr(lifecycle, "_running_container_inventory", lambda: next(unknown_values))
    assert lifecycle._full_stop_inventory_errors() == ("docker_inventory_unavailable",)


def lifecycle_snapshot(*, engine: bool) -> ProbeSnapshot:
    return ProbeSnapshot(
        docker_engine=engine,
        docker_desktop_stopped=not engine,
        project_containers=(),
        mcp_owned=False,
        mcp_listener=False,
        api_listener=False,
        api_ready=False,
        runtime_files=(),
        volume_exists=True if engine else None,
    )


def test_successful_full_stop_requires_clean_double_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    off = LifecycleReport.create("OFF", lifecycle_snapshot(engine=True), "off")
    fully_off = LifecycleReport.create("FULLY_OFF", lifecycle_snapshot(engine=False), "fully off")
    reports = iter([off, fully_off])
    commands: list[list[str]] = []

    monkeypatch.setattr(lifecycle, "_establish_stop_barrier", lambda: [])
    monkeypatch.setattr(lifecycle, "_stop_owned_mcp", lambda: [])
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)
    monkeypatch.setattr(lifecycle, "snapshot", lambda: lifecycle_snapshot(engine=True))
    monkeypatch.setattr(lifecycle, "status", lambda: next(reports))
    monkeypatch.setattr(lifecycle, "_full_stop_inventory_errors", lambda: ())
    monkeypatch.setattr(lifecycle, "_wait_for", lambda *_: True)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    result = lifecycle.stop(full=True)
    assert result.state == "FULLY_OFF"
    assert ["docker", "desktop", "stop"] in commands


def test_start_is_idempotent_when_already_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    ready_snapshot = ProbeSnapshot(
        docker_engine=True,
        docker_desktop_stopped=False,
        project_containers=("api", "postgres"),
        mcp_owned=True,
        mcp_listener=True,
        api_listener=True,
        api_ready=True,
        runtime_files=("mcp.json",),
        volume_exists=True,
    )
    ready = LifecycleReport.create("READY", ready_snapshot, "ready")
    monkeypatch.setattr(lifecycle, "status", lambda: ready)
    monkeypatch.setattr(
        lifecycle,
        "_ensure_docker",
        lambda: (_ for _ in ()).throw(AssertionError("idempotent start changed state")),
    )
    assert lifecycle.start().state == "READY"


def test_database_setup_uses_one_shot_containers_without_secrets_in_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    commands: list[list[str]] = []
    environments: list[dict[str, str] | None] = []
    secret = "safe-test-secret-value"
    monkeypatch.setattr(
        lifecycle,
        "_local_environment",
        lambda: {
            "TRAINER_POSTGRES_ADMIN_PASSWORD": secret + "-admin",
            "TRAINER_DATABASE_APP_PASSWORD": secret + "-app",
            "TRAINER_DATABASE_MIGRATOR_PASSWORD": secret + "-migrator",
            "TRAINER_API_TOKEN": secret + "-token-value",
        },
    )
    monkeypatch.setattr(lifecycle, "_wait_for", lambda predicate, _: predicate())

    def fake_run(
        command: list[str], *, env: dict[str, str] | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        environments.append(env)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    lifecycle._prepare_database()

    assert commands[0] == ["docker", "compose", "up", "-d", "postgres"]
    assert sum("run" in command for command in commands) == 3
    assert all(secret not in " ".join(command) for command in commands)
    run_environments = [
        environment
        for command, environment in zip(commands, environments, strict=True)
        if "run" in command
    ]
    assert all(environment is not None for environment in run_environments)
    assert any(
        "TRAINER_DATABASE_ADMIN_DSN" in environment
        for environment in run_environments
        if environment
    )


def test_stop_barrier_falls_back_to_stopping_only_project_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    commands: list[list[str]] = []
    port_checks = iter([(10,), ()])
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: next(port_checks))
    monkeypatch.setattr(lifecycle, "_local_api_token", lambda: "x" * 32)
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)
    monkeypatch.setattr(lifecycle, "_wait_for", lambda predicate, _: predicate())
    monkeypatch.setattr(
        "trainer.lifecycle.system.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("unavailable")),
    )

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    assert lifecycle._establish_stop_barrier() == []
    assert commands == [["docker", "compose", "stop", "api"]]


def test_repeated_ordinary_stop_remains_off_and_preserves_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    off_snapshot = lifecycle_snapshot(engine=True)
    off = LifecycleReport.create("OFF", off_snapshot, "off")
    commands: list[list[str]] = []

    monkeypatch.setattr(lifecycle, "_establish_stop_barrier", lambda: [])
    monkeypatch.setattr(lifecycle, "_stop_owned_mcp", lambda: [])
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)
    monkeypatch.setattr(lifecycle, "snapshot", lambda: off_snapshot)
    monkeypatch.setattr(lifecycle, "status", lambda: off)
    monkeypatch.setattr(
        "trainer.lifecycle.system.os.kill",
        lambda *_: (_ for _ in ()).throw(AssertionError("ordinary stop signalled a PID")),
    )

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    assert lifecycle.stop().state == "OFF"
    assert lifecycle.stop().state == "OFF"
    assert off_snapshot.volume_exists is True
    docker_commands = [command for command in commands if command[:1] == ["docker"]]
    assert docker_commands == [
        ["docker", "compose", "down", "--remove-orphans"],
        ["docker", "compose", "down", "--remove-orphans"],
    ]
    assert all("-v" not in command and "--volumes" not in command for command in docker_commands)
    assert ["docker", "desktop", "stop"] not in docker_commands
