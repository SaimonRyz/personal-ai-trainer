"""Offline tests for the package-wide read-only release inventory."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import preflight_release as preflight


class ReleasePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.package = Path(temporary.name)
        self.app = self.package / "app"
        (self.app / "distribution-draft").mkdir(parents=True)
        self.package_patch = patch.object(preflight, "PACKAGE", self.package)
        self.app_patch = patch.object(preflight, "APP", self.app)
        self.package_patch.start()
        self.app_patch.start()
        self.addCleanup(self.package_patch.stop)
        self.addCleanup(self.app_patch.stop)

    def test_manifest_needs_independent_digest_and_detects_changed_file(self) -> None:
        path = self.app / "README.md"
        path.write_bytes(b"synthetic source")
        manifest = {
            "schema_version": 1,
            "files": {"app/README.md": {
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }},
        }
        manifest_path = self.package / "PACKAGE_MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        trusted_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

        self.assertEqual(preflight.package_integrity(None)["state"], "SELF_CONSISTENT_ONLY")
        self.assertEqual(preflight.package_integrity(trusted_hash)["state"], "VERIFIED")
        self.assertEqual(preflight.package_integrity("f" * 64)["state"], "INVALID")
        (self.app / ".env").write_text("synthetic local settings", encoding="utf-8")
        (self.app / ".venv/bin").mkdir(parents=True)
        (self.app / ".venv/bin/python").symlink_to("/usr/bin/python3")
        (self.app / "wheelhouse/macos-arm64").mkdir(parents=True)
        (self.app / "wheelhouse/macos-arm64/example.whl").write_bytes(b"wheel")
        (self.app / "RELEASE_BUILD.json").write_text("{}", encoding="utf-8")
        (self.app / "tmp/release-lifecycle").mkdir(parents=True)
        (self.app / "tmp/release-lifecycle/state.json").write_text("{}", encoding="utf-8")
        self.assertEqual(preflight.package_integrity(trusted_hash)["state"], "VERIFIED")
        extra = self.app / "evil.py"
        extra.write_text("synthetic unlisted source", encoding="utf-8")
        self.assertEqual(preflight.package_integrity(trusted_hash)["state"], "INVALID")
        extra.unlink()
        (self.app / "wheelhouse/linked.whl").symlink_to("/usr/bin/python3")
        self.assertEqual(preflight.package_integrity(trusted_hash)["state"], "INVALID")
        (self.app / "wheelhouse/linked.whl").unlink()
        path.write_bytes(b"changed")
        self.assertEqual(preflight.package_integrity(trusted_hash)["state"], "INVALID")

    def test_existing_venv_requires_exact_installed_distributions(self) -> None:
        site = self.app / ".venv/lib/python3.12/site-packages"
        site.mkdir(parents=True)
        (self.app / "pyproject.toml").write_text(
            '[project]\nname = "personal-ai-trainer"\nversion = "0.1.0"\n'
        )
        (self.app / "distribution-draft/RELEASE_WHEEL_PLAN.json").write_text(
            json.dumps({"schema_version": 1, "runtime": {
                "macos/arm64": [{"name": "example-lib", "version": "2.0"}],
            }, "build": [{"name": "build-lib", "version": "3.0"}]}),
            encoding="utf-8",
        )

        def installed(name: str, version: str) -> Path:
            info = site / f"{name.replace('-', '_')}-{version}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n",
                encoding="utf-8",
            )
            return info

        self.assertEqual(preflight.venv_package_inventory(self.app / ".venv")["state"],
                         "MISMATCH")
        installed("example-lib", "2.0")
        installed("build-lib", "3.0")
        installed("personal-ai-trainer", "0.1.0")
        self.assertEqual(preflight.venv_package_inventory(self.app / ".venv")["state"],
                         "VERIFIED")
        extra = installed("foreign-lib", "1.0")
        self.assertEqual(preflight.venv_package_inventory(self.app / ".venv")["extra"],
                         ["foreign-lib"])
        (extra / "METADATA").unlink()
        extra.rmdir()
        (site / "example_lib-2.0.dist-info/METADATA").write_text(
            "Metadata-Version: 2.3\nName: example-lib\nVersion: 2.1\n"
        )
        self.assertEqual(preflight.venv_package_inventory(self.app / ".venv")["different"],
                         ["example-lib"])

    def test_venv_interpreter_must_report_its_own_prefix(self) -> None:
        interpreter = self.app / ".venv/bin/python"
        virtualenv = self.app / ".venv"
        with patch.object(preflight, "_command", return_value=str(virtualenv)):
            self.assertTrue(preflight._venv_prefix_matches(interpreter, virtualenv))
        with patch.object(preflight, "_command", return_value=str(self.package / "other")):
            self.assertFalse(preflight._venv_prefix_matches(interpreter, virtualenv))
        with patch.object(preflight, "_command", return_value=None):
            self.assertFalse(preflight._venv_prefix_matches(interpreter, virtualenv))

    def test_user_local_uv_is_detected_without_changing_path(self) -> None:
        user_bin = self.package / ".local/bin"
        user_bin.mkdir(parents=True)
        uv = user_bin / "uv"
        uv.write_bytes(b"synthetic executable")
        uv.chmod(0o700)
        with (
            patch.object(preflight.Path, "home", return_value=self.package),
            patch.object(preflight.shutil, "which", return_value=None),
            patch.object(preflight, "_command", side_effect=lambda argv, **kwargs:
                         "uv 0.12.24 (build metadata)" if argv == [str(uv), "-V"] else None),
        ):
            self.assertEqual(preflight.environment_inventory()["uv"], "uv 0.12.24")
        uv.unlink()
        uv.symlink_to("/usr/bin/false")
        with (
            patch.object(preflight.Path, "home", return_value=self.package),
            patch.object(preflight.shutil, "which", return_value=None),
            patch.object(preflight, "_command", return_value=None),
        ):
            self.assertIsNone(preflight.environment_inventory()["uv"])

    def test_docker_client_is_reported_without_engine_access(self) -> None:
        docker = "/usr/local/bin/docker"

        def command(argv: list[str], **kwargs: object) -> str | None:
            if argv == [docker, "--version"]:
                return "Docker version 29.7.2, build a7dcaa6"
            if argv == [docker, "compose", "version", "--short"]:
                return "5.4.0"
            return None

        with (
            patch.object(preflight.shutil, "which", side_effect=lambda name:
                         docker if name == "docker" else None),
            patch.object(preflight, "_command", side_effect=command),
        ):
            report = preflight.environment_inventory()
        self.assertEqual(report["docker_client"], "29.7.2")
        self.assertIsNone(report["docker_engine"])
        self.assertFalse(report["local_docker"])

    def test_missing_wheels_are_reported_as_one_exact_list_without_download(self) -> None:
        contents = b"synthetic wheel"
        artifact = {
            "name": "synthetic", "version": "1.0",
            "filename": "synthetic-1.0-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/synthetic-1.0-py3-none-any.whl",
            "size": len(contents),
            "hash": "sha256:" + hashlib.sha256(contents).hexdigest(),
        }
        plan = {"schema_version": 1, "uv_lock_sha256": "a" * 64, "runtime": {
            "macos/arm64": [artifact], "linux/arm64": [artifact],
        }, "build": []}
        pins = {"schema_version": 1, "uv_lock_sha256": "a" * 64, **{
            name: {
                "reference": f"registry.example/{name}@sha256:{'a' * 64}",
                "platform_manifest_digest": "sha256:" + "a" * 64,
                "expected_image_id": "sha256:" + "c" * 64,
                "compressed_layer_bytes": 100,
            }
            for name in ("python_image", "uv_image", "postgres_image")
        }}
        (self.app / "distribution-draft/RELEASE_WHEEL_PLAN.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        (self.app / "distribution-draft/RELEASE_INPUTS.json").write_text(
            json.dumps(pins), encoding="utf-8"
        )

        report = preflight.artifact_inventory(local_docker=False)
        self.assertEqual(report["missing_wheel_count"], 2)
        self.assertEqual(report["missing_wheel_bytes"], 2 * len(contents))
        self.assertEqual(len(report["missing_or_unverified_images"]), 3)
        self.assertEqual(report["docker_image_download_bytes"], 300)
        self.assertEqual(
            report["docker_image_download_bytes_basis"],
            "sum_of_declared_compressed_layers_before_cache_deduplication",
        )
        self.assertTrue(
            all(item["state"] == "NOT_CHECKED" for item in report["missing_or_unverified_images"])
        )
        folder = self.app / "wheelhouse/macos-arm64"
        folder.mkdir(parents=True)
        (folder / artifact["filename"]).write_bytes(contents)
        self.assertEqual(preflight.artifact_inventory(False)["missing_wheel_count"], 1)
        (folder / "unexpected.whl").write_bytes(b"unlisted")
        (self.app / "wheelhouse/other-target").mkdir()
        self.assertEqual(
            preflight.artifact_inventory(False)["unexpected_wheelhouse_entries"],
            ["macos-arm64/unexpected.whl", "other-target"],
        )
        (folder / "unexpected.whl").unlink()
        (self.app / "wheelhouse/other-target").rmdir()
        pins["postgres_image"]["expected_image_id"] = "sha256:" + "b" * 64
        (self.app / "distribution-draft/RELEASE_INPUTS.json").write_text(
            json.dumps(pins), encoding="utf-8"
        )
        inspected = [
            json.dumps({"Os": "linux", "Architecture": "arm64",
                        "RepoDigests": [pins[name]["reference"]], "Id": "sha256:" + "c" * 64})
            for name in ("python_image", "uv_image", "postgres_image")
        ]
        with patch.object(preflight, "_command", side_effect=inspected):
            images = preflight.artifact_inventory(True)
        self.assertEqual(len(images["verified_images"]), 2)
        self.assertEqual(images["docker_image_download_bytes"], 100)
        self.assertEqual(
            images["missing_or_unverified_images"][0]["state"],
            "PRESENT_NEEDS_CONFIG_VERIFICATION",
        )
        (folder / artifact["filename"]).write_bytes(b"incorrect")
        with self.assertRaisesRegex(ValueError, "existing wheel differs"):
            preflight.artifact_inventory(False)

    def test_unpinned_image_size_fails_closed(self) -> None:
        plan = {"schema_version": 1, "uv_lock_sha256": "a" * 64,
                "runtime": {"macos/arm64": [], "linux/arm64": []}, "build": []}
        pins = {"schema_version": 1, "uv_lock_sha256": "a" * 64, **{
            name: {"reference": f"registry.example/{name}@sha256:{'a' * 64}",
                   "platform_manifest_digest": "sha256:" + "a" * 64,
                   "expected_image_id": "sha256:" + "b" * 64,
                   "compressed_layer_bytes": 100}
            for name in ("python_image", "uv_image", "postgres_image")
        }}
        pins["uv_image"]["compressed_layer_bytes"] = 0
        (self.app / "distribution-draft/RELEASE_WHEEL_PLAN.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        (self.app / "distribution-draft/RELEASE_INPUTS.json").write_text(
            json.dumps(pins), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "release image metadata is invalid"):
            preflight.artifact_inventory(False)

    def test_linked_wheelhouse_is_rejected(self) -> None:
        outside = self.package / "outside"
        outside.mkdir()
        (self.app / "wheelhouse").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "linked wheelhouse"):
            preflight.artifact_inventory(False)

    def test_port_inventory_does_not_treat_failed_check_as_free(self) -> None:
        with patch.object(preflight.subprocess, "run") as run:
            run.return_value = CompletedProcess([], 0, stdout="123\n", stderr="")
            self.assertEqual(
                preflight._port_state("/usr/sbin/lsof", 8767),
                "OCCUPIED_REQUIRES_OWNERSHIP_REVIEW",
            )
            run.return_value = CompletedProcess([], 1, stdout="", stderr="")
            self.assertEqual(preflight._port_state("/usr/sbin/lsof", 8767), "FREE")
            run.return_value = CompletedProcess([], 1, stdout="", stderr="permission denied")
            self.assertEqual(preflight._port_state("/usr/sbin/lsof", 8767), "NOT_CHECKED")

    def test_existing_volume_is_reported_without_reading_database(self) -> None:
        volume = {
            "Name": "personal-ai-trainer-alpha_trainer_postgres_data",
            "Labels": {"com.docker.compose.project": "personal-ai-trainer-alpha",
                       "com.docker.compose.volume": "trainer_postgres_data"},
        }
        with (
            patch.object(preflight.shutil, "which", return_value="/usr/local/bin/docker"),
            patch.object(preflight, "_port_state", return_value="FREE"),
            patch.object(preflight, "_command", side_effect=[
                volume["Name"], json.dumps(volume)
            ]) as command,
        ):
            report = preflight.local_state_inventory(True)
        self.assertEqual(
            report["volume_state"], "PRESENT_EXPECTED_LABELS_REQUIRES_OWNERSHIP_REVIEW"
        )
        self.assertEqual(report["loopback_ports"], {"8767": "FREE", "8768": "FREE"})
        self.assertEqual(command.call_args.args[0][:3], [
            "/usr/local/bin/docker", "volume", "inspect"
        ])

    def test_new_installation_reports_absent_volume_without_guessing(self) -> None:
        with (
            patch.object(preflight.shutil, "which", return_value="/usr/local/bin/docker"),
            patch.object(preflight, "_port_state", return_value="FREE"),
            patch.object(preflight, "_command", return_value=""),
        ):
            report = preflight.local_state_inventory(True)
        self.assertEqual(report["volume_state"], "ABSENT")

    def test_unexpected_wheelhouse_entry_blocks_single_preflight_report(self) -> None:
        environment = {
            "platform": {"supported": True}, "python_3_12": "Python 3.12.14",
            "uv": "uv 0.12.24", "docker_engine": "29.0", "docker_compose": "2.0",
            "local_docker": True, "codex_present": True, "venv": "PRESENT_3_12_14",
            "env_file": "PRESENT_0600",
            "venv_packages": {"state": "VERIFIED"},
        }
        local_state = {"volume_state": "ABSENT", "loopback_ports": {
            "8767": "FREE", "8768": "FREE",
        }}
        artifacts = {"missing_wheels": [], "missing_or_unverified_images": [],
                     "unexpected_wheelhouse_entries": ["macos-arm64/unexpected.whl"]}
        output = io.StringIO()
        with (
            patch.object(preflight.sys, "argv", ["preflight_release.py"]),
            patch.object(preflight, "package_integrity", return_value={"state": "VERIFIED"}),
            patch.object(preflight, "environment_inventory", return_value=environment),
            patch.object(preflight, "local_state_inventory", return_value=local_state),
            patch.object(preflight, "artifact_inventory", return_value=artifacts),
            redirect_stdout(output),
        ):
            preflight.main()
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "NOT_READY")
        self.assertEqual(report["blockers"], ["unexpected_wheelhouse_entries_require_review"])

    def test_existing_venv_package_mismatch_blocks_preflight(self) -> None:
        environment = {
            "platform": {"supported": True}, "python_3_12": "Python 3.12.14",
            "uv": "uv 0.12.24", "docker_engine": "29.0", "docker_compose": "2.0",
            "local_docker": True, "codex_present": True, "venv": "PRESENT_3_12_14",
            "venv_packages": {"state": "MISMATCH", "missing": ["example-lib"],
                              "different": [], "extra": []},
            "env_file": "PRESENT_0600",
        }
        local_state = {"volume_state": "ABSENT", "loopback_ports": {
            "8767": "FREE", "8768": "FREE",
        }}
        artifacts = {"missing_wheels": [], "missing_or_unverified_images": [],
                     "unexpected_wheelhouse_entries": []}
        output = io.StringIO()
        with (
            patch.object(preflight.sys, "argv", ["preflight_release.py"]),
            patch.object(preflight, "package_integrity", return_value={"state": "VERIFIED"}),
            patch.object(preflight, "environment_inventory", return_value=environment),
            patch.object(preflight, "local_state_inventory", return_value=local_state),
            patch.object(preflight, "artifact_inventory", return_value=artifacts),
            redirect_stdout(output),
        ):
            preflight.main()
        report = json.loads(output.getvalue())
        self.assertEqual(report["blockers"], ["local_venv_packages_mismatch_or_unverified"])


if __name__ == "__main__":
    unittest.main()
