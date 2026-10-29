"""Read-only inventory and scoped downloader safety tests; no network access."""

from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prepare_release_wheels as wheels


class FakeOpener:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self, request, timeout):  # type: ignore[no-untyped-def]
        return io.BytesIO(self.data)


class ReleaseWheelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = b"synthetic exact wheel"
        self.artifact = {
            "name": "synthetic", "version": "1.0",
            "filename": "synthetic-1.0-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/example/synthetic-1.0-py3-none-any.whl",
            "size": len(self.data),
            "hash": "sha256:" + hashlib.sha256(self.data).hexdigest(),
        }
        self.expected = {"linux-arm64": {self.artifact["filename"]: self.artifact}}

    def test_inventory_reports_missing_and_accepts_verified_file(self) -> None:
        self.assertEqual(len(wheels.inventory(self.root, self.expected)), 1)
        folder = self.root / "wheelhouse/linux-arm64"
        folder.mkdir(parents=True)
        (folder / self.artifact["filename"]).write_bytes(self.data)
        self.assertEqual(wheels.inventory(self.root, self.expected), [])
        (folder / self.artifact["filename"]).write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "differs"):
            wheels.inventory(self.root, self.expected)

    def test_symlinked_wheelhouse_and_artifact_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "wheelhouse").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "linked wheelhouse"):
            wheels.inventory(self.root, self.expected)

    def test_fetch_checks_size_and_hash_before_publishing(self) -> None:
        target = self.root / "wheelhouse/linux-arm64" / self.artifact["filename"]
        with patch.object(
            wheels.urllib.request, "build_opener", return_value=FakeOpener(self.data)
        ):
            wheels.fetch_exact(self.artifact, target)
        self.assertEqual(target.read_bytes(), self.data)
        self.assertEqual(wheels.inventory(self.root, self.expected), [])
        with (
            self.assertRaisesRegex(ValueError, "appeared"),
            patch.object(wheels.urllib.request, "build_opener", return_value=FakeOpener(self.data)),
        ):
            wheels.fetch_exact(self.artifact, target)

    def test_bad_download_never_publishes_target(self) -> None:
        target = self.root / "wheelhouse/linux-arm64" / self.artifact["filename"]
        with (
            patch.object(wheels.urllib.request, "build_opener", return_value=FakeOpener(b"wrong")),
            self.assertRaisesRegex(ValueError, "differs"),
        ):
            wheels.fetch_exact(self.artifact, target)
        self.assertFalse(target.exists())

    def test_runtime_requirements_are_exact_and_repeatable(self) -> None:
        plan = {"runtime": {"macos/arm64": [self.artifact], "linux/arm64": [self.artifact]}}
        for name in ("macos-arm64", "linux-arm64"):
            (self.root / "wheelhouse" / name).mkdir(parents=True)
        wheels.write_runtime_requirements(self.root, plan)
        wheels.write_runtime_requirements(self.root, plan)
        path = self.root / "wheelhouse/macos-arm64/runtime-requirements.txt"
        self.assertEqual(
            path.read_text(),
            f"synthetic==1.0 --hash={self.artifact['hash']}\n",
        )
        path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "differ"):
            wheels.write_runtime_requirements(self.root, plan)


if __name__ == "__main__":
    unittest.main()
