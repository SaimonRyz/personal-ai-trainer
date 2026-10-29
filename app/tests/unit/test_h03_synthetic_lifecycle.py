from __future__ import annotations

import json
import multiprocessing
import subprocess
import threading
import time
from pathlib import Path

import pytest

from trainer.lifecycle.model import LifecycleReport, ProbeSnapshot
from trainer.lifecycle.synthetic import (
    CANDIDATE_ID,
    CANDIDATE_TAG,
    SyntheticCandidateLifecycleManager,
)
from trainer.lifecycle.system import LifecycleError, LifecycleManager


def manager(tmp_path: Path) -> SyntheticCandidateLifecycleManager:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "compose.h03-synthetic.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# test\n", encoding="utf-8")
    lifecycle = SyntheticCandidateLifecycleManager(tmp_path)
    lifecycle._postgres_image_id_from_tag = lambda: "sha256:" + "a" * 64
    return lifecycle


def test_profile_is_private_unique_and_does_not_read_main_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    (tmp_path / ".env").write_text("TRAINER_API_TOKEN=main-private-token\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    profile = lifecycle._profile()
    assert profile is not None
    assert profile["project"].startswith("trainer-h03-")
    assert lifecycle.profile_path.stat().st_mode & 0o777 == 0o600
    assert lifecycle.runtime_dir.stat().st_mode & 0o777 == 0o700
    assert "main-private-token" not in json.dumps(profile)
    assert len({profile[name] for name in profile if name.startswith("TRAINER_")}) == 4


def test_corrupt_or_public_profile_fails_closed(tmp_path: Path) -> None:
    lifecycle = manager(tmp_path)
    lifecycle.runtime_dir.mkdir(parents=True)
    lifecycle.runtime_dir.chmod(0o700)
    lifecycle.profile_path.write_text("{}", encoding="utf-8")
    lifecycle.profile_path.chmod(0o600)
    with pytest.raises(LifecycleError, match="incomplete"):
        lifecycle._create_profile()
    lifecycle.profile_path.unlink()
    lifecycle.profile_path.symlink_to(tmp_path / ".env")
    with pytest.raises(LifecycleError, match="symlink"):
        lifecycle._profile()


def test_missing_profile_with_runtime_state_fails_closed(tmp_path: Path) -> None:
    lifecycle = manager(tmp_path)
    lifecycle.runtime_dir.mkdir(parents=True)
    lifecycle.runtime_dir.chmod(0o700)
    lifecycle.state_path.write_text('{"state":"PARTIALLY_STOPPED"}', encoding="utf-8")
    lifecycle.state_path.chmod(0o600)
    with pytest.raises(LifecycleError, match="state remains"):
        lifecycle._create_profile()


def test_stopped_orphan_project_blocks_new_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(command, 0, "trainer-h03-orphan-postgres-1\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="orphan"):
        lifecycle._project_containers()


def test_compose_commands_use_one_exact_isolated_file_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    command = lifecycle._compose_command(["docker", "compose", "up", "-d", "api"])
    profile = lifecycle._profile()
    assert profile is not None
    assert command[:4] == ["docker", "compose", "-p", profile["project"]]
    assert command.count("-f") == 2
    assert str(tmp_path / "compose.yaml") in command
    assert str(tmp_path / "compose.h03-synthetic.yaml") in command
    assert command[-3:] == ["up", "-d", "api"]
    assert "personal-ai-trainer" not in command


def test_exact_candidate_guard_rejects_different_image_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert command[-1] == CANDIDATE_TAG
        return subprocess.CompletedProcess(command, 0, "sha256:wrong\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="exact candidate"):
        lifecycle._verify_candidate()
    assert CANDIDATE_ID != "sha256:wrong"


def test_ambient_values_cannot_override_candidate_or_synthetic_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    profile = lifecycle._profile()
    assert profile is not None
    captured: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(
        _self: LifecycleManager,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "context", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, f"unix://{Path.home() / '.docker' / 'run' / 'docker.sock'}\n", ""
            )
        captured.append((command, env))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(LifecycleManager, "_run", fake_run)
    lifecycle._run(
        ["docker", "compose", "up", "-d", "api"],
        env={"TRAINER_H03_IMAGE": "wrong", "TRAINER_API_TOKEN": "wrong"},
    )
    command, environment = captured[0]
    assert environment is not None
    assert environment["TRAINER_H03_IMAGE"] == CANDIDATE_ID
    assert environment["TRAINER_API_TOKEN"] == profile["TRAINER_API_TOKEN"]
    assert command[-6:] == ["up", "--pull", "never", "--no-build", "-d", "api"]
    assert profile["TRAINER_API_TOKEN"] not in " ".join(command)


def test_volume_with_wrong_project_label_is_never_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert "rm" not in command
        profile = lifecycle._profile()
        assert profile is not None
        if command[2:3] == ["ls"]:
            return subprocess.CompletedProcess(
                command, 0, f"{profile['project']}_trainer_postgres_data\n", ""
            )
        return subprocess.CompletedProcess(
            command, 0, '{"com.docker.compose.project":"foreign"}\n', ""
        )

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="resource ownership"):
        lifecycle._validate_resources()


def test_unknown_resource_inventory_blocks_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)
    monkeypatch.setattr(
        lifecycle,
        "_run",
        lambda command, **_: subprocess.CompletedProcess(command, 1, "", "permission denied"),
    )
    with pytest.raises(LifecycleError, match="inventory"):
        lifecycle._validate_resources()


def test_copied_primary_mcp_metadata_cannot_signal_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: (42,))
    lifecycle.runtime_dir.mkdir(parents=True)
    lifecycle.runtime_dir.chmod(0o700)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    lifecycle.mcp_path.write_text(
        json.dumps({"pid": 42, "started_signature": "old", "project_root": str(tmp_path)}),
        encoding="utf-8",
    )
    lifecycle.mcp_path.chmod(0o600)
    monkeypatch.setattr(lifecycle, "_process_signature", lambda _: ("trainer-mcp", "old"))
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: (42,))
    monkeypatch.setattr(
        "trainer.lifecycle.system.os.kill",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not signal copied PID")),
    )
    assert lifecycle._stop_owned_mcp() == ["MCP metadata exists but ownership cannot be proven"]


def test_default_lifecycle_refuses_active_synthetic_profile_without_main_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# test\n", encoding="utf-8")
    profile = tmp_path / "tmp" / "h03-synthetic" / "profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}", encoding="utf-8")
    primary = LifecycleManager(tmp_path)
    snapshot = ProbeSnapshot(
        docker_engine=False,
        docker_desktop_stopped=True,
        project_containers=(),
        mcp_owned=False,
        mcp_listener=False,
        api_listener=False,
        api_ready=False,
        runtime_files=(),
        volume_exists=None,
    )
    monkeypatch.setattr(primary, "snapshot", lambda: snapshot)
    monkeypatch.setattr(
        primary,
        "_local_environment",
        lambda: (_ for _ in ()).throw(AssertionError("must not read main .env")),
    )
    assert primary.start().state == "PARTIALLY_STOPPED"
    assert primary.stop().state == "PARTIALLY_STOPPED"


def test_corrupt_transition_state_blocks_start_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    lifecycle.state_path.write_text("not-json", encoding="utf-8")
    lifecycle.state_path.chmod(0o600)
    monkeypatch.setattr(
        LifecycleManager,
        "_ensure_docker",
        lambda _: (_ for _ in ()).throw(AssertionError("must not start Docker")),
    )
    with pytest.raises(LifecycleError, match="transition state"):
        lifecycle.start()


@pytest.mark.parametrize("payload", ["[]", '{"state":[]}', '{"state":"STARTING","errors":{}}'])
def test_malformed_state_shapes_are_partial_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    lifecycle.state_path.write_text(payload, encoding="utf-8")
    lifecycle.state_path.chmod(0o600)
    assert lifecycle._operation_state() == "PARTIALLY_STOPPED"


def test_hostile_runtime_parent_blocks_profile_creation(tmp_path: Path) -> None:
    lifecycle = manager(tmp_path)
    parent = tmp_path / "tmp"
    parent.mkdir()
    parent.chmod(0o777)
    with pytest.raises(LifecycleError, match="parent"):
        lifecycle._create_profile()


def test_group_writable_project_root_blocks_profile_creation(tmp_path: Path) -> None:
    lifecycle = manager(tmp_path)
    original_mode = tmp_path.stat().st_mode & 0o777
    tmp_path.chmod(original_mode | 0o020)
    try:
        with pytest.raises(LifecycleError, match="project root"):
            lifecycle._create_profile()
    finally:
        tmp_path.chmod(original_mode)


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_lock_rejects_symlink_parent_without_external_write(tmp_path: Path, operation: str) -> None:
    lifecycle = manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "tmp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LifecycleError, match="lock parent symlink"):
        getattr(lifecycle, operation)()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_lock_rejects_writable_parent_without_file_creation(tmp_path: Path, operation: str) -> None:
    lifecycle = manager(tmp_path)
    parent = tmp_path / "tmp"
    parent.mkdir()
    parent.chmod(0o777)
    try:
        with pytest.raises(LifecycleError, match="lock parent is unsafe"):
            getattr(lifecycle, operation)()
        assert list(parent.iterdir()) == []
    finally:
        parent.chmod(0o700)


def test_ready_status_degrades_on_wrong_running_image_or_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    ready = LifecycleReport.create(
        "READY",
        ProbeSnapshot(
            docker_engine=True,
            docker_desktop_stopped=False,
            project_containers=("api", "postgres"),
            mcp_owned=True,
            mcp_listener=True,
            api_listener=True,
            api_ready=True,
            runtime_files=(),
            volume_exists=True,
        ),
        "ready",
    )
    monkeypatch.setattr(LifecycleManager, "status", lambda _: ready)
    monkeypatch.setattr(lifecycle, "_validate_ownership", lambda: None)
    monkeypatch.setattr(lifecycle, "_validate_ports_ownership", lambda: None)
    monkeypatch.setattr(lifecycle, "_validate_resources", lambda **_: None)
    monkeypatch.setattr(lifecycle, "_verify_candidate", lambda: None)
    monkeypatch.setattr(
        lifecycle,
        "_verify_running_api",
        lambda: (_ for _ in ()).throw(LifecycleError("wrong image")),
    )
    assert lifecycle.status().state == "DEGRADED"
    monkeypatch.setattr(lifecycle, "_verify_running_api", lambda: None)
    monkeypatch.setattr(
        lifecycle,
        "_validate_resources",
        lambda **_: (_ for _ in ()).throw(LifecycleError("wrong labels")),
    )
    assert lifecycle.status().state == "DEGRADED"


def test_stop_cleanup_and_start_share_one_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopping = manager(tmp_path)
    starting = manager(tmp_path)
    entered_stop = threading.Event()
    finish_stop = threading.Event()
    entered_start = threading.Event()

    def fake_stop(*, full: bool) -> LifecycleReport:
        assert not full
        entered_stop.set()
        assert finish_stop.wait(2)
        return LifecycleReport.create("OFF", _empty_snapshot(), "off")

    def fake_start() -> LifecycleReport:
        entered_start.set()
        return LifecycleReport.create("READY", _empty_snapshot(), "ready")

    monkeypatch.setattr(stopping, "_stop_synthetic_locked", fake_stop)
    monkeypatch.setattr(starting, "_start_synthetic_locked", fake_start)
    stop_thread = threading.Thread(target=stopping.stop)
    start_thread = threading.Thread(target=starting.start)
    stop_thread.start()
    assert entered_stop.wait(2)
    start_thread.start()
    assert not entered_start.wait(0.05)
    finish_stop.set()
    stop_thread.join(2)
    start_thread.join(2)
    assert entered_start.is_set()


def _process_lock_worker(root: str, start: object, active: object, peak: object) -> None:
    lifecycle = LifecycleManager(Path(root))
    start.wait(2)
    with lifecycle._lock():
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        time.sleep(0.05)
        with active.get_lock():
            active.value -= 1


def test_shared_lock_has_one_owner_across_three_processes(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# test\n", encoding="utf-8")
    context = multiprocessing.get_context("fork")
    start = context.Event()
    active = context.Value("i", 0)
    peak = context.Value("i", 0)
    processes = [
        context.Process(target=_process_lock_worker, args=(str(tmp_path), start, active, peak))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(3)
        assert process.exitcode == 0
    assert peak.value == 1
    assert (tmp_path / "tmp" / "trainer-lifecycle.lock").exists()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (subprocess.CompletedProcess([], 1, "", ""), ()),
        (subprocess.CompletedProcess([], 2, "", "permission denied"), None),
    ],
)
def test_port_probe_distinguishes_empty_from_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str],
    expected: tuple[int, ...] | None,
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_run", lambda *_args, **_kwargs: result)
    if expected is None:
        with pytest.raises(LifecycleError, match="inventory failed"):
            lifecycle._port_pids(8765)
    else:
        assert lifecycle._port_pids(8765) == expected


def test_port_probe_missing_executable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(
        lifecycle, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(LifecycleError, match="unavailable"):
        lifecycle._port_pids(8765)


def test_port_probe_timeout_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(
        lifecycle,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("lsof", 5)),
    )
    with pytest.raises(LifecycleError, match="unavailable"):
        lifecycle._port_pids(8765)


def test_postgres_wrong_runtime_image_blocks_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "one-postgres-id\n", "")
        return subprocess.CompletedProcess(command, 0, "sha256:wrong\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="PostgreSQL image ID mismatch"):
        lifecycle._validate_postgres_runtime()


def test_running_api_root_user_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "one-api-id\n", "")
        if "{{.Config.User}}" in command:
            return subprocess.CompletedProcess(command, 0, "root\n", "")
        return subprocess.CompletedProcess(command, 0, CANDIDATE_ID + "\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="non-root"):
        lifecycle._verify_running_api()


def test_postgres_tag_change_rejected_before_compose_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    monkeypatch.setattr(lifecycle, "_postgres_image_id_from_tag", lambda: "sha256:" + "b" * 64)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert command[:3] != ["docker", "compose", "config"]
        if "{{.Config.User}}" in command:
            return subprocess.CompletedProcess(command, 0, "trainer\n", "")
        return subprocess.CompletedProcess(command, 0, CANDIDATE_ID + "\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="PostgreSQL tag"):
        lifecycle._verify_candidate()


@pytest.mark.parametrize(
    "unsafe",
    [
        "restart",
        "read_only",
        "bind_mount",
        "0:0",
        "0:1000",
        "root:root",
        "root:1000",
        "000:0",
        "1001:1001",
        "internal_loopback",
        "external_postgres_network",
    ],
)
def test_preflight_rejects_unsafe_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    profile = lifecycle._profile()
    assert profile is not None
    config = {
        "services": {
            "api": {
                "image": CANDIDATE_ID,
                "ports": [
                    {
                        "mode": "ingress",
                        "host_ip": "127.0.0.1",
                        "target": 8765,
                        "published": "8765",
                        "protocol": "tcp",
                    }
                ],
                "networks": {"trainer_internal": None, "trainer_loopback": None},
                "cpus": 1.0,
                "mem_limit": 384 * 1024 * 1024,
                "pids_limit": 128,
                "restart": "no",
                "read_only": True,
                "security_opt": ["no-new-privileges:true"],
            },
            "postgres": {
                "image": profile["postgres_image_id"],
                "ports": [],
                "networks": {"trainer_internal": None},
                "volumes": [
                    {
                        "type": "volume",
                        "source": "trainer_postgres_data",
                        "target": "/var/lib/postgresql/data",
                    }
                ],
                "cpus": 1.5,
                "mem_limit": 768 * 1024 * 1024,
                "pids_limit": 256,
                "restart": "no",
            },
        },
        "networks": {
            "trainer_internal": {"internal": True},
            "trainer_loopback": {"internal": False},
        },
        "volumes": {"trainer_postgres_data": {}},
    }
    if unsafe == "restart":
        config["services"]["api"]["restart"] = "always"
    elif unsafe == "read_only":
        config["services"]["api"]["read_only"] = False
    elif unsafe == "bind_mount":
        config["services"]["api"]["volumes"] = [
            {"type": "bind", "source": str(tmp_path), "target": "/app"}
        ]
    elif unsafe == "internal_loopback":
        config["networks"]["trainer_loopback"]["internal"] = True
    elif unsafe == "external_postgres_network":
        config["networks"]["trainer_internal"]["internal"] = False
    else:
        config["services"]["api"]["user"] = unsafe

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "compose", "config"]:
            output = json.dumps(config)
        elif "{{.Config.User}}" in command:
            output = "trainer"
        else:
            output = CANDIDATE_ID
        return subprocess.CompletedProcess(command, 0, output + "\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError):
        lifecycle._verify_candidate()
    if unsafe in {"0:0", "0:1000", "root:root", "root:1000", "000:0"}:
        ready = LifecycleReport.create(
            "READY",
            ProbeSnapshot(
                docker_engine=True,
                docker_desktop_stopped=False,
                project_containers=("api", "postgres"),
                mcp_owned=True,
                mcp_listener=True,
                api_listener=True,
                api_ready=True,
                runtime_files=(),
                volume_exists=True,
            ),
            "ready",
        )
        monkeypatch.setattr(LifecycleManager, "status", lambda _: ready)
        monkeypatch.setattr(lifecycle, "_validate_ownership", lambda: None)
        monkeypatch.setattr(lifecycle, "_validate_ports_ownership", lambda: None)
        monkeypatch.setattr(lifecycle, "_validate_resources", lambda **_: None)
        assert lifecycle.status().state == "DEGRADED"


def test_ready_status_rejects_missing_expected_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    ready = LifecycleReport.create(
        "READY",
        ProbeSnapshot(
            docker_engine=True,
            docker_desktop_stopped=False,
            project_containers=("api", "postgres"),
            mcp_owned=True,
            mcp_listener=True,
            api_listener=True,
            api_ready=True,
            runtime_files=(),
            volume_exists=True,
        ),
        "ready",
    )
    monkeypatch.setattr(LifecycleManager, "status", lambda _: ready)
    monkeypatch.setattr(lifecycle, "_validate_ownership", lambda: None)
    monkeypatch.setattr(lifecycle, "_validate_ports_ownership", lambda: None)
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)
    monkeypatch.setattr(
        lifecycle,
        "_run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
    )
    assert lifecycle.status().state == "DEGRADED"


def test_ready_status_rejects_wrong_runtime_network_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    ready = LifecycleReport.create(
        "READY",
        ProbeSnapshot(
            docker_engine=True,
            docker_desktop_stopped=False,
            project_containers=("api", "postgres"),
            mcp_owned=True,
            mcp_listener=True,
            api_listener=True,
            api_ready=True,
            runtime_files=(),
            volume_exists=True,
        ),
        "ready",
    )
    monkeypatch.setattr(LifecycleManager, "status", lambda _: ready)
    monkeypatch.setattr(lifecycle, "_validate_ownership", lambda: None)
    monkeypatch.setattr(lifecycle, "_validate_ports_ownership", lambda: None)
    monkeypatch.setattr(lifecycle, "_validate_resources", lambda **_: None)
    monkeypatch.setattr(lifecycle, "_verify_candidate", lambda: None)
    monkeypatch.setattr(lifecycle, "_verify_running_api", lambda: None)
    monkeypatch.setattr(lifecycle, "_validate_postgres_runtime", lambda: None)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(command, 0, "one-api-id\n", "")
        if "{{json .NetworkSettings.Networks}}" in command:
            return subprocess.CompletedProcess(command, 0, "{}\n", "")
        return subprocess.CompletedProcess(command, 0, "[]\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    assert lifecycle.status().state == "DEGRADED"


@pytest.mark.parametrize("mounts_json", ["null", "{}", '"unexpected"'])
def test_api_runtime_mounts_must_be_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mounts_json: str
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    profile = lifecycle._profile()
    assert profile is not None

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "ps"]:
            output = "one-api-id"
        elif "{{json .NetworkSettings.Networks}}" in command:
            output = json.dumps(
                {
                    f"{profile['project']}_trainer_internal": {},
                    f"{profile['project']}_trainer_loopback": {},
                }
            )
        else:
            output = mounts_json
        return subprocess.CompletedProcess(command, 0, output + "\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="API runtime mounts"):
        lifecycle._validate_runtime_bindings()


def _empty_snapshot() -> ProbeSnapshot:
    return ProbeSnapshot(
        docker_engine=True,
        docker_desktop_stopped=False,
        project_containers=(),
        mcp_owned=False,
        mcp_listener=False,
        api_listener=False,
        api_ready=False,
        runtime_files=(),
        volume_exists=False,
    )


def test_stop_rejects_unowned_containers_before_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = manager(tmp_path)
    monkeypatch.setattr(lifecycle, "_port_pids", lambda _: ())
    lifecycle._create_profile()
    monkeypatch.setattr(lifecycle, "_docker_engine", lambda: True)
    monkeypatch.setattr(lifecycle, "_project_containers", lambda: ("abc:running:foreign",))

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ["docker", "inspect"]
        return subprocess.CompletedProcess(command, 0, "wrong-config-files\n", "")

    monkeypatch.setattr(lifecycle, "_run", fake_run)
    with pytest.raises(LifecycleError, match="ownership"):
        lifecycle.stop()
