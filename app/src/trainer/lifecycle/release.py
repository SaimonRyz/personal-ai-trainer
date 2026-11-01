"""Fail-closed lifecycle for a source-built, local release candidate."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

from trainer.lifecycle.alpha import IMAGE_ID, AlphaWorkspaceLifecycleManager
from trainer.lifecycle.system import LifecycleError, LifecycleManager, LifecycleReport

MARKER = "distribution-draft/release-workspace.json"
MARKER_VALUE = {"kind": "personal-ai-trainer-release-candidate", "version": 1}
RECIPE = "distribution-draft/RELEASE_INPUTS.json"
PROOF = "RELEASE_BUILD.json"


def source_files(root: Path) -> dict[str, str]:
    """Hash source and build inputs, never runtime state or user data."""
    names = {
        "pyproject.toml", "uv.lock", "README.md", "alembic.ini", "compose.yaml",
        ".dockerignore", "distribution-draft/Dockerfile.release",
        "distribution-draft/build-constraints.txt", RECIPE,
        "distribution-draft/BUILD_ARTIFACTS.json",
        "distribution-draft/plan_release_inputs.py",
        "distribution-draft/prepare_release_wheels.py",
        "distribution-draft/preflight_release.py",
        "distribution-draft/RELEASE_WHEEL_PLAN.json",
        "distribution-draft/build_release.py", "distribution-draft/verify_release_inputs.py",
        MARKER,
    }
    for tree in ("src", "mcp", "migrations"):
        base = root / tree
        if base.is_symlink() or not base.is_dir():
            raise LifecycleError("release source tree is missing or linked")
        for path in base.rglob("*"):
            if path.is_symlink():
                raise LifecycleError("release source symlink is forbidden")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                names.add(path.relative_to(root).as_posix())
    result: dict[str, str] = {}
    for name in sorted(names):
        relative = Path(name)
        path = root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise LifecycleError("release input is linked")
        if not path.is_file():
            raise LifecycleError("release input is missing")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class ReleaseWorkspaceLifecycleManager(AlphaWorkspaceLifecycleManager):
    """Reuse alpha ownership/network boundaries, with release-specific proof."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root)
        self.runtime_dir = self.root / "tmp" / "release-lifecycle"
        self.state_path = self.runtime_dir / "state.json"
        self.mcp_path = self.runtime_dir / "mcp.json"

    def _validate_workspace(self, *, require_environment: bool) -> None:
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise LifecycleError("release workspace directory is unsafe")
        marker = self.root / MARKER
        compose = self.root / "compose.yaml"
        if (
            marker.is_symlink() or not marker.is_file()
            or compose.is_symlink() or not compose.is_file()
        ):
            raise LifecycleError("release marker or Compose file is unsafe")
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LifecycleError("release workspace marker is unreadable") from error
        if value != MARKER_VALUE or (self.root / "alpha-workspace.json").exists():
            raise LifecycleError("release workspace profile is ambiguous")
        for path in (self.runtime_dir.parent, self.runtime_dir, self.state_path, self.mcp_path):
            if path.is_symlink():
                raise LifecycleError("release runtime symlink is forbidden")
            if path.exists():
                item = path.stat()
                forbidden = 0o022 if path == self.runtime_dir.parent else 0o077
                if item.st_uid != os.getuid() or item.st_mode & forbidden:
                    raise LifecycleError("release runtime permissions are unsafe")
        source_files(self.root)
        if require_environment:
            if Path(sys.prefix).absolute() != (self.root / ".venv").absolute():
                raise LifecycleError("release launcher requires its own .venv")
            self._validate_build_proof(self._local_environment())

    def _validate_build_proof(self, local: dict[str, str]) -> None:
        path = self.root / PROOF
        if path.is_symlink() or not path.is_file():
            raise LifecycleError("release build proof is missing")
        try:
            proof = json.loads(path.read_text(encoding="utf-8"))
            image_id = local["TRAINER_ALPHA_API_IMAGE"]
            pins = json.loads((self.root / RECIPE).read_text(encoding="utf-8"))
            expected = source_files(self.root)
            image_files = {name: value for name, value in expected.items()
                           if name.startswith(("src/", "mcp/", "migrations/"))
                           or name in {"pyproject.toml", "uv.lock", "README.md", "alembic.ini"}}
            lock = tomllib.loads((self.root / "uv.lock").read_text(encoding="utf-8"))
            allowed = {package["name"]: package["version"] for package in lock["package"]}
            constraints = self.root / "distribution-draft/build-constraints.txt"
            for line in constraints.read_text().splitlines():
                name, version = line.split(" ", 1)[0].split("==")
                allowed[name] = version
            inspection = proof["inspection"]
            packages = inspection["packages"]
            postgres = proof["postgres"]
            valid = (
                proof["schema_version"] == 1 and proof["status"] == "BUILD_PASS"
                and IMAGE_ID.fullmatch(image_id) is not None and proof["image_id"] == image_id
                and proof["source_files"] == expected and proof["release_inputs"] == pins
                and inspection["python"] == "3.12.14"
                and inspection["imports"] == ["trainer", "trainer_mcp"]
                and inspection["lock_sha256"] == expected["uv.lock"]
                and inspection["source_files"] == image_files
                and inspection["dependencies_verified"] is True
                and isinstance(packages, dict)
                and {"personal-ai-trainer", "mcp", "psycopg-binary", "fastapi"} <= packages.keys()
                and all(allowed.get(name) == version for name, version in packages.items())
                and proof["platform"] == "linux/arm64"
                and postgres["reference"] == pins["postgres_image"]["reference"]
                and postgres["config_digest"] == pins["postgres_image"]["expected_image_id"]
                and postgres["config_bytes_verified"] is True
                and postgres["local_image_id"] == local["TRAINER_ALPHA_POSTGRES_IMAGE"]
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise LifecycleError("release build proof is invalid") from error
        if not valid:
            raise LifecycleError("release build proof does not match source and image")

    def _validate_deployed_build_proof(self, local: dict[str, str]) -> None:
        """Permit a safe stop while source has advanced beyond the running image."""
        path = self.root / PROOF
        if path.is_symlink() or not path.is_file():
            raise LifecycleError("deployed release build proof is missing")
        try:
            proof = json.loads(path.read_text(encoding="utf-8"))
            source = proof["source_files"]
            image_files = {
                name: value for name, value in source.items()
                if name.startswith(("src/", "mcp/", "migrations/"))
                or name in {"pyproject.toml", "uv.lock", "README.md", "alembic.ini"}
            }
            pins = json.loads((self.root / RECIPE).read_text(encoding="utf-8"))
            inspection = proof["inspection"]
            postgres = proof["postgres"]
            valid = (
                proof["schema_version"] == 1 and proof["status"] == "BUILD_PASS"
                and IMAGE_ID.fullmatch(local["TRAINER_ALPHA_API_IMAGE"]) is not None
                and proof["image_id"] == local["TRAINER_ALPHA_API_IMAGE"]
                and proof["release_inputs"] == pins
                and source["uv.lock"] == hashlib.sha256((self.root / "uv.lock").read_bytes()).hexdigest()
                and inspection["source_files"] == image_files
                and inspection["lock_sha256"] == source["uv.lock"]
                and inspection["python"] == "3.12.14"
                and inspection["dependencies_verified"] is True
                and proof["platform"] == "linux/arm64"
                and postgres["reference"] == pins["postgres_image"]["reference"]
                and postgres["config_digest"] == pins["postgres_image"]["expected_image_id"]
                and postgres["config_bytes_verified"] is True
                and postgres["local_image_id"] == local["TRAINER_ALPHA_POSTGRES_IMAGE"]
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise LifecycleError("deployed release build proof is invalid") from error
        if not valid:
            raise LifecycleError("deployed release build proof does not match configured images")

    def stop(self, *, full: bool = False) -> LifecycleReport:
        if full:
            return super().stop(full=True)
        self._validate_workspace(require_environment=False)
        if Path(sys.prefix).absolute() != (self.root / ".venv").absolute():
            raise LifecycleError("release launcher requires its own .venv")
        self._validate_deployed_build_proof(self._local_environment())
        owned_services = self._validate_ownership()
        if self._port_pids(self.api_port) and "api" not in owned_services:
            raise LifecycleError("release API listener has no owned container")
        return LifecycleManager.stop(self, full=False)

    def _validate_postgres_provenance(self, local: dict[str, str]) -> None:
        try:
            pin = json.loads((self.root / RECIPE).read_text())["postgres_image"]
            proof_path = self.root / PROOF
            if proof_path.is_symlink() or not proof_path.is_file():
                raise LifecycleError("release PostgreSQL proof is missing")
            proof = json.loads(proof_path.read_text())["postgres"]
            image_id = local["TRAINER_ALPHA_POSTGRES_IMAGE"]
            if (
                IMAGE_ID.fullmatch(image_id) is None
                or proof["local_image_id"] != image_id
                or proof["config_digest"] != pin["expected_image_id"]
                or proof["reference"] != pin["reference"]
                or proof["config_bytes_verified"] is not True
            ):
                raise LifecycleError("release PostgreSQL config differs from verified proof")
            result = self._run(
                ["docker", "image", "inspect", "--format", "{{json .}}", image_id]
            )
            if result.returncode != 0:
                raise LifecycleError("release PostgreSQL image is unavailable")
            image = json.loads(result.stdout)
            repository, digest = pin["reference"].split("@", 1)
            repository = repository.rsplit(":", 1)[0].removeprefix("docker.io/")
            digests: set[str] = set()
            for entry in image.get("RepoDigests", []):
                repo, entry_digest = entry.split("@", 1)
                repo = repo.removeprefix("docker.io/")
                if "/" not in repo:
                    repo = "library/" + repo
                digests.add(repo + "@" + entry_digest)
            if (
                image.get("Id") != image_id
                or image.get("Os") != "linux"
                or image.get("Architecture") != "arm64"
                or repository + "@" + digest not in digests
            ):
                raise LifecycleError("release PostgreSQL registry provenance is unverified")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise LifecycleError("release PostgreSQL metadata is invalid") from error

    def _ensure_docker(self) -> None:
        self._validate_workspace(require_environment=True)
        LifecycleManager._ensure_docker(self)
        self._validate_images_and_compose()
        self._validate_ownership()
        if self._port_pids(self.api_port):
            raise LifecycleError("release API loopback port is occupied")
        if self._port_pids(self.mcp_port):
            if not self._mcp_owned():
                raise LifecycleError("release MCP loopback port has no proven ownership")
            errors = self._stop_owned_mcp()
            if errors or self._port_pids(self.mcp_port):
                raise LifecycleError("owned release MCP process did not stop")

    def configure_image(self) -> dict[str, object]:
        """Select a verified API image without changing any other private setting."""
        self._validate_workspace(require_environment=False)
        if Path(sys.prefix).absolute() != (self.root / ".venv").absolute():
            raise LifecycleError("release configuration requires its own .venv")
        with self._lock():
            if not self._docker_engine():
                raise LifecycleError("local Docker is unavailable")
            if self._project_containers() or any(
                self._port_pids(port) for port in (self.api_port, self.mcp_port)
            ):
                raise LifecycleError("stop the release runtime before selecting an API image")
            self._validate_ownership()
            current_values = self._local_environment()
            env_path = self.root / ".env"
            original = env_path.lstat()
            raw = env_path.read_bytes()
            try:
                image_id = json.loads((self.root / PROOF).read_text())["image_id"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise LifecycleError("release build proof is invalid") from error
            candidate = {**current_values, "TRAINER_ALPHA_API_IMAGE": image_id}
            self._validate_build_proof(candidate)
            self._validate_postgres_provenance(candidate)
            result = self._run(
                ["docker", "image", "inspect", "--format", "{{json .}}", image_id]
            )
            try:
                image = json.loads(result.stdout)
            except ValueError as error:
                raise LifecycleError("release API image metadata is invalid") from error
            if (
                result.returncode != 0 or image.get("Id") != image_id
                or image.get("Os") != "linux" or image.get("Architecture") != "arm64"
            ):
                raise LifecycleError("release API image differs from verified build")
            try:
                lines = raw.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError as error:
                raise LifecycleError("private configuration is not UTF-8") from error
            updated: list[str] = []
            matches = 0
            for line in lines:
                if "=" in line and line.split("=", 1)[0].strip() == "TRAINER_ALPHA_API_IMAGE":
                    prefix = line.split("=", 1)[0]
                    if line.endswith("\r\n"):
                        ending = "\r\n"
                    elif line.endswith("\n"):
                        ending = "\n"
                    else:
                        ending = ""
                    updated.append(f"{prefix}={image_id}{ending}")
                    matches += 1
                else:
                    updated.append(line)
            if matches != 1:
                raise LifecycleError("private configuration has no unique API image setting")
            replacement = "".join(updated).encode("utf-8")
            if replacement == raw:
                return {"status": "CONFIGURED", "api_image_id": image_id,
                        "database_preserved": True, "changed": False}
            current = env_path.lstat()
            if (
                stat.S_ISLNK(current.st_mode) or current.st_ino != original.st_ino
                or current.st_mtime_ns != original.st_mtime_ns
            ):
                raise LifecycleError("private configuration changed during image validation")
            descriptor, temporary = tempfile.mkstemp(prefix=".env-update-", dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(replacement)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, env_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return {"status": "CONFIGURED", "api_image_id": image_id,
                    "database_preserved": True, "changed": True}
