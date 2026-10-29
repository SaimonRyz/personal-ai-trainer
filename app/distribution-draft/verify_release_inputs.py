"""Static, read-only gate for the candidate public build recipe.

This does not contact registries, install packages, or prove a runtime build.
Run with CPython 3.12+ from the current project root:
    python distribution-draft/verify_release_inputs.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONSTRAINT = re.compile(r"([a-z][a-z0-9-]*)==([^ ]+) --hash=(sha256:[0-9a-f]{64})\Z")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify() -> tuple[int, int]:
    pins = json.loads((HERE / "RELEASE_INPUTS.json").read_text(encoding="utf-8"))
    check(pins["schema_version"] == 1, "unsupported release-input schema")
    check(pins["target_host"] == "macos/arm64", "unsupported host target")
    check(pins["target_container"] == "linux/arm64", "unsupported container target")
    check(pins["status"] == "candidate_not_runtime_verified", "static gate cannot approve runtime")

    dockerfile = (HERE / "Dockerfile.release").read_text(encoding="utf-8")
    for key, host in (
        ("python_image", "docker.io"),
        ("uv_image", "ghcr.io"),
        ("postgres_image", "docker.io"),
    ):
        image = pins[key]
        reference = image["reference"]
        check(reference.startswith(host + "/"), f"wrong {key} registry")
        check(
            "@sha256:" in reference
            and SHA.fullmatch("sha256:" + reference.rsplit("@sha256:", 1)[1]) is not None,
            f"unpinned {key}",
        )
        check(
            SHA.fullmatch(image["platform_manifest_digest"]) is not None,
            f"invalid {key} platform manifest digest",
        )
        check(SHA.fullmatch(image["expected_image_id"]) is not None, f"invalid {key} image ID")
        check(
            type(image["compressed_layer_bytes"]) is int and image["compressed_layer_bytes"] > 0,
            f"invalid {key} compressed layer size",
        )
        metadata = urlparse(image["metadata_source"])
        check(
            metadata.scheme == "https" and metadata.hostname in {"hub.docker.com", "github.com"},
            f"untrusted {key} metadata source",
        )
        if key != "postgres_image":
            check(
                dockerfile.count(reference) == 1, f"Dockerfile must use pinned {key} exactly once"
            )
    check(
        pins["postgres_image"]["platform_manifest_digest"]
        == "sha256:" + pins["postgres_image"]["reference"].rsplit("@sha256:", 1)[1],
        "PostgreSQL reference must be the platform manifest",
    )
    check("latest" not in dockerfile.lower(), "Dockerfile contains a mutable latest tag")
    check(
        "COPY --from=uv_binary /usr/local/bin/uv /usr/local/bin/uv" in dockerfile,
        "pinned Alpine uv image requires /usr/local/bin/uv source path",
    )
    check(
        "COPY wheelhouse/linux-arm64 /tmp/release-wheels" in dockerfile,
        "verified wheelhouse must be copied into release build",
    )
    check(
        (
            "--offline --no-index --find-links /tmp/release-wheels --require-hashes "
            "--no-build -r /tmp/release-wheels/runtime-requirements.txt"
        )
        in dockerfile,
        "runtime dependencies must use only hashed offline wheels",
    )
    check(
        (
            "--offline --no-index --find-links /tmp/release-wheels --require-hashes "
            "--no-build -r /app/build-constraints.txt"
        )
        in dockerfile,
        "build dependencies must use only hashed offline wheels",
    )
    check(
        "uv pip install --python /app/.venv/bin/python --offline --no-build-isolation --no-deps ."
        in dockerfile,
        "local project build must be offline without dependency resolution",
    )
    context_rules = (PROJECT / ".dockerignore").read_text(encoding="utf-8")
    check("**" in context_rules.splitlines(), "build context must deny by default")
    for required in (
        "!pyproject.toml",
        "!uv.lock",
        "!README.md",
        "!src/**",
        "!mcp/**",
        "!migrations/**",
        "!distribution-draft/build-constraints.txt",
        "!wheelhouse/linux-arm64/**",
    ):
        check(required in context_rules.splitlines(), f"required build input excluded: {required}")
    for private in (".env", "TRAINING_PLAN.md", "**/*.sql", "**/*.log"):
        check(
            private in context_rules.splitlines(),
            f"private build-context exclusion missing: {private}",
        )

    lock_path = PROJECT / "uv.lock"
    check(SHA.fullmatch("sha256:" + pins["uv_lock_sha256"]) is not None, "invalid lock digest")
    check(digest(lock_path) == pins["uv_lock_sha256"], "uv.lock differs from release pin")
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    check(project["project"]["requires-python"] == ">=3.12,<3.13", "project Python range changed")
    check(project["build-system"]["build-backend"] == "hatchling.build", "unexpected build backend")
    check(lock["requires-python"] == "==3.12.*", "unexpected lock Python range")
    check(pins["python_package_index"] == "https://pypi.org/simple", "untrusted Python index")
    check(pins["python_artifact_host"] == "files.pythonhosted.org", "untrusted artifact host")

    package_count = 0
    artifact_count = 0
    for package in lock["package"]:
        source = package["source"]
        if package["name"] == project["project"]["name"]:
            check("editable" in source, "project source should be local")
            continue
        check(
            source == {"registry": pins["python_package_index"]},
            f"untrusted package source: {package['name']}",
        )
        check(package.get("wheels"), f"no wheel declared: {package['name']}")
        for artifact in [
            *package.get("wheels", []),
            *([package["sdist"]] if "sdist" in package else []),
        ]:
            url = urlparse(artifact["url"])
            check(
                url.scheme == "https" and url.hostname == pins["python_artifact_host"],
                f"untrusted artifact URL: {package['name']}",
            )
            check(SHA.fullmatch(artifact["hash"]) is not None, f"missing hash: {package['name']}")
            check(
                isinstance(artifact["size"], int) and artifact["size"] > 0,
                f"missing size: {package['name']}",
            )
            artifact_count += 1
        package_count += 1

    constraints = (HERE / "build-constraints.txt").read_text(encoding="utf-8").splitlines()
    names: set[str] = set()
    for line in constraints:
        match = CONSTRAINT.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid build constraint: {line!r}")
        check(match.group(1) not in names, f"duplicate build constraint: {match.group(1)}")
        names.add(match.group(1))
    check(
        names == {"hatchling", "packaging", "pathspec", "pluggy", "tomlkit", "trove-classifiers"},
        "build constraint closure changed",
    )
    check(
        "COPY distribution-draft/build-constraints.txt /app/build-constraints.txt" in dockerfile,
        "build constraints not copied into image",
    )
    return package_count, artifact_count


if __name__ == "__main__":
    try:
        packages, artifacts = verify()
    except (KeyError, OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"RELEASE_INPUTS_STATIC_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(
        f"RELEASE_INPUTS_STATIC_PASS packages={packages} artifacts={artifacts} runtime=NOT_CHECKED"
    )
