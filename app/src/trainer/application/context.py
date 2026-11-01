from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from trainer.domain.context import (
    DomainValidationError,
    digest_payload,
    materialize_onboarding_defaults,
    validate_goal,
    validate_onboarding,
)
from trainer.domain.exercise import validate_exercise
from trainer.domain.program import validate_program_draft


class ContextApplicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        audited: bool = False,
        required_next_action: str | None = None,
        field_errors: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.audited = audited
        self.required_next_action = required_next_action
        self.field_errors = field_errors or []


class ContextService:
    def __init__(self, executor: Callable[..., dict[str, Any]]) -> None:
        self._executor = executor

    def execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        try:
            if action == "confirm":
                payload = kwargs["payload"]
                self.verify_digest(payload, payload["request_digest"])
                normalized = deepcopy(payload)
                normalized["context"].pop("effective_at")
                normalized.pop("request_digest")
                kwargs["payload"] = {
                    **payload,
                    "request_digest": digest_payload(normalized),
                }
            return self._executor(action=action, **kwargs)
        except DomainValidationError as error:
            raise ContextApplicationError(
                "validation_error",
                str(error),
                status_code=error.status_code,
                audited=True,
                field_errors=error.field_errors,
            ) from error
        except RuntimeError as error:
            code = getattr(error, "code", "internal_error")
            status_code = getattr(error, "status_code", 500)
            raise ContextApplicationError(
                code,
                str(error),
                status_code=status_code,
                audited=True,
                required_next_action=getattr(error, "required_next_action", None),
                field_errors=getattr(error, "field_errors", None),
            ) from error

    @staticmethod
    def verify_digest(payload: dict[str, Any], supplied: str) -> None:
        body = dict(payload)
        body.pop("request_digest", None)
        if digest_payload(body) != supplied:
            raise ContextApplicationError("digest_mismatch", "request digest does not match")

    @staticmethod
    def prepare_onboarding(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = materialize_onboarding_defaults(payload)
        try:
            validate_onboarding(normalized)
        except DomainValidationError as error:
            raise ContextApplicationError(
                "validation_error", str(error), status_code=error.status_code,
                field_errors=error.field_errors,
            ) from error
        return normalized

    @staticmethod
    def validate_goal(payload: dict[str, Any]) -> None:
        try:
            validate_goal(payload)
        except DomainValidationError as error:
            raise ContextApplicationError(
                "validation_error", str(error), field_errors=error.field_errors
            ) from error

    @staticmethod
    def validate_exercise(payload: dict[str, Any]) -> None:
        try:
            validate_exercise(payload)
        except DomainValidationError as error:
            raise ContextApplicationError(
                "validation_error", str(error),
                status_code=error.status_code, field_errors=error.field_errors,
            ) from error

    @staticmethod
    def validate_program(payload: dict[str, Any]) -> None:
        try:
            validate_program_draft(payload)
        except DomainValidationError as error:
            raise ContextApplicationError("validation_error", str(error)) from error
