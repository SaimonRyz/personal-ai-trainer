"""Read-only checks for the allowlisted source package mapping."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import package_candidate
import plan_release_inputs as planner
import preflight_release as preflight


class ReleasePackageMappingTests(unittest.TestCase):
    def test_current_candidate_maps_both_release_entrypoints(self) -> None:
        mapping = package_candidate.source_map()
        self.assertEqual(
            mapping["app/distribution-draft/build_release.py"],
            package_candidate.ROOT / "distribution-draft/build_release.py",
        )
        self.assertEqual(
            mapping["app/distribution-draft/release-workspace.json"],
            package_candidate.ROOT / "distribution-draft/release-workspace.template.json",
        )

    def test_partial_release_profile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "distribution-draft").mkdir()
            (root / "distribution-draft/release-workspace.template.json").write_text("{}")
            with (
                patch.object(package_candidate, "ROOT", root),
                self.assertRaisesRegex(ValueError, "packaged together"),
            ):
                package_candidate.source_map()

    def test_complete_release_entrypoints_are_mapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "distribution-draft").mkdir()
            (root / "src/trainer/lifecycle").mkdir(parents=True)
            for name in (
                *package_candidate.RELEASE_ENTRYPOINTS,
                "src/trainer/lifecycle/release.py",
            ):
                (root / name).write_text("{}")
            with patch.object(package_candidate, "ROOT", root):
                mapping = package_candidate.source_map()
            self.assertEqual(
                mapping["app/distribution-draft/build_release.py"],
                root / "distribution-draft/build_release.py",
            )
            self.assertEqual(
                mapping["app/distribution-draft/release-workspace.json"],
                root / "distribution-draft/release-workspace.template.json",
            )

    def test_generated_split_package_contains_release_profile_without_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "candidate"
            with patch.object(package_candidate, "DEST", destination), patch("builtins.print"):
                package_candidate.main()
            manifest = json.loads((destination / "PACKAGE_MANIFEST.json").read_text())
            files = set(manifest["files"])
            self.assertIn("app/distribution-draft/build_release.py", files)
            self.assertIn("app/distribution-draft/plan_release_inputs.py", files)
            self.assertIn("app/distribution-draft/prepare_release_wheels.py", files)
            self.assertIn("app/distribution-draft/preflight_release.py", files)
            self.assertIn("app/distribution-draft/BUILD_ARTIFACTS.json", files)
            self.assertIn("app/distribution-draft/RELEASE_WHEEL_PLAN.json", files)
            wheel_plan = json.loads(
                (destination / "app/distribution-draft/RELEASE_WHEEL_PLAN.json").read_text()
            )
            self.assertEqual(
                wheel_plan["uv_lock_sha256"],
                json.loads(
                    (destination / "app/distribution-draft/RELEASE_INPUTS.json").read_text()
                )["uv_lock_sha256"],
            )
            packaged_root = destination / "app"
            packaged_recipe = packaged_root / "distribution-draft"
            with (
                patch.object(preflight, "PACKAGE", destination),
                patch.object(preflight, "APP", packaged_root),
            ):
                self.assertEqual(
                    preflight.package_integrity(None)["state"], "SELF_CONSISTENT_ONLY"
                )
                self.assertGreater(preflight.artifact_inventory(False)["missing_wheel_count"], 0)
            with (
                patch.object(planner, "ROOT", packaged_root),
                patch.object(planner, "HERE", packaged_recipe),
            ):
                self.assertEqual(planner.plan(), wheel_plan)
                snapshot = packaged_recipe / "RELEASE_WHEEL_PLAN.json"
                snapshot.write_text('{"schema_version": 1}')
                with self.assertRaisesRegex(ValueError, "packaged wheel plan differs"):
                    planner.plan()
            self.assertIn("app/distribution-draft/release-workspace.json", files)
            self.assertIn("app/src/trainer/lifecycle/release.py", files)
            self.assertIn("trainer-workspace/.codex/config.toml", files)
            self.assertNotIn("app/.env", files)
            self.assertNotIn("app/RELEASE_BUILD.json", files)
            self.assertNotIn("app/AGENT_WORKLOG.md", files)
            self.assertNotIn("trainer-workspace/src/trainer/lifecycle/release.py", files)

    def test_archive_matches_manifest_and_independent_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "candidate"
            with patch.object(package_candidate, "DEST", destination), patch("builtins.print"):
                package_candidate.main()
            manifest_path = destination / "PACKAGE_MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            trusted_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            with (
                patch.object(preflight, "PACKAGE", destination),
                patch.object(preflight, "APP", destination / "app"),
            ):
                self.assertEqual(
                    preflight.package_integrity(trusted_digest)["state"], "VERIFIED"
                )
            archive = destination.with_suffix(".tar.gz")
            with tarfile.open(archive, "r:gz") as stream:
                members = stream.getmembers()
                expected_names = {
                    f"candidate/{name}" for name in manifest["files"]
                } | {"candidate/PACKAGE_MANIFEST.json"}
                self.assertEqual({member.name for member in members}, expected_names)
                self.assertTrue(all(member.isfile() for member in members))
                for member in members:
                    relative = member.name.removeprefix("candidate/")
                    self.assertEqual(
                        stream.extractfile(member).read(),
                        (destination / relative).read_bytes(),
                    )

    def test_same_sources_produce_identical_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archives = []
            for name in ("first", "second"):
                parent = Path(temporary) / name
                parent.mkdir()
                destination = parent / "candidate"
                with patch.object(package_candidate, "DEST", destination), patch("builtins.print"):
                    package_candidate.main()
                archives.append(destination.with_suffix(".tar.gz").read_bytes())
            self.assertEqual(archives[0], archives[1])

    def test_release_scripts_do_not_invalidate_installed_package_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "candidate"
            with patch.object(package_candidate, "DEST", destination), patch("builtins.print"):
                package_candidate.main()
            app = destination / "app"
            manifest = destination / "PACKAGE_MANIFEST.json"
            trusted_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            wheels = subprocess.run(
                [sys.executable, "distribution-draft/prepare_release_wheels.py"],
                cwd=app, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertEqual(wheels.returncode, 0, wheels.stderr)
            builder = subprocess.run(
                [sys.executable, "distribution-draft/build_release.py"],
                cwd=app, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertNotEqual(builder.returncode, 0)
            self.assertNotIn("Traceback", builder.stderr)
            with patch.object(preflight, "PACKAGE", destination), patch.object(
                preflight, "APP", app
            ):
                self.assertEqual(preflight.package_integrity(trusted_digest)["state"], "VERIFIED")

    def test_cli_builds_new_destination_without_overwriting_existing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "fresh-candidate"
            command = [
                sys.executable,
                str(package_candidate.ROOT / "distribution-draft/package_candidate.py"),
                "--destination", str(destination),
            ]
            first = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            archive = destination.with_suffix(".tar.gz")
            original_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            second = subprocess.run(
                command, capture_output=True, text=True, timeout=20, check=False
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), original_digest)
            self.assertIn("candidate_not_runtime_verified", (
                destination / "PACKAGE_MANIFEST.json"
            ).read_text(encoding="utf-8"))
            archive_only = Path(temporary) / "archive-only"
            archive_only.with_suffix(".tar.gz").write_bytes(b"keep existing artifact")
            with self.assertRaisesRegex(ValueError, "destination or archive exists"):
                package_candidate.main(archive_only)
            self.assertFalse(archive_only.exists())
            self.assertEqual(
                archive_only.with_suffix(".tar.gz").read_bytes(), b"keep existing artifact"
            )


if __name__ == "__main__":
    unittest.main()
