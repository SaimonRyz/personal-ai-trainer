"""Offline release lifecycle checks; never contact Docker or user data."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from trainer.lifecycle import cli
from trainer.lifecycle.release import PROOF, RECIPE, ReleaseWorkspaceLifecycleManager
from trainer.lifecycle.system import LifecycleError, LifecycleManager


def test_release_postgres_requires_registry_provenance() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        recipe = root / RECIPE
        recipe.parent.mkdir(parents=True)
        reference = "docker.io/library/postgres:17.11-bookworm@sha256:" + "a" * 64
        image_id = "sha256:" + "b" * 64
        pin = {"reference": reference, "expected_image_id": "sha256:" + "c" * 64}
        recipe.write_text(json.dumps({"postgres_image": pin}))
        (root / PROOF).write_text(json.dumps({"postgres": {
            "reference": reference, "config_digest": pin["expected_image_id"],
            "local_image_id": image_id, "config_bytes_verified": True,
        }}))
        manager = ReleaseWorkspaceLifecycleManager(root)
        image = {"Id": image_id, "Os": "linux", "Architecture": "arm64",
                 "RepoDigests": ["postgres@sha256:" + "a" * 64]}
        with patch.object(manager, "_run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(image)
        )):
            manager._validate_postgres_provenance({"TRAINER_ALPHA_POSTGRES_IMAGE": image_id})
        image["RepoDigests"] = []
        with patch.object(manager, "_run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(image)
        )), pytest.raises(LifecycleError, match="registry provenance"):
            manager._validate_postgres_provenance({"TRAINER_ALPHA_POSTGRES_IMAGE": image_id})


def test_release_start_reaps_only_proven_owned_stale_mcp() -> None:
    with TemporaryDirectory() as directory:
        manager = ReleaseWorkspaceLifecycleManager(Path(directory))
        with (
            patch.object(manager, "_validate_workspace"),
            patch.object(LifecycleManager, "_ensure_docker"),
            patch.object(manager, "_validate_images_and_compose"),
            patch.object(manager, "_validate_ownership"),
            patch.object(manager, "_port_pids", side_effect=[(), (123,), ()]),
            patch.object(manager, "_mcp_owned", return_value=True),
            patch.object(manager, "_stop_owned_mcp", return_value=[]) as stop,
        ):
            manager._ensure_docker()
            stop.assert_called_once_with()


def test_release_start_refuses_foreign_mcp() -> None:
    with TemporaryDirectory() as directory:
        manager = ReleaseWorkspaceLifecycleManager(Path(directory))
        with (
            patch.object(manager, "_validate_workspace"),
            patch.object(LifecycleManager, "_ensure_docker"),
            patch.object(manager, "_validate_images_and_compose"),
            patch.object(manager, "_validate_ownership"),
            patch.object(manager, "_port_pids", side_effect=[(), (123,)]),
            patch.object(manager, "_mcp_owned", return_value=False),
            patch.object(manager, "_stop_owned_mcp") as stop,
            pytest.raises(LifecycleError, match="no proven ownership"),
        ):
            manager._ensure_docker()
            stop.assert_not_called()


def test_configure_image_preserves_comments_and_other_values() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        manager = ReleaseWorkspaceLifecycleManager(root)
        old_id = "sha256:" + "1" * 64
        new_id = "sha256:" + "2" * 64
        postgres_id = "sha256:" + "3" * 64
        env = root / ".env"
        original = (
            "# User-maintained configuration\n"
            f"TRAINER_ALPHA_API_IMAGE={old_id}\n"
            f"TRAINER_ALPHA_POSTGRES_IMAGE={postgres_id}\n"
            "TRAINER_E1RM_BENCH_GRIP_CONDITIONS=[]\n"
        )
        env.write_text(original)
        env.chmod(0o600)
        (root / PROOF).write_text(json.dumps({"image_id": new_id}))
        image = {"Id": new_id, "Os": "linux", "Architecture": "arm64"}
        local = {"TRAINER_ALPHA_API_IMAGE": old_id,
                 "TRAINER_ALPHA_POSTGRES_IMAGE": postgres_id}
        with (
            patch.object(sys, "prefix", str(manager.root / ".venv")),
            patch.object(manager, "_validate_workspace"),
            patch.object(manager, "_docker_engine", return_value=True),
            patch.object(manager, "_project_containers", return_value=()),
            patch.object(manager, "_port_pids", return_value=()),
            patch.object(manager, "_validate_ownership"),
            patch.object(manager, "_local_environment", return_value=local),
            patch.object(manager, "_validate_build_proof"),
            patch.object(manager, "_validate_postgres_provenance"),
            patch.object(manager, "_run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(image)
            )),
        ):
            result = manager.configure_image()
            assert result["changed"] is True
            assert env.read_text() == original.replace(old_id, new_id)
            assert env.stat().st_mode & 0o777 == 0o600
            assert not list(root.glob(".env-update-*"))
            local["TRAINER_ALPHA_API_IMAGE"] = new_id
            result = manager.configure_image()
            assert result["changed"] is False


def test_configure_image_refuses_running_runtime() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        manager = ReleaseWorkspaceLifecycleManager(root)
        env = root / ".env"
        env.write_text("TRAINER_ALPHA_API_IMAGE=unchanged\n")
        env.chmod(0o600)
        with (
            patch.object(sys, "prefix", str(manager.root / ".venv")),
            patch.object(manager, "_validate_workspace"),
            patch.object(manager, "_docker_engine", return_value=True),
            patch.object(manager, "_project_containers", return_value=("api",)),
            pytest.raises(LifecycleError, match="stop the release runtime"),
        ):
            manager.configure_image()
        assert env.read_text() == "TRAINER_ALPHA_API_IMAGE=unchanged\n"


def test_cli_uses_release_only_with_own_venv_and_marker() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        marker = root / "distribution-draft/release-workspace.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({
            "kind": "personal-ai-trainer-release-candidate", "version": 1,
        }))
        with (
            patch.object(sys, "prefix", str(root / ".venv")),
            patch.object(sys, "argv", ["trainer", "status"]),
            patch.object(cli, "ReleaseWorkspaceLifecycleManager") as factory,
            patch.object(cli, "remove_empty_runtime_parent"),
            patch("builtins.print"),
        ):
            report = factory.return_value.status.return_value
            report.state = "OFF"
            report.errors = ()
            report.to_dict.return_value = {"state": "OFF"}
            cli.main()
            factory.assert_called_once_with(root)
        with (
            patch.object(sys, "prefix", str(root / "other-venv")),
            patch.object(sys, "argv", ["trainer", "start"]),
            patch.object(cli, "find_project_root", return_value=root),
            pytest.raises(SystemExit),
        ):
            cli.main()


def test_cli_rejects_missing_release_marker_without_ordinary_fallback() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        recipe = root / RECIPE
        recipe.parent.mkdir(parents=True)
        recipe.write_text("{}")
        (root / "compose.yaml").write_text("name: personal-ai-trainer-alpha\n")
        with (
            patch.object(sys, "prefix", str(root / ".venv")),
            patch.object(sys, "argv", ["trainer", "start"]),
            patch.object(cli, "find_project_root", return_value=root),
            patch.object(cli, "LifecycleManager") as ordinary,
            pytest.raises(SystemExit),
        ):
            cli.main()
        ordinary.assert_not_called()
