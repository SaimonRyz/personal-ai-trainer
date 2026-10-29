from __future__ import annotations

from typing import Any

from trainer.entrypoints import api


def test_uvicorn_uses_existing_json_logging_without_text_access_log(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    settings = type(
        "Settings",
        (),
        {
            "database_app_dsn": None,
            "database_connect_timeout_seconds": 1.0,
            "api_host": "127.0.0.1",
            "api_port": 8765,
            "api_bearer_token": "x" * 32,
            "max_request_body_bytes": 1024,
            "startup_max_attempts": 1,
            "startup_retry_delay_seconds": 0.0,
            "lifecycle_drain_token": "y" * 32,
        },
    )()
    monkeypatch.setattr(api, "load_settings", lambda: settings)

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(api.uvicorn, "run", fake_run)

    api.run()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["access_log"] is False
    assert captured["log_config"] is None
