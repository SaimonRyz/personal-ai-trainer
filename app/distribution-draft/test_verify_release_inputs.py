"""Mutation probes for the read-only public recipe verifier."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import verify_release_inputs as gate


class ReleaseInputsTest(unittest.TestCase):
    def test_current_candidate_is_statically_consistent(self) -> None:
        self.assertEqual(gate.verify(), (55, 264))

    def test_lock_digest_mismatch_fails_closed(self) -> None:
        with (
            patch.object(gate, "digest", return_value="0" * 64),
            self.assertRaisesRegex(ValueError, "uv.lock differs"),
        ):
            gate.verify()

    def test_missing_build_constraints_fail_closed(self) -> None:
        original = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == gate.HERE / "build-constraints.txt":
                return ""
            return original(path, *args, **kwargs)

        with (
            patch.object(Path, "read_text", read_text),
            self.assertRaisesRegex(ValueError, "build constraint closure changed"),
        ):
            gate.verify()

    def test_wrong_uv_copy_source_fails_closed(self) -> None:
        original = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            content = original(path, *args, **kwargs)
            if path == gate.HERE / "Dockerfile.release":
                return content.replace(
                    "COPY --from=uv_binary /usr/local/bin/uv /usr/local/bin/uv",
                    "COPY --from=uv_binary /uv /usr/local/bin/uv",
                )
            return content

        with (
            patch.object(Path, "read_text", read_text),
            self.assertRaisesRegex(ValueError, "source path"),
        ):
            gate.verify()

    def test_missing_image_layer_size_fails_closed(self) -> None:
        original = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            content = original(path, *args, **kwargs)
            if path == gate.HERE / "RELEASE_INPUTS.json":
                pins = json.loads(content)
                pins["python_image"]["compressed_layer_bytes"] = 0
                return json.dumps(pins)
            return content

        with (
            patch.object(Path, "read_text", read_text),
            self.assertRaisesRegex(ValueError, "compressed layer size"),
        ):
            gate.verify()


if __name__ == "__main__":
    unittest.main()
