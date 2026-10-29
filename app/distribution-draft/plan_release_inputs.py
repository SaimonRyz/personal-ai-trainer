"""Resolve exact pinned runtime/build wheels for the two supported ARM64 targets.

Read-only: this module never downloads files, creates an environment or writes a plan.
Unknown lock markers or wheel tags fail closed instead of guessing an artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONSTRAINT = re.compile(r"([a-z][a-z0-9-]*)==([^ ]+) --hash=(sha256:[0-9a-f]{64})\Z")
LONG_MACHINE_MARKER = (
    "platform_machine == 'AMD64' or platform_machine == 'WIN32' or "
    "platform_machine == 'aarch64' or platform_machine == 'amd64' or "
    "platform_machine == 'ppc64le' or platform_machine == 'win32' or "
    "platform_machine == 'x86_64'"
)


def marker_applies(marker: str | None, target: str) -> bool:
    if marker is None:
        return True
    if marker in {
        "implementation_name != 'PyPy'", "implementation_name != 'pypy'",
        "platform_python_implementation != 'PyPy'", "sys_platform != 'emscripten'",
    }:
        return True
    if marker == "sys_platform == 'win32'":
        return False
    if marker == LONG_MACHINE_MARKER:
        return target == "linux/arm64"
    raise ValueError(f"unsupported lock marker: {marker}")


def runtime_closure(lock: dict[str, Any], target: str) -> dict[str, dict[str, Any]]:
    packages = {item["name"]: item for item in lock["package"]}
    project = packages["personal-ai-trainer"]
    pending = list(project["dependencies"])
    selected: dict[str, dict[str, Any]] = {}
    processed_extras: dict[str, set[str]] = {}
    while pending:
        dependency = pending.pop()
        if not marker_applies(dependency.get("marker"), target):
            continue
        name = dependency["name"]
        package = packages.get(name)
        if package is None or name == "personal-ai-trainer":
            raise ValueError(f"missing locked dependency: {name}")
        if name not in selected:
            selected[name] = package
            pending.extend(package.get("dependencies", []))
        extras = processed_extras.setdefault(name, set())
        for extra in dependency.get("extra", []):
            if extra not in extras:
                optional = package.get("optional-dependencies", {})
                if extra not in optional:
                    raise ValueError(f"missing locked extra: {name}[{extra}]")
                pending.extend(optional[extra])
                extras.add(extra)
    return dict(sorted(selected.items()))


def wheel_score(filename: str, target: str) -> int | None:
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return None
    python_tags, abi_tags, platform_tags = (part.split(".") for part in parts[-3:])
    exact = "cp312" in python_tags and "cp312" in abi_tags
    stable_versions = [
        int(tag[3:]) for tag in python_tags
        if re.fullmatch(r"cp3(\d+)", tag) and int(tag[3:]) <= 12
    ] if "abi3" in abi_tags else []
    stable = bool(stable_versions)
    pure = "none" in abi_tags and any(tag in {"py3", "py2", "py312"} for tag in python_tags)
    if not (exact or stable or pure):
        return None
    abi_score = 300 if exact else 200 + max(stable_versions) if stable else 100
    platform_score: int | None = None
    for platform in platform_tags:
        if platform == "any":
            platform_score = max(platform_score or 0, 1)
        elif target == "linux/arm64":
            match = re.fullmatch(r"manylinux_2_(\d+)_aarch64", platform)
            if match and 17 <= int(match.group(1)) <= 36:
                platform_score = max(platform_score or 0, int(match.group(1)))
            elif platform == "manylinux2014_aarch64":
                platform_score = max(platform_score or 0, 17)
        elif target == "macos/arm64":
            match = re.fullmatch(r"macosx_(\d+)_(\d+)_(arm64|universal2)", platform)
            if match and (int(match.group(1)), int(match.group(2))) <= (11, 0):
                platform_score = max(platform_score or 0, 20)
    return abi_score + platform_score if platform_score is not None else None


def choose_wheel(package: dict[str, Any], target: str) -> dict[str, Any]:
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for artifact in package.get("wheels", []):
        filename = artifact["url"].rsplit("/", 1)[-1]
        score = wheel_score(filename, target)
        if score is not None:
            candidates.append((score, filename, artifact))
    if not candidates:
        raise ValueError(f"no compatible wheel: {package['name']} {target}")
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise ValueError(f"ambiguous compatible wheels: {package['name']} {target}")
    return candidates[0][2]


def checked_artifact(
    name: str, version: str, artifact: dict[str, Any]
) -> dict[str, Any]:
    url = urlparse(artifact["url"])
    filename = url.path.rsplit("/", 1)[-1]
    if (
        url.scheme != "https" or url.hostname != "files.pythonhosted.org"
        or url.query or url.fragment or artifact.get("filename", filename) != filename
        or not filename.endswith(".whl") or SHA.fullmatch(artifact["hash"]) is None
        or not isinstance(artifact["size"], int) or artifact["size"] <= 0
    ):
        raise ValueError(f"unverified artifact metadata: {name}")
    return {"name": name, "version": version, "filename": filename,
            "url": artifact["url"], "size": artifact["size"], "hash": artifact["hash"]}


def build_artifacts(lock: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = json.loads((HERE / "BUILD_ARTIFACTS.json").read_text(encoding="utf-8"))
    if metadata.get("schema_version") != 1:
        raise ValueError("unsupported build artifact metadata")
    missing = metadata["artifacts"]
    if set(missing) != {"hatchling", "tomlkit", "trove-classifiers"}:
        raise ValueError("unexpected out-of-lock build artifacts")
    packages = {item["name"]: item for item in lock["package"]}
    result = []
    for line in (HERE / "build-constraints.txt").read_text().splitlines():
        match = CONSTRAINT.fullmatch(line)
        if match is None:
            raise ValueError("invalid build constraint")
        name, version, expected_hash = match.groups()
        if name in missing:
            artifact = missing[name]
            if artifact["version"] != version:
                raise ValueError(f"build version differs: {name}")
        else:
            package = packages[name]
            if package["version"] != version:
                raise ValueError(f"locked build version differs: {name}")
            artifact = choose_wheel(package, "linux/arm64")
        if artifact["hash"] != expected_hash:
            raise ValueError(f"build hash differs: {name}")
        result.append(checked_artifact(name, version, artifact))
    if len(result) != 6:
        raise ValueError("incomplete build dependency closure")
    return result


def plan() -> dict[str, Any]:
    lock_bytes = (ROOT / "uv.lock").read_bytes()
    pins = json.loads((HERE / "RELEASE_INPUTS.json").read_text())
    lock_hash = hashlib.sha256(lock_bytes).hexdigest()
    if lock_hash != pins["uv_lock_sha256"]:
        raise ValueError("uv.lock differs from release pin")
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    runtime = {}
    for target in ("macos/arm64", "linux/arm64"):
        runtime[target] = [
            checked_artifact(name, package["version"], choose_wheel(package, target))
            for name, package in runtime_closure(lock, target).items()
        ]
    build = build_artifacts(lock)
    result = {"schema_version": 1, "status": "PLAN_ONLY_NOT_DOWNLOADED",
              "uv_lock_sha256": lock_hash, "runtime": runtime, "build": build,
              "images": {key: pins[key]["reference"] for key in
                         ("python_image", "uv_image", "postgres_image")}}
    snapshot_path = HERE / "RELEASE_WHEEL_PLAN.json"
    if snapshot_path.exists() and json.loads(snapshot_path.read_text()) != result:
        raise ValueError("packaged wheel plan differs from current lock and pins")
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        print(f"RELEASE_INPUT_PLAN_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
