from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest

from trainer.lifecycle.alpha import AlphaWorkspaceLifecycleManager
from trainer.lifecycle.system import LifecycleError


TEMPLATE_PIN = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "alpha_workspace_templates"
    / "postgres_image.json"
)


def setup_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    image_metadata: dict[str, Any] | None = None,
    configured_image_id: str | None = None,
) -> tuple[AlphaWorkspaceLifecycleManager, list[list[str]]]:
    pin_path = tmp_path / "postgres_image.json"
    pin_path.write_bytes(TEMPLATE_PIN.read_bytes())
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    calls: list[list[str]] = []
    image_id = pin["image_id"]
    monkeypatch.setattr(
        AlphaWorkspaceLifecycleManager,
        "_local_environment",
        lambda self: {
            "TRAINER_ALPHA_POSTGRES_IMAGE": configured_image_id or image_id,
        },
    )

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = image_metadata or {"Id": image_id, "Os": "linux", "Architecture": "arm64"}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(output), stderr="")

    manager = AlphaWorkspaceLifecycleManager(tmp_path)
    monkeypatch.setattr(manager, "_run", run)
    return manager, calls


def test_postgres_provenance_accepts_exact_pin_and_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, calls = setup_manager(tmp_path, monkeypatch)
    local = manager._local_environment()

    manager._validate_postgres_provenance(local)

    assert calls == [
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            local["TRAINER_ALPHA_POSTGRES_IMAGE"],
        ]
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Id", "sha256:" + "0" * 64),
        ("Os", "darwin"),
        ("Architecture", "amd64"),
    ],
)
def test_postgres_provenance_rejects_wrong_local_image_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    pin = json.loads(TEMPLATE_PIN.read_text(encoding="utf-8"))
    metadata = {
        "Id": pin["image_id"],
        "Os": pin["platform"]["os"],
        "Architecture": pin["platform"]["architecture"],
    }
    metadata[field] = value
    manager, _ = setup_manager(tmp_path, monkeypatch, image_metadata=metadata)

    with pytest.raises(LifecycleError, match="approved ID and platform"):
        manager._validate_postgres_provenance(manager._local_environment())


def test_start_rejects_wrong_postgres_platform_before_compose_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = json.loads(TEMPLATE_PIN.read_text(encoding="utf-8"))
    image_id = pin["image_id"]
    api_image_id = "sha256:" + "1" * 64
    calls: list[list[str]] = []
    manager = AlphaWorkspaceLifecycleManager(tmp_path)
    (tmp_path / "postgres_image.json").write_bytes(TEMPLATE_PIN.read_bytes())

    monkeypatch.setattr(manager, "_validate_workspace", lambda **kwargs: None)
    monkeypatch.setattr(manager, "_foreign_synthetic_profile_exists", lambda: False)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: SimpleNamespace(state="OFF"),
    )
    monkeypatch.setattr(manager, "_write_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_docker_engine", lambda: True)
    monkeypatch.setattr(manager, "_validate_ownership", lambda: {"postgres"})
    monkeypatch.setattr(manager, "_port_pids", lambda port: [])
    monkeypatch.setattr(
        manager,
        "_local_environment",
        lambda: {
            "TRAINER_ALPHA_API_IMAGE": api_image_id,
            "TRAINER_ALPHA_POSTGRES_IMAGE": image_id,
        },
    )

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            requested_id = command[-1]
            if requested_id == api_image_id:
                metadata = {"Id": api_image_id, "Os": "linux", "Architecture": "arm64"}
            else:
                metadata = {"Id": image_id, "Os": "linux", "Architecture": "amd64"}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(metadata), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(
        "trainer.lifecycle.system.LifecycleManager._ensure_docker",
        lambda self: None,
    )
    monkeypatch.setattr(
        "trainer.lifecycle.system.LifecycleManager._prepare_database",
        lambda self: calls.append(["database-prepare"]),
    )
    monkeypatch.setattr(
        "trainer.lifecycle.system.LifecycleManager.snapshot",
        lambda self: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trainer.lifecycle.model.LifecycleReport.create",
        lambda state, probes, message, errors=(): SimpleNamespace(
            state=state, errors=errors, message=message
        ),
    )

    report = manager.start()

    assert report.state == "PARTIALLY_STOPPED"
    assert any("approved ID and platform" in error for error in report.errors)
    assert ["database-prepare"] not in calls
    assert not any(command[:3] == ["docker", "compose", "up"] for command in calls)
    assert not any(command[:3] == ["docker", "compose", "config"] for command in calls)


def test_postgres_provenance_rejects_environment_image_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong_id = "sha256:" + "0" * 64
    manager, calls = setup_manager(tmp_path, monkeypatch, configured_image_id=wrong_id)

    with pytest.raises(LifecycleError, match="configured PostgreSQL image"):
        manager._validate_postgres_provenance(manager._local_environment())

    assert calls == []


@pytest.mark.parametrize("mutation", ["reference", "manifest_digest", "image_id"])
def test_postgres_provenance_rejects_modified_pin_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    manager, calls = setup_manager(tmp_path, monkeypatch)
    pin_path = tmp_path / "postgres_image.json"
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    if mutation == "reference":
        pin["reference"] = "postgres:17.11-bookworm"
    elif mutation == "manifest_digest":
        pin["platform_manifest_digest"] = "sha256:" + "0" * 64
    else:
        pin["image_id"] = "sha256:" + "0" * 64
    pin_path.write_text(json.dumps(pin), encoding="utf-8")

    with pytest.raises(LifecycleError, match="does not match the approved release"):
        manager._validate_postgres_provenance(manager._local_environment())

    assert calls == []


def test_postgres_provenance_fails_closed_when_pin_is_missing_or_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, calls = setup_manager(tmp_path, monkeypatch)
    (tmp_path / "postgres_image.json").unlink()
    with pytest.raises(LifecycleError, match="pin is missing or unsafe"):
        manager._validate_postgres_provenance(manager._local_environment())
    assert calls == []

    (tmp_path / "postgres_image.json").write_text("{invalid", encoding="utf-8")
    with pytest.raises(LifecycleError, match="pin is unreadable"):
        manager._validate_postgres_provenance(manager._local_environment())
    assert calls == []
