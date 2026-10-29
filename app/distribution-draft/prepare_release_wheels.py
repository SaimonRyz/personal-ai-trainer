"""Stage exact release wheels only after an explicit, scoped operator approval.

Without --execute this command performs a read-only inventory and prints one
complete list of missing artifacts. It never resolves dependency versions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.dont_write_bytecode = True

from plan_release_inputs import ROOT, checked_artifact, plan  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ValueError("release artifact redirected away from its exact pinned URL")


def expected_artifacts(
    input_plan: dict[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for target, name in (("macos/arm64", "macos-arm64"), ("linux/arm64", "linux-arm64")):
        items = input_plan["runtime"][target] + input_plan["build"]
        selected: dict[str, dict[str, Any]] = {}
        for item in items:
            checked = checked_artifact(item["name"], item["version"], item)
            prior = selected.get(checked["filename"])
            if prior is not None and prior != checked:
                raise ValueError("two pinned artifacts share a filename")
            selected[checked["filename"]] = checked
        result[name] = selected
    return result


def verify_existing(path: Path, artifact: dict[str, Any]) -> bool:
    if path.is_symlink():
        raise ValueError(f"linked release artifact: {path.name}")
    if not path.exists():
        return False
    if not path.is_file() or path.stat().st_size != artifact["size"]:
        raise ValueError(f"existing release artifact differs: {path.name}")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != artifact["hash"]:
        raise ValueError(f"existing release artifact differs: {path.name}")
    return True


def inventory(
    root: Path, expected: dict[str, dict[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    wheelhouse = root / "wheelhouse"
    if wheelhouse.is_symlink():
        raise ValueError("linked wheelhouse is forbidden")
    for target, artifacts in expected.items():
        folder = wheelhouse / target
        if folder.is_symlink():
            raise ValueError(f"linked wheelhouse target: {target}")
        if folder.exists() and not folder.is_dir():
            raise ValueError(f"wheelhouse target is not a directory: {target}")
        for artifact in artifacts.values():
            if not verify_existing(folder / artifact["filename"], artifact):
                missing.append({"target": target, **artifact})
    return missing


def fetch_exact(artifact: dict[str, Any], target: Path) -> None:
    parsed = urlparse(artifact["url"])
    if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org":
        raise ValueError("untrusted release artifact URL")
    if target.parent.parent.is_symlink() or target.parent.is_symlink():
        raise ValueError("linked wheelhouse is forbidden")
    target.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(artifact["url"], method="GET")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".release-wheel-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        received = 0
        with os.fdopen(descriptor, "wb") as output, opener.open(request, timeout=30) as response:
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > artifact["size"]:
                    raise ValueError("downloaded release artifact exceeds pinned size")
                digest.update(block)
                output.write(block)
        if received != artifact["size"] or "sha256:" + digest.hexdigest() != artifact["hash"]:
            raise ValueError("downloaded release artifact differs from pinned metadata")
        if target.exists() or target.is_symlink():
            raise ValueError("release artifact appeared during download")
        os.chmod(temporary, 0o600)
        os.link(temporary, target, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


def write_runtime_requirements(root: Path, input_plan: dict[str, Any]) -> None:
    for target, name in (("macos/arm64", "macos-arm64"), ("linux/arm64", "linux-arm64")):
        folder = root / "wheelhouse" / name
        if folder.parent.is_symlink() or folder.is_symlink() or not folder.is_dir():
            raise ValueError(f"verified wheelhouse target is missing: {name}")
        requirements = "".join(
            f"{item['name']}=={item['version']} --hash={item['hash']}\n"
            for item in input_plan["runtime"][target]
        )
        path = folder / "runtime-requirements.txt"
        if path.is_symlink():
            raise ValueError(f"linked requirements file: {name}")
        if path.exists():
            if not path.is_file() or path.read_text(encoding="utf-8") != requirements:
                raise ValueError(f"runtime requirements differ from pinned plan: {name}")
            continue
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(requirements)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="download only after explicit approval"
    )
    args = parser.parse_args()
    input_plan = plan()
    selected = expected_artifacts(input_plan)
    missing = inventory(ROOT, selected)
    summary = {"status": "PLAN_ONLY_NOT_DOWNLOADED", "missing_count": len(missing),
               "missing_bytes": sum(item["size"] for item in missing), "missing": missing}
    if not args.execute:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    for item in missing:
        target = ROOT / "wheelhouse" / item["target"] / item["filename"]
        fetch_exact(item, target)
    if inventory(ROOT, selected):
        raise ValueError("release wheelhouse is still incomplete")
    write_runtime_requirements(ROOT, input_plan)
    print("RELEASE_WHEELS_VERIFIED")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        print(f"RELEASE_WHEELS_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
