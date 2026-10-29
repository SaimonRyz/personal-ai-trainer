"""Offline checks that release build refuses incomplete or altered wheel inputs."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_release

from trainer.lifecycle.system import LifecycleError


class ReleaseWheelhouseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "wheelhouse/linux-arm64"
        self.folder.mkdir(parents=True)
        self.wheel = b"synthetic-wheel-for-metadata-check"
        self.artifact = {
            "name": "test-wheel", "version": "1.0",
            "filename": "test_wheel-1.0-py3-none-any.whl",
            "size": len(self.wheel),
            "hash": "sha256:" + hashlib.sha256(self.wheel).hexdigest(),
        }
        self.plan = {"runtime": {"linux/arm64": [self.artifact]}, "build": []}

    def test_complete_wheelhouse_produces_exact_requirements(self) -> None:
        (self.folder / self.artifact["filename"]).write_bytes(self.wheel)
        with patch.object(build_release, "ROOT", self.root), patch.object(
            build_release, "plan_inputs", return_value=self.plan
        ):
            self.assertEqual(build_release.verify_wheelhouse(), self.plan)
            expected = f"test-wheel==1.0 --hash={self.artifact['hash']}\n"
            self.assertEqual((self.folder / "runtime-requirements.txt").read_text(), expected)
            self.assertEqual(build_release.verify_wheelhouse(), self.plan)

    def test_missing_changed_and_extra_wheels_fail_closed(self) -> None:
        with patch.object(build_release, "ROOT", self.root), patch.object(
            build_release, "plan_inputs", return_value=self.plan
        ):
            with self.assertRaisesRegex(LifecycleError, "missing="):
                build_release.verify_wheelhouse()
            wheel_path = self.folder / self.artifact["filename"]
            wheel_path.write_bytes(self.wheel + b"changed")
            with self.assertRaisesRegex(LifecycleError, "integrity check"):
                build_release.verify_wheelhouse()
            wheel_path.write_bytes(self.wheel)
            (self.folder / "unexpected.whl").write_bytes(b"extra")
            with self.assertRaisesRegex(LifecycleError, "unexpected="):
                build_release.verify_wheelhouse()

    def test_linked_wheel_is_rejected(self) -> None:
        target = self.root / "outside.whl"
        target.write_bytes(self.wheel)
        (self.folder / self.artifact["filename"]).symlink_to(target)
        with patch.object(build_release, "ROOT", self.root), patch.object(
            build_release, "plan_inputs", return_value=self.plan
        ), self.assertRaisesRegex(LifecycleError, "integrity check"):
            build_release.verify_wheelhouse()

    def test_linked_wheelhouse_parent_is_rejected(self) -> None:
        (self.folder / self.artifact["filename"]).write_bytes(self.wheel)
        linked_root = self.root / "linked"
        linked_root.mkdir()
        (linked_root / "wheelhouse").symlink_to(self.root / "wheelhouse")
        with patch.object(build_release, "ROOT", linked_root), patch.object(
            build_release, "plan_inputs", return_value=self.plan
        ), self.assertRaisesRegex(LifecycleError, "wheelhouse is missing"):
            build_release.verify_wheelhouse()


if __name__ == "__main__":
    unittest.main()
