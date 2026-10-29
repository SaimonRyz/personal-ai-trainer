from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trainer.lifecycle import cli
from trainer.lifecycle.alpha import AlphaWorkspaceLifecycleManager
from trainer.lifecycle.system import LifecycleError


def test_alpha_executable_binds_own_root_even_from_dev_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    (alpha / "alpha-workspace.json").write_text("{}", encoding="utf-8")
    dev = tmp_path / "dev"
    dev.mkdir()
    selected: list[Path] = []

    class StubManager:
        def __init__(self, root: Path) -> None:
            selected.append(root)

        def status(self) -> Any:
            return SimpleNamespace(state="OFF", errors=(), to_dict=lambda: {"state": "OFF"})

    monkeypatch.setattr(cli.sys, "prefix", str(alpha / ".venv"))
    monkeypatch.setattr(cli, "find_project_root", lambda: dev)
    monkeypatch.setattr(cli, "AlphaWorkspaceLifecycleManager", StubManager)
    monkeypatch.setattr(cli, "remove_empty_runtime_parent", lambda manager: None)
    monkeypatch.setattr(sys, "argv", ["trainer", "status"])
    cli.main()
    assert selected == [alpha]


def test_dev_executable_refuses_alpha_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    (alpha / "alpha-workspace.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli.sys, "prefix", str(tmp_path / "dev" / ".venv"))
    monkeypatch.setattr(cli, "find_project_root", lambda: alpha)
    monkeypatch.setattr(sys, "argv", ["trainer", "status"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "context,endpoint,allowed",
    [
        ("desktop-linux", "unix:///Users/test/.docker/run/docker.sock", True),
        ("remote", "ssh://example.invalid", False),
        ("desktop-linux", "tcp://127.0.0.1:2375", False),
    ],
)
def test_alpha_docker_endpoint_is_local_desktop_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    context: str,
    endpoint: str,
    allowed: bool,
) -> None:
    manager = AlphaWorkspaceLifecycleManager(tmp_path)
    monkeypatch.setattr(
        "trainer.lifecycle.alpha.pwd.getpwuid", lambda uid: SimpleNamespace(pw_dir="/Users/test")
    )
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = context if command[2] == "show" else endpoint
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(manager, "_run", run)
    if allowed:
        manager._validate_docker_endpoint()
    else:
        with pytest.raises(LifecycleError, match="local Docker Desktop socket"):
            manager._validate_docker_endpoint()


def test_alpha_docker_host_override_is_rejected_before_context_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = AlphaWorkspaceLifecycleManager(tmp_path)
    monkeypatch.setenv("DOCKER_HOST", "ssh://example.invalid")
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *args, **kwargs: pytest.fail("Docker command must not run with remote override"),
    )
    with pytest.raises(LifecycleError, match="override is forbidden"):
        manager._validate_docker_endpoint()


@pytest.mark.parametrize("wrong_network", [False, True])
def test_alpha_container_networks_match_project_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong_network: bool
) -> None:
    manager = AlphaWorkspaceLifecycleManager(tmp_path)
    project = manager.compose_project
    api_image, pg_image = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    monkeypatch.setattr(manager, "_docker_engine", lambda: True)
    monkeypatch.setattr(
        manager,
        "_local_environment",
        lambda: {"TRAINER_ALPHA_API_IMAGE": api_image, "TRAINER_ALPHA_POSTGRES_IMAGE": pg_image},
    )

    def container(service: str) -> dict[str, Any]:
        networks = (
            {f"{project}_trainer_internal": {}, f"{project}_trainer_loopback": {}}
            if service == "api"
            else {f"{project}_trainer_internal": {}}
        )
        if wrong_network and service == "api":
            networks["foreign_bridge"] = {}
        return {
            "Config": {
                "Labels": {
                    "com.docker.compose.service": service,
                    "com.docker.compose.project.config_files": str(tmp_path / "compose.yaml"),
                }
            },
            "Image": api_image if service == "api" else pg_image,
            "HostConfig": {
                "PortBindings": (
                    {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8767"}]}
                    if service == "api"
                    else {}
                )
            },
            "Mounts": []
            if service == "api"
            else [
                {
                    "Name": f"{project}_trainer_postgres_data",
                    "Destination": "/var/lib/postgresql/data",
                }
            ],
            "NetworkSettings": {"Networks": networks},
        }

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "ps"]:
            value = "api-id\npostgres-id\n"
        elif command[:2] == ["docker", "inspect"]:
            value = json.dumps([container("api" if command[2] == "api-id" else "postgres")])
        elif command[2:3] == ["ls"]:
            kind = command[1]
            names = (
                [f"{project}_trainer_postgres_data"]
                if kind == "volume"
                else [f"{project}_trainer_internal", f"{project}_trainer_loopback"]
            )
            value = "\n".join(names)
        else:
            name = command[-1]
            item = name.removeprefix(f"{project}_")
            label = (
                "com.docker.compose.volume"
                if command[1] == "volume"
                else "com.docker.compose.network"
            )
            value = json.dumps({"com.docker.compose.project": project, label: item})
        return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")

    monkeypatch.setattr(manager, "_run", run)
    if wrong_network:
        with pytest.raises(LifecycleError, match="network attachment"):
            manager._validate_ownership()
    else:
        assert manager._validate_ownership() == {"api", "postgres"}


def test_package_manifest_rejects_extra_build_input(tmp_path: Path) -> None:
    package = tmp_path / "package"
    for tree in ("src/trainer", "mcp/trainer_mcp", "migrations", "scripts"):
        (package / tree).mkdir(parents=True)
    source = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "alpha_workspace_templates"
        / "check_package_manifest.py"
    )
    checker = package / "scripts/check_package_manifest.py"
    checker.write_bytes(source.read_bytes())
    files = {"scripts/check_package_manifest.py": hashlib.sha256(checker.read_bytes()).hexdigest()}
    operational_files: dict[str, str] = {}
    for name in (
        "AGENTS.md",
        "INITIAL_SETUP_INSTRUCTIONS.md",
        ".agents/skills/trainer-launch/SKILL.md",
    ):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture: {name}\n", encoding="utf-8")
        operational_files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = hashlib.sha256(
        json.dumps(
            {"files": files, "operational_files": operational_files},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    (package / "PACKAGE_MANIFEST.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "approved_source_snapshot_sha256": snapshot,
                "files": files,
                "operational_files": operational_files,
            }
        ),
        encoding="utf-8",
    )
    command = [sys.executable, str(checker)]
    assert subprocess.run(command, cwd=package, capture_output=True).returncode == 0
    cache = package / "src/trainer/__pycache__"
    cache.mkdir()
    (cache / "artifact.pyc").write_bytes(b"synthetic cache")
    assert subprocess.run(command, cwd=package, capture_output=True).returncode == 0
    (package / "src/trainer/extra.py").write_text("x = 1\n", encoding="utf-8")
    result = subprocess.run(command, cwd=package, capture_output=True, text=True)
    assert result.returncode != 0
    assert "build input file set" in result.stderr


def test_approved_starter_catalog_is_in_source_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    preview = subprocess.run(
        [sys.executable, str(root / "scripts/package_alpha_workspace.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    files = json.loads(preview.stdout)["files"]
    assert "STARTER_CATALOG.json" in files
    assert "postgres_image.json" in files
    catalog = json.loads(
        (root / "scripts/alpha_workspace_templates/starter_catalog.json").read_text()
    )
    assert catalog["automatic_import"] is False
    assert len(catalog["entries"]) == 10
    postgres_pin = json.loads((root / "scripts/alpha_workspace_templates/postgres_image.json").read_text())
    assert postgres_pin["reference"] == "docker.io/library/postgres:17.11-bookworm"
    assert postgres_pin["platform"] == {"os": "linux", "architecture": "arm64", "variant": "v8"}


def test_package_contains_one_message_launch_skill_without_changing_image_manifest(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    packager = root / "scripts/package_alpha_workspace.py"
    proposed = subprocess.run(
        [sys.executable, str(packager)],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    package_manifest = json.loads(proposed.stdout)
    destination = tmp_path / "alpha"
    subprocess.run(
        [
            sys.executable,
            str(packager),
            "--write",
            "--destination",
            str(destination),
            "--approved-source-snapshot",
            package_manifest["approved_source_snapshot_sha256"],
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )

    skill_path = ".agents/skills/trainer-launch/SKILL.md"
    skill = (destination / skill_path).read_text(encoding="utf-8")
    agents = (destination / "AGENTS.md").read_text(encoding="utf-8")
    setup = (destination / "INITIAL_SETUP_INSTRUCTIONS.md").read_text(encoding="utf-8")
    ignore = (destination / ".gitignore").read_text(encoding="utf-8")
    actual_manifest = json.loads((destination / "PACKAGE_MANIFEST.json").read_text())

    assert skill_path not in actual_manifest["files"]
    assert set(actual_manifest["operational_files"]) == {
        "AGENTS.md",
        "INITIAL_SETUP_INSTRUCTIONS.md",
        skill_path,
    }
    assert "operational_files=3" in subprocess.run(
        [sys.executable, str(destination / "scripts/check_package_manifest.py")],
        cwd=destination,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "trainer-launch" in agents
    assert "один полный preflight" in skill
    assert "Не запрашивай разрешение последовательно по одному пакету" in setup
    assert "!/.agents/**" in ignore
    assert "Не скачивай и не устанавливай компоненты без согласования точного набора" in setup

    (destination / "INITIAL_SETUP_INSTRUCTIONS.md").write_text(
        setup + "\nИзменение без обновления manifest.\n", encoding="utf-8"
    )
    rejected = subprocess.run(
        [sys.executable, str(destination / "scripts/check_package_manifest.py")],
        cwd=destination,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "operational instruction mismatch: INITIAL_SETUP_INSTRUCTIONS.md" in rejected.stderr


def test_alpha_image_build_preserves_existing_proof_before_docker_calls(tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    build_script = Path(__file__).resolve().parents[2] / (
        "scripts/alpha_workspace_templates/build_alpha_image.sh"
    )
    (scripts / "build_alpha_image.sh").write_bytes(build_script.read_bytes())
    (root / "alpha-workspace.json").write_text("{}\n", encoding="utf-8")
    (root / "PACKAGE_MANIFEST.json").write_text("{}\n", encoding="utf-8")
    proof = root / "ALPHA_IMAGE.json"
    proof.write_text('{"image_id":"sha256:preserve-me"}\n', encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_calls = tmp_path / "docker-calls"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\nprintf 'called\\n' >> \"$DOCKER_CALLS_FILE\"\nexit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TRAINER_ALPHA_BASE_TAG": "verified-local-tag",
        "DOCKER_CALLS_FILE": str(docker_calls),
    }

    result = subprocess.run(
        ["/bin/sh", str(scripts / "build_alpha_image.sh")],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "ALPHA_IMAGE.json already exists" in result.stderr
    assert not docker_calls.exists()
    assert proof.read_text(encoding="utf-8") == '{"image_id":"sha256:preserve-me"}\n'


def test_alpha_image_build_pins_and_verifies_linux_arm64() -> None:
    build_script = (
        Path(__file__).resolve().parents[2]
        / "scripts/alpha_workspace_templates/build_alpha_image.sh"
    ).read_text(encoding="utf-8")

    assert "base_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}'" in build_script
    assert "test \"$base_platform\" = 'linux/arm64'" in build_script
    assert "docker build --platform=linux/arm64 --pull=false --network=none" in build_script
    assert "image_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}'" in build_script
    assert "test \"$image_platform\" = 'linux/arm64'" in build_script


def test_alpha_image_proof_writer_refuses_to_replace_proof_created_during_build(
    tmp_path: Path,
) -> None:
    build_script = (
        Path(__file__).resolve().parents[2]
        / "scripts/alpha_workspace_templates/build_alpha_image.sh"
    ).read_text(encoding="utf-8")
    marker = 'python3 - "$image_id" "$expected_source" "$base_id" <<\'PY\'\n'
    start = build_script.index(marker) + len(marker)
    end = build_script.index("\nPY\n", start)
    proof_writer = build_script[start:end]
    proof = tmp_path / "ALPHA_IMAGE.json"
    (tmp_path / "PACKAGE_MANIFEST.json").write_text("{}\n", encoding="utf-8")
    expected = '{"image_id":"sha256:concurrent-proof"}\n'
    proof.write_text(expected, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", proof_writer, "sha256:new", "fingerprint 1", "sha256:base"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "ALPHA_IMAGE.json already exists" in result.stderr
    assert proof.read_text(encoding="utf-8") == expected
    assert not (tmp_path / "ALPHA_IMAGE.json.tmp").exists()
