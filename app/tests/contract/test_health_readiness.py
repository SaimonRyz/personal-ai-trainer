from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from trainer.application.readiness import ReadinessResult
from trainer.config.settings import Settings
from trainer.transport.http.app import create_app

TOKEN = "stage-c-readiness-token-that-is-long-enough"


@dataclass
class StubProbe:
    result: ReadinessResult
    calls: int = 0

    def check(self) -> ReadinessResult:
        self.calls += 1
        return self.result


def settings() -> Settings:
    return Settings(
        api_token=SecretStr(TOKEN),
        startup_probe_attempts=1,
        startup_probe_delay_seconds=0,
    )


def api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Correlation-ID": str(uuid4()),
        "X-Causation-ID": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


def test_health_is_live_even_when_dependencies_are_not_ready() -> None:
    probe = StubProbe(ReadinessResult(ready=False, reason="database_unavailable"))
    with TestClient(create_app(settings(), probe)) as client:
        health = client.get("/health")
        ready = client.get("/ready")
    assert health.status_code == 200
    assert health.json() == {"status": "live"}
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "reason": "database_unavailable",
    }


def test_ready_returns_only_safe_stable_status() -> None:
    probe = StubProbe(ReadinessResult(ready=True, reason="ready"))
    with TestClient(create_app(settings(), probe)) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "reason": "ready"}
    assert probe.calls >= 2  # startup preflight and request-time recovery probe


def test_drain_barrier_rejects_new_api_calls_but_keeps_process_live() -> None:
    probe = StubProbe(ReadinessResult(ready=True, reason="ready"))
    with TestClient(create_app(settings(), probe)) as client:
        drain = client.post(
            "/internal/lifecycle/drain",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        blocked = client.post(
            "/api/v1/not-a-route",
            headers=api_headers(),
            json={},
        )
        ready = client.get("/ready")
        health = client.get("/health")
    assert drain.status_code == 204
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "service_stopping"
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reason": "stopping"}
    assert health.status_code == 200


def test_drain_allows_already_accepted_request_to_finish() -> None:
    probe = StubProbe(ReadinessResult(ready=True, reason="ready"))
    app = create_app(settings(), probe)
    accepted = Event()
    release = Event()
    result: list[int] = []

    @app.post("/api/v1/stage-c-running-fixture")
    def running_fixture() -> dict[str, str]:
        accepted.set()
        release.wait(timeout=3)
        return {"status": "finished"}

    with TestClient(app) as client:
        request = Thread(
            target=lambda: result.append(
                client.post(
                    "/api/v1/stage-c-running-fixture",
                    headers=api_headers(),
                ).status_code
            )
        )
        request.start()
        assert accepted.wait(timeout=3)
        drain = client.post(
            "/internal/lifecycle/drain",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        blocked = client.post(
            "/api/v1/not-a-route",
            headers=api_headers(),
        )
        release.set()
        request.join(timeout=3)
    assert drain.status_code == 204
    assert blocked.status_code == 503
    assert result == [200]
