from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any

from trainer.lifecycle.model import LifecycleReport
from trainer.lifecycle.system import LifecycleError, LifecycleManager

CANDIDATE_TAG = "personal-ai-trainer-api:h01-45b941c77e618484"
CANDIDATE_ID = "sha256:296a3c71eb7f52c13428d32f418c26a74ff8ff3ad67982128f83b889f8ffd32c"
COMPOSE_ENV_KEYS = frozenset(
    {
        "TRAINER_DATABASE_ADMIN_DSN",
        "TRAINER_DATABASE_APP_DSN",
        "TRAINER_DATABASE_MIGRATOR_DSN",
        "TRAINER_API_HOST",
    }
)


class SyntheticCandidateLifecycleManager(LifecycleManager):
    """The ordinary lifecycle with an isolated, exact-image synthetic profile."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root)
        self.runtime_dir = self.root / "tmp" / "h03-synthetic"
        self.state_path = self.runtime_dir / "state.json"
        self.mcp_path = self.runtime_dir / "mcp.json"
        self.profile_path = self.runtime_dir / "profile.json"
        self.compose_files = (self.root / "compose.yaml", self.root / "compose.h03-synthetic.yaml")

    def _assert_private_runtime(self) -> None:
        root_mode = self.root.stat().st_mode
        if (
            not stat.S_ISDIR(root_mode)
            or self.root.stat().st_uid != os.getuid()
            or root_mode & 0o022
        ):
            raise LifecycleError("synthetic project root is unsafe")
        parent = self.runtime_dir.parent
        if parent.is_symlink() or self.runtime_dir.is_symlink():
            raise LifecycleError("synthetic runtime symlink is forbidden")
        if parent.exists():
            mode = parent.stat().st_mode
            if not stat.S_ISDIR(mode) or parent.stat().st_uid != os.getuid() or mode & 0o022:
                raise LifecycleError("synthetic runtime parent is unsafe")
        if self.runtime_dir.exists():
            mode = self.runtime_dir.stat().st_mode
            if (
                not stat.S_ISDIR(mode)
                or self.runtime_dir.stat().st_uid != os.getuid()
                or mode & 0o077
            ):
                raise LifecycleError("synthetic runtime directory is unsafe")
        for path in (self.profile_path, self.state_path, self.mcp_path):
            if path.is_symlink():
                raise LifecycleError("synthetic runtime file symlink is forbidden")
            if path.exists() and not stat.S_ISREG(path.stat().st_mode):
                raise LifecycleError("synthetic runtime file is not regular")
            if path.exists() and path.stat().st_uid != os.getuid():
                raise LifecycleError("synthetic runtime file owner is unsafe")
            if path.exists() and path.stat().st_mode & 0o077:
                raise LifecycleError("synthetic runtime file permissions are too broad")

    def _profile(self) -> dict[str, str] | None:
        self._assert_private_runtime()
        if not self.profile_path.exists():
            if self.runtime_dir.exists() and any(self.runtime_dir.iterdir()):
                raise LifecycleError("synthetic profile is missing but runtime state remains")
            return None
        try:
            value = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LifecycleError("synthetic profile cannot be read") from error
        if not isinstance(value, dict):
            raise LifecycleError("synthetic profile is invalid")
        required = {
            "project",
            "postgres_image_id",
            "TRAINER_POSTGRES_ADMIN_PASSWORD",
            "TRAINER_DATABASE_APP_PASSWORD",
            "TRAINER_DATABASE_MIGRATOR_PASSWORD",
            "TRAINER_API_TOKEN",
        }
        if set(value) != required or not all(isinstance(v, str) for v in value.values()):
            raise LifecycleError("synthetic profile is incomplete")
        if re.fullmatch(r"trainer-h03-[0-9a-f]{12}", value["project"]) is None:
            raise LifecycleError("synthetic project name is invalid")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value["postgres_image_id"]) is None:
            raise LifecycleError("synthetic PostgreSQL image ID is invalid")
        if any(
            re.fullmatch(r"[0-9a-f]{48}", value[name]) is None
            for name in required - {"project", "postgres_image_id"}
        ):
            raise LifecycleError("synthetic credentials are invalid")
        if self.profile_path.stat().st_mode & 0o077:
            raise LifecycleError("synthetic profile permissions are too broad")
        return value

    def _create_profile(self) -> None:
        if self._profile() is not None:
            return
        if self._port_pids(8765) or self._port_pids(8766):
            raise LifecycleError("synthetic ports are occupied")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.chmod(0o700)
        profile = {
            "project": f"trainer-h03-{secrets.token_hex(6)}",
            "postgres_image_id": self._postgres_image_id_from_tag(),
        }
        for name in (
            "TRAINER_POSTGRES_ADMIN_PASSWORD",
            "TRAINER_DATABASE_APP_PASSWORD",
            "TRAINER_DATABASE_MIGRATOR_PASSWORD",
            "TRAINER_API_TOKEN",
        ):
            profile[name] = secrets.token_hex(24)
        descriptor = os.open(self.profile_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(profile, stream)

    def _local_environment(self) -> dict[str, str]:
        profile = self._profile()
        return (
            {}
            if profile is None
            else {
                key: value
                for key, value in profile.items()
                if key not in {"project", "postgres_image_id"}
            }
        )

    def _runtime_files(self) -> tuple[str, ...]:
        return tuple(name for name in super()._runtime_files() if name != "profile.json")

    def _operation_state(self) -> str | None:
        self._assert_private_runtime()
        if self.state_path.exists():
            try:
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return "PARTIALLY_STOPPED"
            if (
                not isinstance(value, dict)
                or value.get("state") not in ("STARTING", "STOPPING", "PARTIALLY_STOPPED")
                or not isinstance(value.get("errors", []), list)
                or any(not isinstance(item, str) for item in value.get("errors", []))
            ):
                return "PARTIALLY_STOPPED"
        state = super()._operation_state()
        return "PARTIALLY_STOPPED" if self.state_path.exists() and state is None else state

    def _port_pids(self, port: int) -> tuple[int, ...]:
        try:
            result = self._run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LifecycleError("synthetic loopback port inventory is unavailable") from error
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return ()
        if result.returncode != 0:
            raise LifecycleError("synthetic loopback port inventory failed")
        values = result.stdout.splitlines()
        if any(not item.strip().isdigit() for item in values):
            raise LifecycleError("synthetic loopback port inventory is invalid")
        return tuple(int(item) for item in values)

    def _compose_command(self, command: list[str]) -> list[str]:
        profile = self._profile()
        if profile is None:
            raise LifecycleError("synthetic profile is missing")
        result = ["docker", "compose", "-p", profile["project"]]
        for path in self.compose_files:
            result.extend(("-f", str(path)))
        return [*result, "--env-file", "/dev/null", *command[2:]]

    def _run(
        self,
        command: list[str],
        *,
        check: bool = False,
        timeout: float = 15,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        safe_env = {
            key: os.environ[key] for key in ("PATH", "HOME", "USER", "TMPDIR") if key in os.environ
        }
        if command[:2] == ["docker", "compose"]:
            if command[2:3] == ["up"]:
                command = [*command[:3], "--pull", "never", "--no-build", *command[3:]]
            if command[2:3] == ["run"]:
                command = [*command[:3], "--pull", "never", *command[3:]]
            command = self._compose_command(command)
            safe_env.update(
                {key: value for key, value in (env or {}).items() if key in COMPOSE_ENV_KEYS}
            )
            safe_env.update(
                {
                    **self._local_environment(),
                    "TRAINER_H03_IMAGE": CANDIDATE_ID,
                    "TRAINER_H03_POSTGRES_IMAGE": self._profile_required()["postgres_image_id"],
                    "COMPOSE_PULL_POLICY": "never",
                }
            )
        if command[:1] == ["docker"]:
            self._assert_local_docker_context(safe_env)
            env = safe_env
        return super()._run(command, check=check, timeout=timeout, env=env)

    def _assert_local_docker_context(self, safe_env: dict[str, str]) -> None:
        context = super()._run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            env=safe_env,
            timeout=5,
        )
        if context.returncode != 0:
            raise LifecycleError("Docker context cannot be inspected")
        endpoint = context.stdout.strip()
        allowed = {
            "unix:///var/run/docker.sock",
            f"unix://{Path.home() / '.docker' / 'run' / 'docker.sock'}",
        }
        if endpoint not in allowed:
            raise LifecycleError("Docker context is not the local Desktop socket")

    def _profile_required(self) -> dict[str, str]:
        profile = self._profile()
        if profile is None:
            raise LifecycleError("synthetic profile is missing")
        return profile

    def _postgres_image_id_from_tag(self) -> str:
        result = self._run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", "postgres:17.6-bookworm"]
        )
        image_id = result.stdout.strip()
        if result.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise LifecycleError("local PostgreSQL 17.6 image ID is unavailable")
        return image_id

    def _write_state(self, state: str, errors: list[str] | None = None) -> None:
        self._assert_private_runtime()
        super()._write_state(state, errors)
        self.state_path.chmod(0o600)

    def _read_mcp_metadata(self) -> dict[str, Any] | None:
        self._assert_private_runtime()
        return super()._read_mcp_metadata()

    def _mcp_owned(self) -> bool:
        profile = self._profile()
        metadata = self._read_mcp_metadata()
        if profile is None or metadata is None:
            return False
        if (
            metadata.get("synthetic_project") != profile["project"]
            or metadata.get("api_url") != "http://127.0.0.1:8765"
            or metadata.get("profile_binding") != self._mcp_binding(profile)
        ):
            return False
        return super()._mcp_owned()

    def _mcp_binding(self, profile: dict[str, str]) -> str:
        material = f"{profile['project']}:{profile['TRAINER_API_TOKEN']}:127.0.0.1:8765"
        return hashlib.sha256(material.encode()).hexdigest()

    def _mcp_metadata_fields(self) -> dict[str, str]:
        profile = self._profile()
        if profile is None:
            raise LifecycleError("synthetic profile is missing for MCP")
        return {
            "synthetic_project": profile["project"],
            "api_url": "http://127.0.0.1:8765",
            "profile_binding": self._mcp_binding(profile),
        }

    def _load_mcp_environment(self) -> dict[str, str]:
        profile = self._profile()
        if profile is None:
            raise LifecycleError("synthetic profile is missing for MCP")
        safe = {
            key: os.environ[key] for key in ("PATH", "HOME", "USER", "TMPDIR") if key in os.environ
        }
        return {
            **safe,
            "TRAINER_API_TOKEN": profile["TRAINER_API_TOKEN"],
            "TRAINER_API_URL": "http://127.0.0.1:8765",
            "TRAINER_MCP_TRANSPORT": "streamable-http",
            "TRAINER_MCP_PORT": "8766",
        }

    def _start_mcp(self) -> None:
        self._assert_private_runtime()
        super()._start_mcp()
        self.mcp_path.chmod(0o600)

    def _project_containers(self) -> tuple[str, ...]:
        profile = self._profile()
        if profile is None:
            if self._docker_engine():
                self._scan_orphans()
            return ()
        if not self._docker_engine():
            return ()
        result = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={profile['project']}",
                "--format",
                "{{.ID}}:{{.State}}:{{.Names}}",
            ]
        )
        if result.returncode != 0:
            raise LifecycleError("synthetic container inventory is unavailable")
        return tuple(line for line in result.stdout.splitlines() if line)

    def _scan_orphans(self) -> None:
        inventories = (
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            ["docker", "network", "ls", "--format", "{{.Name}}"],
        )
        for command in inventories:
            result = self._run(command)
            if result.returncode != 0:
                raise LifecycleError("synthetic resource inventory is unavailable")
            if any(name.startswith("trainer-h03-") for name in result.stdout.splitlines()):
                raise LifecycleError("orphan synthetic resources exist without profile")

    def _validate_ownership(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        expected = ",".join(str(path) for path in self.compose_files)
        for item in self._project_containers():
            container_id = item.split(":", 1)[0]
            result = self._run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
                    container_id,
                ]
            )
            if result.returncode != 0 or result.stdout.strip() != expected:
                raise LifecycleError("synthetic Compose ownership is ambiguous")

    def _validate_resources(self, *, require_all: bool = False) -> None:
        profile = self._profile()
        if profile is None or not self._docker_engine():
            return
        project = profile["project"]
        resources = (
            (
                "volume",
                f"{project}_trainer_postgres_data",
                "com.docker.compose.volume",
                "trainer_postgres_data",
            ),
            (
                "network",
                f"{project}_trainer_internal",
                "com.docker.compose.network",
                "trainer_internal",
            ),
            (
                "network",
                f"{project}_trainer_loopback",
                "com.docker.compose.network",
                "trainer_loopback",
            ),
        )
        for kind, name, key, expected in resources:
            inventory = self._run(["docker", kind, "ls", "--format", "{{.Name}}"])
            if inventory.returncode != 0:
                raise LifecycleError("synthetic resource inventory is unavailable")
            if name not in inventory.stdout.splitlines():
                if require_all:
                    raise LifecycleError("synthetic required resource is missing")
                continue
            inspected = self._run(["docker", kind, "inspect", "--format", "{{json .Labels}}", name])
            if inspected.returncode != 0:
                raise LifecycleError("synthetic resource inspect failed")
            try:
                labels = json.loads(inspected.stdout)
            except json.JSONDecodeError as error:
                raise LifecycleError("synthetic resource labels are invalid") from error
            if (
                not isinstance(labels, dict)
                or labels.get("com.docker.compose.project") != project
                or labels.get(key) != expected
            ):
                raise LifecycleError("synthetic resource ownership is ambiguous")

    def _resources_absent(self, project: str) -> bool:
        if self._project_containers():
            return False
        for kind in ("volume", "network"):
            result = self._run(["docker", kind, "ls", "--format", "{{.Name}}"])
            if result.returncode != 0:
                raise LifecycleError("synthetic resource inventory is unavailable")
            if any(name.startswith(f"{project}_") for name in result.stdout.splitlines()):
                return False
        return True

    def _volume_exists(self) -> bool | None:
        if not self._docker_engine():
            return None
        profile = self._profile()
        if profile is None:
            return False
        result = self._run(
            ["docker", "volume", "inspect", f"{profile['project']}_trainer_postgres_data"]
        )
        return result.returncode == 0

    def status(self) -> LifecycleReport:
        report = super().status()
        try:
            self._validate_ownership()
            if self._profile() is not None and report.probes.docker_engine:
                self._validate_ports_ownership()
                self._validate_resources(require_all=report.state == "READY")
                if report.state == "READY":
                    self._verify_candidate()
                    self._verify_running_api()
                    self._validate_postgres_runtime()
                    self._validate_runtime_bindings()
        except (LifecycleError, KeyError, TypeError, ValueError) as error:
            return LifecycleReport.create(
                "DEGRADED", report.probes, "synthetic ownership is ambiguous", (str(error),)
            )
        if (
            self._profile() is None
            and not report.probes.project_containers
            and not report.probes.api_listener
            and not report.probes.mcp_listener
        ):
            state = (
                "OFF"
                if report.probes.docker_engine
                else "FULLY_OFF"
                if report.probes.docker_desktop_stopped
                else report.state
            )
            return LifecycleReport.create(state, report.probes, f"SYNTHETIC PROJECT IS {state}")
        return report

    def _verify_candidate(self) -> None:
        result = self._run(["docker", "image", "inspect", "--format", "{{.Id}}", CANDIDATE_TAG])
        if result.returncode != 0 or result.stdout.strip() != CANDIDATE_ID:
            raise LifecycleError("exact candidate image ID is unavailable")
        user = self._run(
            ["docker", "image", "inspect", "--format", "{{.Config.User}}", CANDIDATE_ID]
        )
        if user.returncode != 0 or user.stdout.strip() != "trainer":
            raise LifecycleError("candidate API image non-root user is unproven")
        profile = self._profile_required()
        if self._postgres_image_id_from_tag() != profile["postgres_image_id"]:
            raise LifecycleError("PostgreSQL tag no longer resolves to pinned image ID")
        config = self._run(["docker", "compose", "config", "--format", "json"])
        if config.returncode != 0:
            raise LifecycleError("synthetic Compose config failed")
        value: dict[str, Any] = json.loads(config.stdout)
        services = value["services"]
        if set(services) != {"postgres", "api"}:
            raise LifecycleError("unexpected synthetic Compose services")
        api = services["api"]
        if api.get("image") != CANDIDATE_ID or api.get("build"):
            raise LifecycleError("synthetic API image selection is unsafe")
        postgres = services["postgres"]
        if postgres.get("image") != profile["postgres_image_id"] or postgres.get("build"):
            raise LifecycleError("synthetic PostgreSQL image selection is unsafe")
        if api.get("ports") != [
            {
                "mode": "ingress",
                "host_ip": "127.0.0.1",
                "target": 8765,
                "published": "8765",
                "protocol": "tcp",
            }
        ]:
            raise LifecycleError("synthetic API port is not loopback-only")
        if postgres.get("ports"):
            raise LifecycleError("synthetic PostgreSQL host port is forbidden")
        networks = value.get("networks", {})
        if (
            set(networks) != {"trainer_internal", "trainer_loopback"}
            or networks["trainer_internal"].get("internal") is not True
            or networks["trainer_loopback"].get("internal") not in (None, False)
        ):
            raise LifecycleError("synthetic network boundary is unsafe")
        if set(api.get("networks", {})) != set(networks) or set(postgres.get("networks", {})) != {
            "trainer_internal"
        }:
            raise LifecycleError("synthetic network membership is unsafe")
        if api.get("volumes"):
            raise LifecycleError("synthetic API mounts are forbidden")
        volumes = postgres.get("volumes", [])
        if (
            len(volumes) != 1
            or volumes[0].get("type") != "volume"
            or volumes[0].get("source") != "trainer_postgres_data"
            or volumes[0].get("target") != "/var/lib/postgresql/data"
        ):
            raise LifecycleError("synthetic PostgreSQL mount is unsafe")
        if set(value.get("volumes", {})) != {"trainer_postgres_data"}:
            raise LifecycleError("synthetic volume set is unsafe")
        bounds = {"api": (1.0, 384 * 1024 * 1024, 128), "postgres": (1.5, 768 * 1024 * 1024, 256)}
        for name, (cpu_max, memory_max, pid_max) in bounds.items():
            service = services[name]
            try:
                cpu = float(service["cpus"])
                memory = int(service["mem_limit"])
                pids = int(service["pids_limit"])
            except (KeyError, ValueError, TypeError) as error:
                raise LifecycleError("synthetic resource bounds are missing") from error
            if not (0 < cpu <= cpu_max and 0 < memory <= memory_max and 0 < pids <= pid_max):
                raise LifecycleError("synthetic resource bounds are unsafe")
        if any(service.get("restart") != "no" for service in services.values()):
            raise LifecycleError("synthetic auto-restart is forbidden")
        if api.get("read_only") is not True or "no-new-privileges:true" not in api.get(
            "security_opt", []
        ):
            raise LifecycleError("synthetic API hardening is missing")
        service_user = str(api.get("user", "")).split(":", 1)[0].lower()
        if service_user == "root" or (service_user.isdigit() and int(service_user) == 0):
            raise LifecycleError("synthetic API root user is forbidden")
        if api.get("user") not in (None, "trainer"):
            raise LifecycleError("synthetic API user differs from the verified image user")

    def _ensure_docker(self) -> None:
        super()._ensure_docker()
        primary = self._run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=personal-ai-trainer",
            ]
        )
        if primary.returncode != 0 or primary.stdout.strip():
            raise LifecycleError("primary project containers exist or inventory is unavailable")
        self._validate_ports_ownership()
        self._validate_ownership()
        self._validate_resources()
        self._verify_candidate()

    def _validate_ports_ownership(self) -> None:
        profile = self._profile()
        if profile is None:
            raise LifecycleError("synthetic profile is missing")
        api = self._run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={profile['project']}",
                "--filter",
                "label=com.docker.compose.service=api",
            ]
        )
        if api.returncode != 0:
            raise LifecycleError("synthetic API inventory is unavailable")
        if (self._port_pids(8765) and not api.stdout.strip()) or (
            self._port_pids(8766) and not self._mcp_owned()
        ):
            raise LifecycleError("synthetic loopback port is occupied by another process")

    def start(self) -> LifecycleReport:
        with self._lock():
            return self._start_synthetic_locked()

    def _start_synthetic_locked(self) -> LifecycleReport:
        if self.state_path.exists() and self._operation_state() == "PARTIALLY_STOPPED":
            raise LifecycleError("synthetic transition state requires stop before restart")
        LifecycleManager._ensure_docker(self)
        if self._profile() is None:
            self._scan_orphans()
        self._create_profile()
        report = super().start()
        if report.state != "READY":
            return report
        try:
            self._verify_running_api()
        except LifecycleError:
            cleanup = self.stop()
            return LifecycleReport.create(
                "DEGRADED", cleanup.probes, "running API image ID mismatch", ("image_mismatch",)
            )
        return report

    def _verify_running_api(self) -> None:
        profile = self._profile()
        if profile is None:
            raise LifecycleError("synthetic profile is missing")
        api = self._run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={profile['project']}",
                "--filter",
                "label=com.docker.compose.service=api",
            ]
        )
        ids = api.stdout.split()
        if api.returncode != 0 or len(ids) != 1:
            raise LifecycleError("synthetic API ownership is ambiguous")
        image = self._run(["docker", "inspect", "--format", "{{.Image}}", ids[0]])
        if image.returncode != 0 or image.stdout.strip() != CANDIDATE_ID:
            raise LifecycleError("running API image ID mismatch")
        user = self._run(["docker", "inspect", "--format", "{{.Config.User}}", ids[0]])
        if user.returncode != 0 or user.stdout.strip() != "trainer":
            raise LifecycleError("running API non-root user is unproven")

    def _validate_postgres_runtime(self) -> None:
        profile = self._profile_required()
        postgres = self._run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={profile['project']}",
                "--filter",
                "label=com.docker.compose.service=postgres",
            ]
        )
        ids = postgres.stdout.split()
        if postgres.returncode != 0 or len(ids) != 1:
            raise LifecycleError("synthetic PostgreSQL container ownership is ambiguous")
        image = self._run(["docker", "inspect", "--format", "{{.Image}}", ids[0]])
        if image.returncode != 0 or image.stdout.strip() != profile["postgres_image_id"]:
            raise LifecycleError("running PostgreSQL image ID mismatch")

    def _validate_runtime_bindings(self) -> None:
        profile = self._profile_required()
        project = profile["project"]
        expected_networks = {
            "api": {f"{project}_trainer_internal", f"{project}_trainer_loopback"},
            "postgres": {f"{project}_trainer_internal"},
        }
        for service in ("api", "postgres"):
            listed = self._run(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    f"label=com.docker.compose.service={service}",
                ]
            )
            ids = listed.stdout.split()
            if listed.returncode != 0 or len(ids) != 1:
                raise LifecycleError("synthetic runtime binding owner is ambiguous")
            networks = self._run(
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", ids[0]]
            )
            mounts = self._run(["docker", "inspect", "--format", "{{json .Mounts}}", ids[0]])
            if networks.returncode != 0 or mounts.returncode != 0:
                raise LifecycleError("synthetic runtime bindings cannot be inspected")
            try:
                network_map = json.loads(networks.stdout)
                mount_list = json.loads(mounts.stdout)
            except json.JSONDecodeError as error:
                raise LifecycleError("synthetic runtime bindings are invalid") from error
            if not isinstance(network_map, dict) or set(network_map) != expected_networks[service]:
                raise LifecycleError("synthetic runtime network binding is unsafe")
            if service == "api" and (not isinstance(mount_list, list) or mount_list):
                raise LifecycleError("synthetic API runtime mounts are forbidden")
            if service == "postgres":
                expected_name = f"{project}_trainer_postgres_data"
                if (
                    not isinstance(mount_list, list)
                    or len(mount_list) != 1
                    or mount_list[0].get("Type") != "volume"
                    or mount_list[0].get("Name") != expected_name
                    or mount_list[0].get("Destination") != "/var/lib/postgresql/data"
                ):
                    raise LifecycleError("synthetic PostgreSQL runtime mount is unsafe")

    def stop(self, *, full: bool = False) -> LifecycleReport:
        with self._lock():
            return self._stop_synthetic_locked(full=full)

    def _stop_synthetic_locked(self, *, full: bool) -> LifecycleReport:
        if full:
            return LifecycleReport.create(
                "OFF",
                self.snapshot(),
                "stop synthetic first, then use ordinary trainer stop --full",
                ("synthetic_full_stop_requires_separate_command",),
            )
        profile = self._profile()
        if profile is None:
            return self.status()
        self._validate_ownership()
        self._validate_resources()
        self._validate_ports_ownership()
        report = super().stop(full=False)
        if report.state not in {"OFF", "FULLY_OFF"}:
            if (
                report.probes.project_containers
                or report.probes.api_listener
                or report.probes.mcp_listener
                or self._read_mcp_metadata() is not None
                or not self._resources_absent(profile["project"])
            ):
                return report
            self.state_path.unlink(missing_ok=True)
        if self._docker_engine():
            volume = f"{profile['project']}_trainer_postgres_data"
            if (
                self._volume_exists()
                and self._run(["docker", "volume", "rm", volume]).returncode != 0
            ):
                return LifecycleReport.create(
                    "PARTIALLY_STOPPED",
                    self.snapshot(),
                    "synthetic volume cleanup failed",
                    ("volume_cleanup_failed",),
                )
            if not self._resources_absent(profile["project"]):
                return LifecycleReport.create(
                    "PARTIALLY_STOPPED",
                    self.snapshot(),
                    "synthetic resources remain",
                    ("resource_cleanup_incomplete",),
                )
        self.profile_path.unlink(missing_ok=True)
        if self.runtime_dir.exists() and not any(self.runtime_dir.iterdir()):
            self.runtime_dir.rmdir()
        return self.status()
