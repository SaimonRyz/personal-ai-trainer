from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: str


class ReadinessProbe(Protocol):
    def check(self) -> ReadinessResult: ...


class UnconfiguredReadinessProbe:
    def check(self) -> ReadinessResult:
        return ReadinessResult(ready=False, reason="database_not_configured")
