from __future__ import annotations

from functools import lru_cache
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Fail-closed runtime configuration loaded only from the local environment."""

    model_config = SettingsConfigDict(
        env_prefix="TRAINER_",
        case_sensitive=False,
        extra="forbid",
    )

    api_token: SecretStr = Field(min_length=32)
    owner_verifier_token: SecretStr | None = Field(default=None, min_length=32)
    api_host: str = Field(default="127.0.0.1", pattern=r"^(127\.0\.0\.1|0\.0\.0\.0)$")
    allow_container_bind_all: bool = False
    api_port: int = Field(default=8765, ge=1024, le=65535)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=4_194_304)
    database_app_dsn: SecretStr | None = None
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    startup_probe_attempts: int = Field(default=3, ge=1, le=10)
    startup_probe_delay_seconds: float = Field(default=0.25, ge=0, le=5)
    e1rm_bench_exercise_ids: frozenset[UUID] = frozenset()
    e1rm_bench_grip_conditions: frozenset[str] = frozenset()

    @field_validator("owner_verifier_token", mode="before")
    @classmethod
    def optional_owner_token(cls, value: object) -> object:
        return None if value in ("", "null") else value

    @field_validator("e1rm_bench_grip_conditions", mode="before")
    @classmethod
    def normalize_grip_conditions(cls, value: object) -> object:
        if value is None:
            return frozenset()
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        normalized = {
            " ".join(item.strip().split()).casefold()
            for item in value
            if isinstance(item, str) and item.strip()
        }
        if len(normalized) != len(value):
            raise ValueError("grip conditions must be non-empty unique strings")
        return frozenset(normalized)

    @model_validator(mode="after")
    def validate_bind_scope(self) -> Settings:
        if self.owner_verifier_token is not None and self.owner_verifier_token == self.api_token:
            raise ValueError("owner verifier credential must differ from the agent credential")
        if self.api_host == "0.0.0.0" and not self.allow_container_bind_all:
            raise ValueError("0.0.0.0 is allowed only inside the container network namespace")
        return self


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
