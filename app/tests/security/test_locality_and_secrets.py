from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from trainer.config.settings import Settings

ROOT = Path(__file__).parents[2]
TOKEN = "stage-c-test-token-that-is-long-enough"


def test_runtime_settings_are_deny_by_default_and_secrets_are_redacted() -> None:
    settings = Settings(api_token=SecretStr(TOKEN))
    assert settings.api_host == "127.0.0.1"
    assert settings.api_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(settings)

    with pytest.raises(ValidationError):
        Settings(api_token=SecretStr(TOKEN), unexpected_setting=True)  # type: ignore[call-arg]


def test_compose_publishes_only_api_loopback_and_keeps_mcp_outside() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8765:8765"' in compose
    postgres_section, api_section = compose.split("  api:", maxsplit=1)
    assert "ports:" not in postgres_section
    assert "trainer-mcp" not in compose
    assert "read_only: true" in api_section
    assert "no-new-privileges:true" in api_section
    assert 'restart: "no"' in compose


def test_compose_bounds_project_container_resources() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    postgres_section, api_section = compose.split("  api:", maxsplit=1)

    assert 'cpus: "1.50"' in postgres_section
    assert "mem_limit: 768m" in postgres_section
    assert "mem_reservation: 128m" in postgres_section
    assert "pids_limit: 256" in postgres_section

    assert 'cpus: "1.00"' in api_section
    assert "mem_limit: 384m" in api_section
    assert "mem_reservation: 64m" in api_section
    assert "pids_limit: 128" in api_section


def test_examples_and_image_context_contain_no_real_local_configuration() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "replace-with-unique-local" in example
    assert "TRAINER_API_TOKEN=" in example
    assert "TRAINER_DATABASE_APP_PASSWORD=" in example

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for forbidden in (".env", "*.pdf", "TRAINING_PLAN.md", "INVITRO_*"):
        assert forbidden in dockerignore

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER trainer" in dockerfile
    assert "python:3.12.14-slim-bookworm" in dockerfile
