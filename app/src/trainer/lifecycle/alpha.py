"""Fail-closed lifecycle for a separately packaged, persistent local alpha."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path

from trainer.lifecycle.model import LifecycleReport
from trainer.lifecycle.system import LifecycleError, LifecycleManager

ALPHA_PROJECT = "personal-ai-trainer-alpha"
ALPHA_API_PORT = 8767
ALPHA_MCP_PORT = 8768
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
SECRET = re.compile(r"[0-9a-f]{48}\Z")
OPERATIONAL_FILES = frozenset(
    {
        "AGENTS.md",
        "INITIAL_SETUP_INSTRUCTIONS.md",
        ".agents/skills/trainer-launch/SKILL.md",
    }
)
COMPOSE_ENV_KEYS = frozenset(
    {
        "TRAINER_DATABASE_ADMIN_DSN",
        "TRAINER_DATABASE_APP_DSN",
        "TRAINER_DATABASE_MIGRATOR_DSN",
        "TRAINER_API_HOST",
    }
)
POSTGRES_IMAGE_PIN = {
    "schema_version": 1,
    "reference": "docker.io/library/postgres:17.11-bookworm",
    "index_digest": "sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27",
    "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
    "platform_manifest_digest": "sha256:f97ca6038b7641359fef23126949d824ac68edd8a7081b424d6a78e1f7076f8e",
    "image_id": "sha256:8942ba4a60918c2b98bcad5c842453a765898854a9fec87f64ec816bf44559be",
    "metadata_source": "https://github.com/docker-library/repo-info/blob/master/repos/postgres/remote/17.11-bookworm.md",
}


class AlphaWorkspaceLifecycleManager(LifecycleManager):
    """Use only resources owned by the standalone alpha package and never purge its volume."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root)
        self.compose_project = ALPHA_PROJECT
        self.api_port = ALPHA_API_PORT
        self.mcp_port = ALPHA_MCP_PORT
        self.runtime_dir = self.root / "tmp" / "alpha-lifecycle"
        self.state_path = self.runtime_dir / "state.json"
        self.mcp_path = self.runtime_dir / "mcp.json"

    def _validate_docker_endpoint(self) -> None:
        if any(
            os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY")
        ):
            raise LifecycleError("alpha Docker endpoint override is forbidden")
        expected = f"unix://{pwd.getpwuid(os.getuid()).pw_dir}/.docker/run/docker.sock"
        context = self._run(["docker", "context", "show"], timeout=5)
        endpoint = self._run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            timeout=5,
        )
        if (
            context.returncode != 0
            or context.stdout.strip() != "desktop-linux"
            or endpoint.returncode != 0
            or endpoint.stdout.strip() != expected
        ):
            raise LifecycleError("alpha Docker context must use the local Docker Desktop socket")

    def _docker_engine(self) -> bool:
        self._validate_docker_endpoint()
        return super()._docker_engine()

    def _validate_workspace(self, *, require_environment: bool) -> None:
        root_info = self.root.stat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.getuid()
            or root_info.st_mode & 0o022
        ):
            raise LifecycleError("alpha workspace directory is unsafe")
        marker = self.root / "alpha-workspace.json"
        compose = self.root / "compose.yaml"
        for path in (marker, compose):
            if path.is_symlink() or not path.is_file():
                raise LifecycleError("alpha workspace marker or Compose file is unsafe")
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LifecycleError("alpha workspace marker is unreadable") from error
        if value != {"kind": ALPHA_PROJECT, "version": 1}:
            raise LifecycleError("alpha workspace marker is invalid")
        if require_environment:
            expected_venv = self.root / ".venv"
            if Path(sys.prefix).absolute() != expected_venv.absolute():
                raise LifecycleError("alpha launcher must use the workspace's own virtualenv")
            self._validate_build_proof(self._local_environment())
        for path in (self.runtime_dir, self.state_path, self.mcp_path):
            if path.is_symlink():
                raise LifecycleError("alpha runtime symlink is forbidden")
            if path.exists():
                info = path.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise LifecycleError("alpha runtime permissions are unsafe")

    def _local_environment(self) -> dict[str, str]:
        path = self.root / ".env"
        if path.is_symlink() or not path.is_file():
            raise LifecycleError("private alpha .env is missing or unsafe")
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LifecycleError("private alpha .env permissions are unsafe")
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise LifecycleError("private alpha .env has an invalid line")
            key, value = line.split("=", 1)
            key = key.strip()
            if key in values or key not in {
                "TRAINER_ALPHA_API_IMAGE",
                "TRAINER_ALPHA_POSTGRES_IMAGE",
                "TRAINER_POSTGRES_ADMIN_PASSWORD",
                "TRAINER_DATABASE_APP_PASSWORD",
                "TRAINER_DATABASE_MIGRATOR_PASSWORD",
                "TRAINER_API_TOKEN",
                "TRAINER_OWNER_VERIFIER_TOKEN",
                "TRAINER_E1RM_BENCH_EXERCISE_IDS",
                "TRAINER_E1RM_BENCH_GRIP_CONDITIONS",
            }:
                raise LifecycleError("private alpha .env has an unexpected or duplicate key")
            values[key] = value.strip().strip("'\"")
        for key in ("TRAINER_ALPHA_API_IMAGE", "TRAINER_ALPHA_POSTGRES_IMAGE"):
            if IMAGE_ID.fullmatch(values.get(key, "")) is None:
                raise LifecycleError("private alpha image ID is missing or invalid")
        secret_keys = (
            "TRAINER_POSTGRES_ADMIN_PASSWORD",
            "TRAINER_DATABASE_APP_PASSWORD",
            "TRAINER_DATABASE_MIGRATOR_PASSWORD",
            "TRAINER_API_TOKEN",
        )
        if any(SECRET.fullmatch(values.get(key, "")) is None for key in secret_keys):
            raise LifecycleError("private alpha credentials are missing or invalid")
        if len({values[key] for key in secret_keys}) != len(secret_keys):
            raise LifecycleError("private alpha credentials must be distinct")
        owner = values.get("TRAINER_OWNER_VERIFIER_TOKEN")
        if owner is not None and (
            SECRET.fullmatch(owner) is None or owner in (values[key] for key in secret_keys)
        ):
            raise LifecycleError("private alpha owner verifier is invalid")
        for key in ("TRAINER_E1RM_BENCH_EXERCISE_IDS", "TRAINER_E1RM_BENCH_GRIP_CONDITIONS"):
            if key in values:
                try:
                    parsed = json.loads(values[key])
                except json.JSONDecodeError as error:
                    raise LifecycleError("private alpha e1RM configuration is invalid") from error
                if not isinstance(parsed, list) or any(
                    not isinstance(item, str) for item in parsed
                ):
                    raise LifecycleError("private alpha e1RM configuration is invalid")
        return values

    def _validate_build_proof(self, local: dict[str, str]) -> None:
        manifest_path = self.root / "PACKAGE_MANIFEST.json"
        proof_path = self.root / "ALPHA_IMAGE.json"
        base_path = self.root / "base_image.json"
        for path in (manifest_path, proof_path, base_path):
            if path.is_symlink() or not path.is_file():
                raise LifecycleError("alpha image proof or package manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            base = json.loads(base_path.read_text(encoding="utf-8"))
            files = manifest["files"]
            operational_files = manifest["operational_files"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise LifecycleError("alpha image proof is unreadable") from error
        if not isinstance(operational_files, dict) or set(operational_files) != OPERATIONAL_FILES:
            raise LifecycleError("alpha operational instruction manifest is incomplete")
        for name, expected in operational_files.items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise LifecycleError("alpha operational instruction path is unsafe")
            path = self.root
            for part in relative.parts:
                path = path / part
                if path.is_symlink():
                    raise LifecycleError("alpha operational instruction path is linked")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise LifecycleError("alpha operational instruction differs from package manifest")
        snapshot = hashlib.sha256(
            json.dumps(
                {
                    "files": files,
                    "operational_files": manifest.get("operational_files"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        operational_files = manifest.get("operational_files")
        if (
            manifest.get("schema_version") != 1
            or not isinstance(operational_files, dict)
            or manifest.get("approved_source_snapshot_sha256") != snapshot
        ):
            raise LifecycleError("alpha approved source snapshot is invalid")
        for name, expected in files.items():
            path = self.root / name
            if (
                path.is_symlink()
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise LifecycleError("alpha package file differs from approved snapshot")
        actual_tree_files: set[str] = set()
        for tree in ("src", "mcp", "migrations", "scripts"):
            directory = self.root / tree
            if directory.is_symlink() or not directory.is_dir():
                raise LifecycleError("alpha build input tree is unsafe")
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise LifecycleError("alpha build input symlink is forbidden")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    actual_tree_files.add(str(path.relative_to(self.root)))
        expected_tree_files = {
            name for name in files if name.startswith(("src/", "mcp/", "migrations/", "scripts/"))
        }
        if actual_tree_files != expected_tree_files:
            raise LifecycleError("alpha build input file set differs from approved snapshot")
        source_paths = [self.root / name for name in ("pyproject.toml", "uv.lock", "alembic.ini")]
        for tree in ("src/trainer", "mcp/trainer_mcp", "migrations"):
            source_paths.extend(
                path
                for path in (self.root / tree).rglob("*")
                if path.is_file()
                and not any(
                    part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
                    for part in path.parts
                )
            )
        source = hashlib.sha256()
        for path in sorted(source_paths):
            source.update(
                str(path.relative_to(self.root)).encode()
                + b"\0"
                + hashlib.sha256(path.read_bytes()).digest()
            )
        if (
            proof.get("image_id") != local["TRAINER_ALPHA_API_IMAGE"]
            or proof.get("source_fingerprint") != source.hexdigest()
            or proof.get("source_file_count") != len(source_paths)
            or proof.get("base_image_id") != base.get("dependency_image_id")
            or proof.get("package_manifest_sha256")
            != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        ):
            raise LifecycleError("alpha image does not match the approved package source")

    def _run(
        self,
        command: list[str],
        *,
        check: bool = False,
        timeout: float = 15,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command[:1] != ["docker"]:
            return super()._run(command, check=check, timeout=timeout, env=env)
        safe = {
            key: os.environ[key] for key in ("PATH", "HOME", "USER", "TMPDIR") if key in os.environ
        }
        if command[:2] == ["docker", "compose"]:
            operation = command[2] if len(command) > 2 else ""
            rest = command[2:]
            if operation == "up":
                rest = [operation, "--pull", "never", "--no-build", *command[3:]]
            elif operation == "run":
                rest = [operation, "--pull", "never", *command[3:]]
            command = [
                "docker",
                "compose",
                "-p",
                self.compose_project,
                "-f",
                str(self.root / "compose.yaml"),
                "--env-file",
                str(self.root / ".env"),
                *rest,
            ]
            safe.update(self._local_environment())
            safe.update(
                {key: value for key, value in (env or {}).items() if key in COMPOSE_ENV_KEYS}
            )
        return super()._run(command, check=check, timeout=timeout, env=safe)

    def _validate_images_and_compose(self) -> None:
        local = self._local_environment()
        api_image_id = local["TRAINER_ALPHA_API_IMAGE"]
        api_result = self._run(
            ["docker", "image", "inspect", "--format", "{{json .}}", api_image_id]
        )
        if api_result.returncode != 0:
            raise LifecycleError("approved alpha API image is not present locally")
        try:
            api_image = json.loads(api_result.stdout)
        except json.JSONDecodeError as error:
            raise LifecycleError("approved alpha API image metadata is invalid") from error
        if (
            not isinstance(api_image, dict)
            or api_image.get("Id") != api_image_id
            or api_image.get("Os") != "linux"
            or api_image.get("Architecture") != "arm64"
        ):
            raise LifecycleError("local alpha API image does not match approved ID and platform")
        self._validate_postgres_provenance(local)
        config = self._run(["docker", "compose", "config", "--format", "json"])
        if config.returncode != 0:
            raise LifecycleError("alpha Compose config cannot be resolved")
        try:
            value = json.loads(config.stdout)
        except json.JSONDecodeError as error:
            raise LifecycleError("alpha Compose config is invalid") from error
        if value.get("name") != self.compose_project or set(value.get("services", {})) != {
            "api",
            "postgres",
        }:
            raise LifecycleError("alpha Compose project or services are unsafe")
        services = value["services"]
        api, postgres = services["api"], services["postgres"]
        for service, key in (
            (api, "TRAINER_ALPHA_API_IMAGE"),
            (postgres, "TRAINER_ALPHA_POSTGRES_IMAGE"),
        ):
            if (
                service.get("image") != local[key]
                or service.get("build")
                or service.get("pull_policy") != "never"
            ):
                raise LifecycleError("alpha Compose image selection is unsafe")
        if api.get("ports") != [
            {
                "mode": "ingress",
                "host_ip": "127.0.0.1",
                "target": 8765,
                "published": "8767",
                "protocol": "tcp",
            }
        ] or postgres.get("ports"):
            raise LifecycleError("alpha Compose host ports are unsafe")
        networks = value.get("networks", {})
        if (
            set(networks) != {"trainer_internal", "trainer_loopback"}
            or networks["trainer_internal"].get("internal") is not True
            or networks["trainer_loopback"].get("internal") not in (None, False)
            or set(api.get("networks", {})) != set(networks)
            or set(postgres.get("networks", {})) != {"trainer_internal"}
        ):
            raise LifecycleError("alpha Compose networks are unsafe")
        if api.get("volumes") or len(postgres.get("volumes", [])) != 1:
            raise LifecycleError("alpha Compose mounts are unsafe")
        volume = postgres["volumes"][0]
        if (
            volume.get("source") != "trainer_postgres_data"
            or volume.get("target") != "/var/lib/postgresql/data"
        ):
            raise LifecycleError("alpha PostgreSQL volume is unsafe")
        if set(value.get("volumes", {})) != {"trainer_postgres_data"}:
            raise LifecycleError("alpha Compose volume set is unsafe")
        if api.get("read_only") is not True or "no-new-privileges:true" not in api.get(
            "security_opt", []
        ):
            raise LifecycleError("alpha API hardening is missing")
        if any(service.get("restart") != "no" for service in services.values()):
            raise LifecycleError("alpha auto-restart is forbidden")

    def _validate_postgres_provenance(self, local: dict[str, str]) -> None:
        pin_path = self.root / "postgres_image.json"
        if pin_path.is_symlink() or not pin_path.is_file():
            raise LifecycleError("approved PostgreSQL provenance pin is missing or unsafe")
        try:
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LifecycleError("approved PostgreSQL provenance pin is unreadable") from error
        if pin != POSTGRES_IMAGE_PIN:
            raise LifecycleError("PostgreSQL provenance pin does not match the approved release")
        if local["TRAINER_ALPHA_POSTGRES_IMAGE"] != pin["image_id"]:
            raise LifecycleError("configured PostgreSQL image does not match the approved provenance pin")
        result = self._run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                pin["image_id"],
            ]
        )
        if result.returncode != 0:
            raise LifecycleError("approved PostgreSQL image is not present locally")
        try:
            image = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise LifecycleError("approved PostgreSQL image metadata is invalid") from error
        if (
            not isinstance(image, dict)
            or image.get("Id") != pin["image_id"]
            or image.get("Os") != pin["platform"]["os"]
            or image.get("Architecture") != pin["platform"]["architecture"]
        ):
            raise LifecycleError("local PostgreSQL image does not match approved ID and platform")

    def _validate_ownership(self) -> set[str]:
        if not self._docker_engine():
            return set()
        result = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={self.compose_project}",
                "--format",
                "{{.ID}}",
            ]
        )
        if result.returncode != 0:
            raise LifecycleError("alpha container inventory is unavailable")
        expected_file = str(self.root / "compose.yaml")
        expected_images = self._local_environment()
        seen: set[str] = set()
        for container_id in result.stdout.splitlines():
            inspected = self._run(["docker", "inspect", container_id])
            if inspected.returncode != 0:
                raise LifecycleError("alpha container inspect failed")
            try:
                item = json.loads(inspected.stdout)[0]
            except (json.JSONDecodeError, IndexError, TypeError) as error:
                raise LifecycleError("alpha container inspect is invalid") from error
            labels = item["Config"]["Labels"]
            service = labels.get("com.docker.compose.service")
            if (
                service not in {"api", "postgres"}
                or labels.get("com.docker.compose.project.config_files") != expected_file
                or service in seen
            ):
                raise LifecycleError("alpha container ownership is ambiguous")
            seen.add(service)
            image_key = (
                "TRAINER_ALPHA_API_IMAGE" if service == "api" else "TRAINER_ALPHA_POSTGRES_IMAGE"
            )
            if item.get("Image") != expected_images[image_key]:
                raise LifecycleError("alpha container image does not match the approved ID")
            bindings = item.get("HostConfig", {}).get("PortBindings") or {}
            if service == "postgres" and bindings:
                raise LifecycleError("alpha PostgreSQL has a host port")
            if service == "api" and bindings != {
                "8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8767"}]
            }:
                raise LifecycleError("alpha API port binding is unsafe")
            mounts = item.get("Mounts", [])
            actual_networks = set(item.get("NetworkSettings", {}).get("Networks", {}))
            expected_networks = (
                {
                    f"{self.compose_project}_trainer_internal",
                    f"{self.compose_project}_trainer_loopback",
                }
                if service == "api"
                else {f"{self.compose_project}_trainer_internal"}
            )
            if actual_networks != expected_networks:
                raise LifecycleError("alpha container network attachment is unsafe")
            if service == "api" and any(
                mount.get("Type") != "tmpfs" or mount.get("Destination") != "/tmp"
                for mount in mounts
            ):
                raise LifecycleError("alpha API container has an unexpected mount")
            if service == "postgres" and (
                len(mounts) != 1
                or mounts[0].get("Name") != f"{self.compose_project}_trainer_postgres_data"
                or mounts[0].get("Destination") != "/var/lib/postgresql/data"
            ):
                raise LifecycleError("alpha PostgreSQL mount is unsafe")
        for kind, item, label in (
            ("volume", "trainer_postgres_data", "com.docker.compose.volume"),
            ("network", "trainer_internal", "com.docker.compose.network"),
            ("network", "trainer_loopback", "com.docker.compose.network"),
        ):
            name = f"{self.compose_project}_{item}"
            listed = self._run(["docker", kind, "ls", "--format", "{{.Name}}"])
            if listed.returncode != 0:
                raise LifecycleError("alpha resource inventory is unavailable")
            if name not in listed.stdout.splitlines():
                continue
            inspected = self._run(["docker", kind, "inspect", "--format", "{{json .Labels}}", name])
            if inspected.returncode != 0:
                raise LifecycleError("alpha resource inspect failed")
            try:
                labels = json.loads(inspected.stdout)
            except json.JSONDecodeError as error:
                raise LifecycleError("alpha resource labels are invalid") from error
            if (
                labels.get("com.docker.compose.project") != self.compose_project
                or labels.get(label) != item
            ):
                raise LifecycleError("alpha resource ownership is ambiguous")
        return seen

    def _mcp_binding(self) -> str:
        token = self._local_environment()["TRAINER_API_TOKEN"]
        return hashlib.sha256(
            f"{self.compose_project}:{token}:{self.api_port}".encode()
        ).hexdigest()

    def _mcp_metadata_fields(self) -> dict[str, str]:
        return {"alpha_project": self.compose_project, "profile_binding": self._mcp_binding()}

    def _mcp_owned(self) -> bool:
        metadata = self._read_mcp_metadata()
        if metadata is None:
            return False
        try:
            binding = self._mcp_binding()
        except LifecycleError:
            return False
        if (
            metadata.get("alpha_project") != self.compose_project
            or metadata.get("project_root") != str(self.root)
            or metadata.get("profile_binding") != binding
        ):
            return False
        return super()._mcp_owned()

    def _load_mcp_environment(self) -> dict[str, str]:
        safe = {
            key: os.environ[key] for key in ("PATH", "HOME", "USER", "TMPDIR") if key in os.environ
        }
        return {
            **safe,
            "TRAINER_API_TOKEN": self._local_environment()["TRAINER_API_TOKEN"],
            "TRAINER_API_URL": f"http://127.0.0.1:{self.api_port}",
            "TRAINER_MCP_TRANSPORT": "streamable-http",
            "TRAINER_MCP_PORT": str(self.mcp_port),
        }

    def _ensure_docker(self) -> None:
        self._validate_workspace(require_environment=True)
        super()._ensure_docker()
        self._validate_images_and_compose()
        self._validate_ownership()
        for port in (self.api_port, self.mcp_port):
            if self._port_pids(port):
                raise LifecycleError("alpha loopback port is already occupied")

    def status(self) -> LifecycleReport:
        self._validate_workspace(require_environment=False)
        report = super().status()
        if report.probes.docker_engine:
            try:
                self._validate_ownership()
                if report.state == "READY":
                    self._validate_build_proof(self._local_environment())
                    self._validate_images_and_compose()
            except LifecycleError as error:
                return LifecycleReport.create(
                    "DEGRADED",
                    report.probes,
                    "alpha ownership or image is ambiguous",
                    (str(error),),
                )
        return report

    def start(self) -> LifecycleReport:
        self._validate_workspace(require_environment=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return super().start()

    def stop(self, *, full: bool = False) -> LifecycleReport:
        if full:
            return LifecycleReport.create(
                "OFF",
                self.snapshot(),
                "alpha full stop is forbidden; preserve other Docker users",
                ("alpha_full_stop_forbidden",),
            )
        self._validate_workspace(require_environment=True)
        owned_services = self._validate_ownership()
        if self._port_pids(self.api_port) and "api" not in owned_services:
            raise LifecycleError("alpha API listener has no owned container")
        return super().stop(full=False)
