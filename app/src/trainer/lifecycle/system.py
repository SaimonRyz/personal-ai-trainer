from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from trainer.lifecycle.model import LifecycleReport, ProbeSnapshot, classify

COMPOSE_PROJECT = "personal-ai-trainer"
API_PORT = 8765
MCP_PORT = 8766


class LifecycleError(RuntimeError):
    pass


def find_project_root() -> Path:
    candidates = (Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[3])
    for candidate in candidates:
        if (candidate / "compose.yaml").is_file() and (candidate / "AGENTS.md").is_file():
            return candidate
    raise LifecycleError("project root with compose.yaml and AGENTS.md was not found")


class LifecycleManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or find_project_root()).resolve()
        self.runtime_dir = self.root / "tmp" / "lifecycle"
        self.lock_path = self.root / "tmp" / "trainer-lifecycle.lock"
        self.state_path = self.runtime_dir / "state.json"
        self.mcp_path = self.runtime_dir / "mcp.json"
        self.compose_project = COMPOSE_PROJECT
        self.api_port = API_PORT
        self.mcp_port = MCP_PORT
        self._lock_owner: int | None = None
        self._lock_depth = 0

    def _run(
        self,
        command: list[str],
        *,
        check: bool = False,
        timeout: float = 15,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
            env=env,
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        thread_id = threading.get_ident()
        if self._lock_owner == thread_id:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        root_info = self.root.stat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.getuid()
            or root_info.st_mode & 0o022
        ):
            raise LifecycleError("project root is unsafe for lifecycle lock")
        lock_parent = self.lock_path.parent
        if lock_parent.is_symlink():
            raise LifecycleError("lifecycle lock parent symlink is forbidden")
        if not lock_parent.exists():
            with suppress(FileExistsError):
                lock_parent.mkdir(mode=0o700)
        parent_info = lock_parent.stat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.getuid()
            or parent_info.st_mode & 0o022
        ):
            raise LifecycleError("lifecycle lock parent is unsafe")
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock_file:
            info = os.fstat(lock_file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise LifecycleError("lifecycle lock ownership is unsafe")
            os.fchmod(lock_file.fileno(), 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._lock_owner = thread_id
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_owner = None
                self._lock_depth = 0
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_state(self, state: str, errors: list[str] | None = None) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        signature = self._process_signature(os.getpid())
        self._write_private_json(
            self.state_path,
            {
                "state": state,
                "errors": errors or [],
                "operation_pid": os.getpid(),
                "operation_started_signature": signature[1] if signature else None,
            },
        )

    def _write_private_json(self, path: Path, value: dict[str, Any]) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, separators=(",", ":"))

    def _lock_is_held(self) -> bool:
        if not self.lock_path.exists():
            return False
        try:
            with self.lock_path.open("r+", encoding="utf-8") as lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            return False
        return False

    def _operation_state(self) -> str | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        state = value.get("state")
        if state == "PARTIALLY_STOPPED":
            return "PARTIALLY_STOPPED"
        if state not in {"STARTING", "STOPPING"}:
            return None
        pid = value.get("operation_pid")
        signature = self._process_signature(pid) if isinstance(pid, int) else None
        if (
            signature is not None
            and signature[1] == value.get("operation_started_signature")
            and self._lock_is_held()
        ):
            return str(state)
        return "PARTIALLY_STOPPED"

    def _docker_engine(self) -> bool:
        try:
            return self._run(["docker", "info"], timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _docker_desktop_stopped(self) -> bool:
        try:
            result = self._run(["docker", "desktop", "status"], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None:
            output = f"{result.stdout}\n{result.stderr}".lower()
            if result.returncode == 0 and "stopped" in output:
                return True
        try:
            desktop = self._run(["pgrep", "-x", "Docker Desktop"], timeout=3)
            backend = self._run(["pgrep", "-x", "com.docker.backend"], timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return desktop.returncode != 0 and backend.returncode != 0

    def _project_containers(self) -> tuple[str, ...]:
        if not self._docker_engine():
            return ()
        result = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={self.compose_project}",
                "--filter",
                f"label=com.docker.compose.project.config_files={self.root / 'compose.yaml'}",
                "--format",
                "{{.ID}}:{{.State}}:{{.Names}}",
            ]
        )
        return tuple(line for line in result.stdout.splitlines() if line)

    def _volume_exists(self) -> bool | None:
        if not self._docker_engine():
            return None
        result = self._run(
            ["docker", "volume", "inspect", f"{self.compose_project}_trainer_postgres_data"]
        )
        return result.returncode == 0

    def _port_pids(self, port: int) -> tuple[int, ...]:
        try:
            result = self._run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return ()
        return tuple(int(line) for line in result.stdout.splitlines() if line.strip().isdigit())

    def _read_mcp_metadata(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _process_signature(self, pid: int) -> tuple[str, str] | None:
        try:
            command = self._run(["ps", "-p", str(pid), "-o", "command="], timeout=3)
            started = self._run(["ps", "-p", str(pid), "-o", "lstart="], timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if command.returncode != 0 or started.returncode != 0:
            return None
        return command.stdout.strip(), started.stdout.strip()

    def _mcp_owned(self) -> bool:
        metadata = self._read_mcp_metadata()
        if metadata is None:
            return False
        pid = metadata.get("pid")
        if not isinstance(pid, int):
            return False
        signature = self._process_signature(pid)
        if signature is None:
            return False
        command, started = signature
        return (
            "trainer-mcp" in command
            and metadata.get("started_signature") == started
            and pid in self._port_pids(self.mcp_port)
        )

    def _api_ready(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.api_port}/ready", timeout=2
            ) as response:
                return int(response.status) == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _runtime_files(self) -> tuple[str, ...]:
        if not self.runtime_dir.exists():
            return ()
        return tuple(
            str(path.relative_to(self.runtime_dir))
            for path in sorted(self.runtime_dir.rglob("*"))
            if path.is_file()
        )

    def snapshot(self) -> ProbeSnapshot:
        docker_engine = self._docker_engine()
        desktop_stopped = False if docker_engine else self._docker_desktop_stopped()
        return ProbeSnapshot(
            docker_engine=docker_engine,
            docker_desktop_stopped=desktop_stopped,
            project_containers=self._project_containers() if docker_engine else (),
            mcp_owned=self._mcp_owned(),
            mcp_listener=bool(self._port_pids(self.mcp_port)),
            api_listener=bool(self._port_pids(self.api_port)),
            api_ready=self._api_ready(),
            runtime_files=self._runtime_files(),
            volume_exists=self._volume_exists() if docker_engine else None,
            operation_state=self._operation_state(),
            limitations=("volume_unverified_engine_off",) if not docker_engine else (),
        )

    def status(self) -> LifecycleReport:
        snapshot = self.snapshot()
        state = classify(snapshot)
        return LifecycleReport.create(state, snapshot, f"PROJECT IS {state}")

    def _wait_for(self, predicate: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return bool(predicate())

    def _ensure_docker(self) -> None:
        if self._docker_engine():
            return
        result = self._run(["docker", "desktop", "start"], timeout=30)
        if result.returncode != 0 or not self._wait_for(self._docker_engine, 30):
            raise LifecycleError("Docker Desktop/Engine did not become available")

    def _load_mcp_environment(self) -> dict[str, str]:
        token = self._local_api_token()
        if not token or len(token) < 32:
            raise LifecycleError("TRAINER_API_TOKEN is missing or weak in local .env")
        environment = os.environ.copy()
        environment.update(
            {
                "TRAINER_API_TOKEN": token,
                "TRAINER_API_URL": f"http://127.0.0.1:{self.api_port}",
                "TRAINER_MCP_TRANSPORT": "streamable-http",
                "TRAINER_MCP_PORT": str(self.mcp_port),
            }
        )
        return environment

    def _local_environment(self) -> dict[str, str]:
        values: dict[str, str] = {}
        try:
            lines = (self.root / ".env").read_text(encoding="utf-8").splitlines()
        except OSError:
            return values
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", maxsplit=1)
            values[key.strip()] = value.strip().strip("'\"")
        return values

    def _local_api_token(self) -> str | None:
        return self._local_environment().get("TRAINER_API_TOKEN")

    def _database_setup_environment(self) -> dict[str, str]:
        local = self._local_environment()
        required = (
            "TRAINER_POSTGRES_ADMIN_PASSWORD",
            "TRAINER_DATABASE_APP_PASSWORD",
            "TRAINER_DATABASE_MIGRATOR_PASSWORD",
            "TRAINER_API_TOKEN",
        )
        missing = [name for name in required if len(local.get(name, "")) < 20]
        if missing:
            raise LifecycleError("required local database configuration is missing or weak")
        admin = urllib.parse.quote(local["TRAINER_POSTGRES_ADMIN_PASSWORD"], safe="")
        app = urllib.parse.quote(local["TRAINER_DATABASE_APP_PASSWORD"], safe="")
        migrator = urllib.parse.quote(local["TRAINER_DATABASE_MIGRATOR_PASSWORD"], safe="")
        environment = os.environ.copy()
        environment.update(local)
        environment.update(
            {
                "TRAINER_DATABASE_ADMIN_DSN": (
                    f"postgresql://trainer_admin:{admin}@postgres:5432/trainer"
                ),
                "TRAINER_DATABASE_MIGRATOR_DSN": (
                    f"postgresql+psycopg://trainer_migrator:{migrator}@postgres:5432/trainer"
                ),
                "TRAINER_DATABASE_APP_DSN": (
                    f"postgresql+psycopg://trainer_app:{app}@postgres:5432/trainer"
                ),
                "TRAINER_API_HOST": "127.0.0.1",
            }
        )
        return environment

    def _prepare_database(self) -> None:
        environment = self._database_setup_environment()
        start_postgres = self._run(["docker", "compose", "up", "-d", "postgres"], timeout=60)
        if start_postgres.returncode != 0:
            raise LifecycleError("PostgreSQL Compose start failed")

        def postgres_ready() -> bool:
            result = self._run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_isready",
                    "-U",
                    "trainer_admin",
                    "-d",
                    "trainer",
                ],
                timeout=5,
            )
            return result.returncode == 0

        if not self._wait_for(postgres_ready, 30):
            raise LifecycleError("PostgreSQL did not become ready")
        self._validate_postgres_runtime()
        base = ["docker", "compose", "run", "--rm", "--no-deps"]
        steps = (
            (
                [
                    *base,
                    "-e",
                    "TRAINER_DATABASE_ADMIN_DSN",
                    "-e",
                    "TRAINER_DATABASE_APP_PASSWORD",
                    "-e",
                    "TRAINER_DATABASE_MIGRATOR_PASSWORD",
                    "api",
                    "trainer-db-bootstrap",
                ],
                "database bootstrap",
            ),
            (
                [*base, "-e", "TRAINER_DATABASE_MIGRATOR_DSN", "api", "trainer-migrate"],
                "database migration",
            ),
            (
                [
                    *base,
                    "-e",
                    "TRAINER_API_TOKEN",
                    "-e",
                    "TRAINER_DATABASE_APP_DSN",
                    "-e",
                    "TRAINER_API_HOST",
                    "api",
                    "trainer-db-verify",
                ],
                "database boundary verification",
            ),
        )
        for command, description in steps:
            result = self._run(command, timeout=120, env=environment)
            if result.returncode != 0:
                raise LifecycleError(f"{description} failed")

    def _validate_postgres_runtime(self) -> None:
        return

    def _establish_stop_barrier(self) -> list[str]:
        if not self._port_pids(self.api_port):
            return []
        token = self._local_api_token()
        if token:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.api_port}/internal/lifecycle/drain",
                method="POST",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    if int(response.status) == 204:
                        return []
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
        if self._docker_engine():
            fallback = self._run(["docker", "compose", "stop", "api"], timeout=30)
            if fallback.returncode == 0 and self._wait_for(
                lambda: not self._port_pids(self.api_port), 10
            ):
                return []
        return ["API stop barrier could not be established"]

    def _start_mcp(self) -> None:
        if self._mcp_owned():
            return
        if self._port_pids(self.mcp_port):
            raise LifecycleError("MCP port is occupied by a process not owned by this project")
        executable = Path(sys.executable).parent / "trainer-mcp"
        if not executable.is_file():
            raise LifecycleError("trainer-mcp executable was not found in the active environment")
        process = subprocess.Popen(
            [str(executable)],
            cwd=self.root,
            env=self._load_mcp_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if not self._wait_for(lambda: process.pid in self._port_pids(self.mcp_port), 10):
            process.terminate()
            raise LifecycleError("host-side MCP did not open its loopback listener")
        signature = self._process_signature(process.pid)
        if signature is None:
            process.terminate()
            raise LifecycleError("host-side MCP ownership signature is unavailable")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._write_private_json(
                self.mcp_path,
                {
                    "pid": process.pid,
                    "started_signature": signature[1],
                    "port": self.mcp_port,
                    "project_root": str(self.root),
                    **self._mcp_metadata_fields(),
                },
            )
        except Exception:
            process.terminate()
            process.wait(timeout=5)
            raise

    def _mcp_metadata_fields(self) -> dict[str, str]:
        return {}

    def start(self) -> LifecycleReport:
        with self._lock():
            if self._foreign_synthetic_profile_exists():
                snapshot = self.snapshot()
                return LifecycleReport.create(
                    "PARTIALLY_STOPPED",
                    snapshot,
                    "synthetic profile is active",
                    ("use --synthetic-candidate for this runtime",),
                )
            existing = self.status()
            if existing.state == "READY":
                return existing
            self._write_state("STARTING")
            try:
                self._ensure_docker()
                self._prepare_database()
                compose = self._run(
                    ["docker", "compose", "up", "-d", "--remove-orphans"], timeout=60
                )
                if compose.returncode != 0:
                    raise LifecycleError("Docker Compose start failed")
                if not self._wait_for(self._api_ready, 30):
                    raise LifecycleError("API did not reach ready state")
                self._start_mcp()
                self.state_path.unlink(missing_ok=True)
                report = self.status()
                if report.state != "READY":
                    raise LifecycleError(f"start ended in {report.state}")
                return report
            except Exception as error:
                self._write_state("PARTIALLY_STOPPED", [str(error)])
                snapshot = self.snapshot()
                return LifecycleReport.create(
                    "PARTIALLY_STOPPED", snapshot, "project start failed safely", (str(error),)
                )

    def _stop_owned_mcp(self) -> list[str]:
        metadata = self._read_mcp_metadata()
        if metadata is None:
            if self._port_pids(self.mcp_port):
                return ["MCP listener exists without project ownership metadata"]
            return []
        if not self._mcp_owned():
            pid = metadata.get("pid")
            process_absent = not isinstance(pid, int) or self._process_signature(pid) is None
            if process_absent and not self._port_pids(self.mcp_port):
                self.mcp_path.unlink(missing_ok=True)
                return []
            return ["MCP metadata exists but ownership cannot be proven"]
        pid = int(metadata["pid"])
        os.kill(pid, signal.SIGTERM)
        if not self._wait_for(lambda: self._process_signature(pid) is None, 5):
            if not self._mcp_owned():
                return ["MCP ownership changed while stopping; SIGKILL refused"]
            os.kill(pid, signal.SIGKILL)
            if not self._wait_for(lambda: self._process_signature(pid) is None, 3):
                return ["project MCP did not stop"]
        self.mcp_path.unlink(missing_ok=True)
        return []

    def stop(self, *, full: bool = False) -> LifecycleReport:
        with self._lock():
            if self._foreign_synthetic_profile_exists():
                snapshot = self.snapshot()
                return LifecycleReport.create(
                    "PARTIALLY_STOPPED",
                    snapshot,
                    "synthetic profile is active",
                    ("use --synthetic-candidate for this runtime",),
                )
            self._write_state("STOPPING")
            errors = self._establish_stop_barrier()
            errors.extend(self._stop_owned_mcp())
            if self._docker_engine():
                compose = self._run(["docker", "compose", "down", "--remove-orphans"], timeout=60)
                if compose.returncode != 0:
                    errors.append("Docker Compose stop failed")
            elif not self._docker_desktop_stopped():
                errors.append("Docker state is unavailable")

            if not errors:
                self.state_path.unlink(missing_ok=True)
                if self.runtime_dir.exists() and not any(self.runtime_dir.iterdir()):
                    self.runtime_dir.rmdir()
            snapshot = self.snapshot()
            if (
                errors
                or snapshot.project_containers
                or snapshot.mcp_listener
                or snapshot.api_listener
            ):
                self._write_state("PARTIALLY_STOPPED", errors)
                snapshot = self.snapshot()
                return LifecycleReport.create(
                    "PARTIALLY_STOPPED",
                    snapshot,
                    "project stop left owned or ambiguous residues",
                    tuple(errors),
                )
            if not full:
                report = self.status()
                if report.state not in {"OFF", "FULLY_OFF"}:
                    return LifecycleReport.create(
                        "PARTIALLY_STOPPED",
                        report.probes,
                        "ordinary stop did not prove OFF",
                        (f"observed {report.state}",),
                    )
                return report

            already_stopped = self.status()
            if already_stopped.state == "FULLY_OFF":
                return already_stopped

            inventory_errors = self._full_stop_inventory_errors()
            if inventory_errors == ("docker_inventory_unavailable",):
                return LifecycleReport.create(
                    "OFF",
                    self.snapshot(),
                    "full stop refused: running-container inventory unavailable",
                    inventory_errors,
                )
            if inventory_errors:
                return LifecycleReport.create(
                    "OFF",
                    self.snapshot(),
                    "full stop refused: foreign or changing running-container inventory",
                    inventory_errors,
                )
            result = self._run(["docker", "desktop", "stop"], timeout=30)
            if result.returncode != 0 or not self._wait_for(lambda: not self._docker_engine(), 30):
                return LifecycleReport.create(
                    "OFF",
                    self.snapshot(),
                    "project is OFF; Docker Desktop full stop was not proven",
                    ("docker_desktop_stop_failed",),
                )
            report = self.status()
            if report.state != "FULLY_OFF":
                return LifecycleReport.create(
                    "PARTIALLY_STOPPED",
                    report.probes,
                    "Docker stopped but FULLY_OFF proof is incomplete",
                    (f"observed {report.state}",),
                )
            return report

    def _foreign_synthetic_profile_exists(self) -> bool:
        profile = self.root / "tmp" / "h03-synthetic" / "profile.json"
        return self.runtime_dir.name == "lifecycle" and os.path.lexists(profile)

    def _running_container_inventory(self) -> tuple[str, ...] | None:
        if not self._docker_engine():
            return None
        result = self._run(["docker", "ps", "--format", "{{.ID}}:{{.Names}}"], timeout=10)
        if result.returncode != 0:
            return None
        return tuple(sorted(line for line in result.stdout.splitlines() if line))

    def _full_stop_inventory_errors(self) -> tuple[str, ...]:
        first = self._running_container_inventory()
        second = self._running_container_inventory()
        if first is None or second is None:
            return ("docker_inventory_unavailable",)
        if first != second:
            return ("docker_inventory_changed", *tuple(sorted(set(first) | set(second))))
        return first


def remove_empty_runtime_parent(manager: LifecycleManager) -> None:
    parent = manager.runtime_dir.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
