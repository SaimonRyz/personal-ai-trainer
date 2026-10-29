from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

LifecycleState = Literal[
    "OFF",
    "STARTING",
    "READY",
    "DEGRADED",
    "STOPPING",
    "PARTIALLY_STOPPED",
    "FULLY_OFF",
]


@dataclass(frozen=True)
class ProbeSnapshot:
    docker_engine: bool
    docker_desktop_stopped: bool
    project_containers: tuple[str, ...]
    mcp_owned: bool
    mcp_listener: bool
    api_listener: bool
    api_ready: bool
    runtime_files: tuple[str, ...]
    volume_exists: bool | None
    operation_state: str | None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecycleReport:
    state: LifecycleState
    checked_at: str
    probes: ProbeSnapshot
    message: str
    errors: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def create(
        cls,
        state: LifecycleState,
        probes: ProbeSnapshot,
        message: str,
        errors: tuple[str, ...] = (),
    ) -> LifecycleReport:
        return cls(
            state=state,
            checked_at=datetime.now(UTC).isoformat(),
            probes=probes,
            message=message,
            errors=errors,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def classify(snapshot: ProbeSnapshot) -> LifecycleState:
    if snapshot.operation_state == "STOPPING":
        return "STOPPING"
    if snapshot.operation_state == "PARTIALLY_STOPPED":
        return "PARTIALLY_STOPPED"
    if snapshot.operation_state == "STARTING":
        return "STARTING"

    host_absent = not (
        snapshot.mcp_owned
        or snapshot.mcp_listener
        or snapshot.api_listener
        or snapshot.runtime_files
    )
    containers_absent = not snapshot.project_containers
    if (
        not snapshot.docker_engine
        and snapshot.docker_desktop_stopped
        and host_absent
        and containers_absent
    ):
        return "FULLY_OFF"
    if (
        snapshot.docker_engine
        and host_absent
        and containers_absent
        and snapshot.volume_exists is True
    ):
        return "OFF"
    if (
        snapshot.docker_engine
        and snapshot.mcp_owned
        and snapshot.mcp_listener
        and snapshot.api_listener
        and snapshot.api_ready
        and len(snapshot.project_containers) == 2
    ):
        return "READY"
    if not snapshot.docker_engine and not snapshot.docker_desktop_stopped:
        return "PARTIALLY_STOPPED"
    return "DEGRADED"
