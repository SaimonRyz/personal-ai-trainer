"""Offline tests of the exact ARM64 wheel selection and fail-closed metadata checks."""

from __future__ import annotations

import unittest

import plan_release_inputs as planner


class ReleaseInputPlannerTests(unittest.TestCase):
    def test_current_lock_has_complete_arm64_runtime_and_build_wheels(self) -> None:
        result = planner.plan()
        self.assertEqual(result["status"], "PLAN_ONLY_NOT_DOWNLOADED")
        self.assertEqual(len(result["runtime"]["macos/arm64"]), 38)
        self.assertEqual(len(result["runtime"]["linux/arm64"]), 39)
        self.assertEqual(len(result["build"]), 6)
        for artifacts in (*result["runtime"].values(), result["build"]):
            for artifact in artifacts:
                self.assertEqual(len(artifact["hash"]), 71)
                self.assertTrue(artifact["url"].endswith("/" + artifact["filename"]))
        linux = {item["name"]: item for item in result["runtime"]["linux/arm64"]}
        self.assertIn("manylinux", linux["psycopg-binary"]["filename"])
        self.assertIn("aarch64", linux["psycopg-binary"]["filename"])

    def test_unknown_marker_and_unsupported_wheel_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported lock marker"):
            planner.marker_applies("sys_platform == 'darwin'", "linux/arm64")
        self.assertIsNone(
            planner.wheel_score("unsafe-1.0-cp312-cp312-win_amd64.whl", "linux/arm64")
        )

    def test_artifact_metadata_rejects_unsafe_origin_or_hash(self) -> None:
        artifact = {
            "url": "https://example.org/wheel-1.0-py3-none-any.whl",
            "hash": "sha256:" + "1" * 64,
            "size": 100,
        }
        with self.assertRaisesRegex(ValueError, "unverified artifact metadata"):
            planner.checked_artifact("wheel", "1.0", artifact)
        artifact["url"] = "https://files.pythonhosted.org/wheel-1.0-py3-none-any.whl"
        artifact["hash"] = "sha256:bad"
        with self.assertRaisesRegex(ValueError, "unverified artifact metadata"):
            planner.checked_artifact("wheel", "1.0", artifact)


if __name__ == "__main__":
    unittest.main()
