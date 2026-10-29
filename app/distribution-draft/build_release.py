"""Build/inspect the pinned candidate; never creates .env, a volume or a database."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "distribution-draft"))

from plan_release_inputs import plan as plan_inputs  # noqa: E402

from trainer.lifecycle.release import PROOF, source_files  # noqa: E402
from trainer.lifecycle.system import LifecycleError  # noqa: E402

INSPECT = """
import hashlib, importlib.metadata, json, platform
from pathlib import Path
import trainer, trainer_mcp
import trainer.entrypoints.api, trainer_mcp.server
from packaging.requirements import Requirement
from packaging.version import Version
for distribution in importlib.metadata.distributions():
    extras = ('', 'binary') if distribution.metadata['Name'].lower() == 'psycopg' else ('',)
    for requirement_text in distribution.requires or []:
        requirement = Requirement(requirement_text)
        if requirement.marker and not any(
            requirement.marker.evaluate({'extra': e}) for e in extras
        ):
            continue
        version = importlib.metadata.version(requirement.name)
        if requirement.specifier and Version(version) not in requirement.specifier:
            raise RuntimeError('installed requirement mismatch')
root = Path('/app')
files = {}
for base in ('src', 'mcp', 'migrations'):
    for path in (root / base).rglob('*'):
        if path.is_symlink():
            raise RuntimeError('linked image source')
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
for name in ('pyproject.toml', 'uv.lock', 'README.md', 'alembic.ini'):
    files[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
print(json.dumps({'python': platform.python_version(), 'imports': ['trainer', 'trainer_mcp'],
    'lock_sha256': files['uv.lock'], 'source_files': files,
    'dependencies_verified': True,
    'packages': {d.metadata['Name'].lower().replace('_', '-'): d.version
                 for d in importlib.metadata.distributions()}}))
"""


def verify_wheelhouse() -> dict[str, Any]:
    """Require the entire pinned Linux wheel closure before contacting Docker."""
    input_plan = plan_inputs()
    directory = ROOT / "wheelhouse/linux-arm64"
    if directory.parent.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise LifecycleError("verified Linux ARM64 wheelhouse is missing")
    artifacts = input_plan["runtime"]["linux/arm64"] + input_plan["build"]
    expected = {item["filename"]: item for item in artifacts}
    if len(expected) != len(artifacts):
        raise LifecycleError("duplicate Linux ARM64 wheel filename")
    actual = {path.name for path in directory.iterdir()}
    unexpected = actual - set(expected) - {"runtime-requirements.txt"}
    missing = set(expected) - actual
    if unexpected or missing:
        raise LifecycleError(
            "Linux ARM64 wheelhouse differs from the exact plan: "
            f"missing={sorted(missing)} unexpected={sorted(unexpected)}"
        )
    for filename, item in expected.items():
        path = directory / filename
        if (
            path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]
            or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != item["hash"]
        ):
            raise LifecycleError(f"Linux ARM64 wheel failed integrity check: {filename}")
    requirements = "".join(
        f"{item['name']}=={item['version']} --hash={item['hash']}\n"
        for item in input_plan["runtime"]["linux/arm64"]
    )
    path = directory / "runtime-requirements.txt"
    if path.is_symlink():
        raise LifecycleError("runtime requirements path is linked")
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != requirements:
            raise LifecycleError("runtime requirements differ from pinned wheel plan")
    else:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(requirements)
    return input_plan


def run(command: list[str], *, input: str | None = None) -> str:
    safe = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "TMPDIR") if k in os.environ}
    result = subprocess.run(command, cwd=ROOT, env=safe, input=input, text=True,
                            capture_output=True, timeout=1800)
    if result.returncode:
        # Keep diagnostics local and private; never emit raw logs or host environment.
        with tempfile.NamedTemporaryFile(mode="w", prefix="trainer-build-error-", suffix=".log",
                                         encoding="utf-8", delete=False) as diagnostic:
            diagnostic.write(result.stdout + "\n" + result.stderr)
            diagnostic_path = diagnostic.name
        raise LifecycleError(
            f"release command failed: {command[0]} {command[1]}; local log: {diagnostic_path}"
        )
    return result.stdout.strip()


def verify_postgres_config(
    pin: dict[str, Any], image: dict[str, Any]
) -> dict[str, Any]:
    """Verify config bytes even when containerd exposes a manifest as Docker's Id."""
    expected = pin["expected_image_id"]
    with tempfile.TemporaryDirectory(prefix="trainer-postgres-inspect-") as temporary:
        archive = Path(temporary) / "image.tar"
        run(["docker", "image", "save", "--platform=linux/arm64", "--output", str(archive),
             pin["reference"]])
        with tarfile.open(archive, "r:*") as tar:
            manifest_stream = tar.extractfile("manifest.json")
            if manifest_stream is None:
                raise LifecycleError("PostgreSQL archive manifest is missing")
            manifests = json.load(manifest_stream)
            if len(manifests) != 1:
                raise LifecycleError("PostgreSQL archive contains ambiguous images")
            member = tar.getmember(manifests[0]["Config"])
            if not member.isfile() or member.size > 1024 * 1024:
                raise LifecycleError("PostgreSQL config entry is unsafe")
            config_stream = tar.extractfile(member)
            if config_stream is None:
                raise LifecycleError("PostgreSQL config entry is missing")
            raw = config_stream.read()
    config = json.loads(raw)
    if ("sha256:" + hashlib.sha256(raw).hexdigest() != expected
            or config.get("os") != "linux" or config.get("architecture") != "arm64"
            or config.get("rootfs", {}).get("diff_ids") != image.get("RootFS", {}).get("Layers")):
        raise LifecycleError("PostgreSQL config bytes differ from pinned image")
    return {"reference": pin["reference"], "config_digest": expected,
            "local_image_id": image["Id"], "config_bytes_verified": True}


def main() -> None:
    if sys.version_info[:3] != (3, 12, 14):
        raise LifecycleError("release builder requires CPython 3.12.14")
    if any(os.environ.get(k) for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY")):
        raise LifecycleError("Docker endpoint override is forbidden")
    from trainer.lifecycle.release import ReleaseWorkspaceLifecycleManager
    manager = ReleaseWorkspaceLifecycleManager(ROOT)
    manager._validate_workspace(require_environment=False)
    verify_wheelhouse()
    manager._validate_docker_endpoint()
    run([sys.executable, "-B", "distribution-draft/verify_release_inputs.py"])
    pins = json.loads((ROOT / "distribution-draft/RELEASE_INPUTS.json").read_text())
    before = source_files(ROOT)
    # Inputs must already have been pulled by exact registry digest after approval.
    postgres = None
    for key in ("python_image", "uv_image", "postgres_image"):
        image = json.loads(run(["docker", "image", "inspect", "--format", "{{json .}}",
                               pins[key]["reference"]]))
        if image.get("Os") != "linux" or image.get("Architecture") != "arm64":
            raise LifecycleError("release input image platform mismatch")
        if key == "postgres_image":
            postgres = verify_postgres_config(pins[key], image)
    proof_path = ROOT / PROOF
    if proof_path.is_symlink() or proof_path.exists():
        raise LifecycleError("existing release build proof must be reviewed before replacement")
    # iidfile is private temporary metadata, outside the Docker build context.
    with tempfile.TemporaryDirectory(prefix="trainer-release-build-") as temporary:
        iid = Path(temporary) / "image-id"
        run([
            "docker", "build", "--platform=linux/arm64", "--pull=false",
            "--network=none", "--no-cache", "--iidfile", str(iid),
            "-f", "distribution-draft/Dockerfile.release", ".",
        ])
        image_id = iid.read_text().strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise LifecycleError("builder returned invalid image ID")
    image = json.loads(run(["docker", "image", "inspect", "--format", "{{json .}}", image_id]))
    if image.get("Os") != "linux" or image.get("Architecture") != "arm64":
        raise LifecycleError("built image platform mismatch")
    inspection = json.loads(run(["docker", "run", "--rm", "--pull=never", "--network=none",
                                "--read-only", "--cap-drop=ALL", "--security-opt",
                                "no-new-privileges:true", "-i", "--entrypoint", "python",
                                image_id, "-B", "-"], input=INSPECT))
    expected_image_files = {n: h for n, h in before.items()
                            if n.startswith(("src/", "mcp/", "migrations/"))
                            or n in {"pyproject.toml", "uv.lock", "README.md", "alembic.ini"}}
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    allowed = {p["name"]: p["version"] for p in lock["package"]}
    for line in (ROOT / "distribution-draft/build-constraints.txt").read_text().splitlines():
        name, version = line.split(" ", 1)[0].split("==")
        allowed[name] = version
    packages = inspection["packages"]
    if (inspection["python"] != "3.12.14" or inspection["imports"] != ["trainer", "trainer_mcp"]
            or inspection["dependencies_verified"] is not True
            or inspection["source_files"] != expected_image_files
            or inspection["lock_sha256"] != pins["uv_lock_sha256"]
            or any(allowed.get(n) != v for n, v in packages.items())
            or not {"personal-ai-trainer", "mcp", "psycopg-binary", "fastapi"} <= packages.keys()
            or source_files(ROOT) != before):
        raise LifecycleError("built image inspection differs from source or pinned packages")
    proof = {"schema_version": 1, "status": "BUILD_PASS", "image_id": image_id,
             "source_files": before, "release_inputs": pins, "platform": "linux/arm64",
             "inspection": inspection, "postgres": postgres}
    fd = os.open(proof_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(proof, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"BUILD_PASS image_id={image_id} runtime=NOT_CHECKED")


if __name__ == "__main__":
    try:
        main()
    except (LifecycleError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"BUILD_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
