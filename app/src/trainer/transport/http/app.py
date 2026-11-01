from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from trainer.application.context import ContextApplicationError, ContextService
from trainer.application.readiness import (
    ReadinessProbe,
    ReadinessResult,
    UnconfiguredReadinessProbe,
)
from trainer.config.settings import Settings, load_settings
from trainer.observability.logging import configure_logging
from trainer.transport.http.contracts import (
    CommitResult,
    ConfirmRequest,
    DataEnvelope,
    DraftResult,
    DraftRevisionRequest,
    ErrorDetail,
    ErrorEnvelope,
    FieldError,
    ExerciseCatalogDraftRequest,
    FamiliarExerciseBatchDraftRequest,
    WorkoutImportBatchDraftRequest,
    ExerciseExactResponse,
    ExerciseQueryRequest,
    ExerciseSearchResponse,
    GoalDraftRequest,
    HistoricalRecordDraftRequest,
    InventoryChangeDraftRequest,
    OnboardingDraftRequest,
    OperationStatusRequest,
    OperationStatusResponse,
    PlanDraftRequest,
    PlanLifecycleRequest,
    PlanningContextRequest,
    PlanningContextResponse,
    PlanQueryRequest,
    PlanResponse,
    PreviewRequest,
    PreviewResult,
    PreviewShownRequest,
    ProfileContextRequest,
    ProfileContextResponse,
    ProgramDraftRequest,
    ProgramPublicationDraftRequest,
    ProgramQueryRequest,
    ProgramResponse,
    RecordCandidatesRequest,
    RecordCandidatesResponse,
    RecordDecisionDraftRequest,
    RecordQueryRequest,
    RecordResultsResponse,
    WorkoutCorrectionRequest,
    WorkoutHistoryRequest,
    WorkoutHistoryResponse,
    WorkoutReportDraftRequest,
)
from trainer.transport.http.evidence_contracts import (
    EligibleConclusionsRequest,
    EligibleConclusionsResponse,
    EvidenceCommandRequest,
    RecommendationExplanationRequest,
    RecommendationExplanationResponse,
    VerificationDraftRequest,
)
from trainer.transport.http.measurement_contracts import (
    BMIResponse,
    MeasurementDraftRequest,
    MeasurementQueryRequest,
    SeriesResponse,
    SessionsResponse,
)
from trainer.transport.http.progress_contracts import (
    CalculationDraftResponse,
    CalculationRequest,
    CalculationResult,
    ProgressQueryRequest,
    ProgressQueryResponse,
)

RequestHandler = Callable[[Request], Awaitable[Response]]
logger = logging.getLogger("trainer.http")
DOMAIN_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope}
    for status in (400, 401, 403, 404, 409, 410, 422, 429, 500, 503)
}


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    correlation_id: UUID,
    retryable: bool = False,
    required_next_action: str | None = None,
    field_errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message_safe=message,
            retryable=retryable,
            operation_state="failed",
            correlation_id=correlation_id,
            field_errors=[FieldError(**item) for item in (field_errors or [])],
            changed_dependencies=[],
            required_next_action=required_next_action,
        ),
        meta={"api_version": "v1"},
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


class ApiBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        started = time.monotonic()
        api_path = request.url.path.startswith("/api/v1")
        if api_path:
            accept = request.headers.get("Accept")
            if accept is None or "application/json" not in accept.lower():
                correlation_id = uuid4()
                response = error_response(
                    status_code=406,
                    code="validation_error",
                    message="Accept application/json is required",
                    correlation_id=correlation_id,
                )
                return self._complete(request, response, correlation_id, None, started)
            content_type = request.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if request.method == "POST" and content_type != "application/json":
                correlation_id = uuid4()
                response = error_response(
                    status_code=415,
                    code="validation_error",
                    message="Content-Type application/json is required",
                    correlation_id=correlation_id,
                )
                return self._complete(request, response, correlation_id, None, started)
        raw_correlation = request.headers.get("X-Correlation-ID")
        try:
            if api_path and raw_correlation is None:
                raise ValueError
            correlation_id = UUID(raw_correlation) if raw_correlation else uuid4()
        except ValueError:
            correlation_id = uuid4()
            response = error_response(
                status_code=400,
                code="validation_error",
                message="X-Correlation-ID must be a UUID",
                correlation_id=correlation_id,
            )
            return self._complete(request, response, correlation_id, None, started)
        request.state.correlation_id = correlation_id

        raw_causation = request.headers.get("X-Causation-ID")
        try:
            if api_path and raw_causation is None:
                raise ValueError
            causation_id = UUID(raw_causation) if raw_causation else None
        except ValueError:
            response = error_response(
                status_code=400,
                code="validation_error",
                message="X-Causation-ID must be a UUID",
                correlation_id=correlation_id,
            )
            return self._complete(request, response, correlation_id, None, started)
        request.state.causation_id = causation_id

        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                too_large = int(content_length) > self.settings.max_request_bytes
            except ValueError:
                too_large = True
            if too_large:
                response = error_response(
                    status_code=413,
                    code="request_too_large",
                    message="request exceeds configured limit",
                    correlation_id=correlation_id,
                )
                return self._complete(request, response, correlation_id, causation_id, started)

        protected_path = api_path or request.url.path.startswith("/internal/lifecycle")
        if protected_path:
            expected = f"Bearer {self.settings.api_token.get_secret_value()}"
            supplied = request.headers.get("Authorization", "")
            owner_token = self.settings.owner_verifier_token
            is_owner = owner_token is not None and hmac.compare_digest(
                supplied, f"Bearer {owner_token.get_secret_value()}"
            )
            request.state.owner_verifier = is_owner
            if not hmac.compare_digest(supplied, expected) and not is_owner:
                response = error_response(
                    status_code=401,
                    code="unauthenticated",
                    message="valid bearer credential required",
                    correlation_id=correlation_id,
                )
                return self._complete(request, response, correlation_id, causation_id, started)

        if (
            api_path
            and request.url.path.startswith("/api/v1/queries/")
            and request.headers.get("Idempotency-Key")
        ):
            response = error_response(
                status_code=400,
                code="validation_error",
                message="Idempotency-Key is prohibited for queries",
                correlation_id=correlation_id,
            )
            return self._complete(request, response, correlation_id, causation_id, started)
        if (
            api_path
            and request.url.path.startswith("/api/v1/commands/")
            and not request.headers.get("Idempotency-Key")
        ):
            response = error_response(
                status_code=400,
                code="validation_error",
                message="Idempotency-Key is required for commands",
                correlation_id=correlation_id,
            )
            return self._complete(request, response, correlation_id, causation_id, started)

        if api_path and request.app.state.draining:
            response = error_response(
                status_code=503,
                code="service_stopping",
                message="project is stopping; new calls are disabled",
                correlation_id=correlation_id,
                retryable=True,
            )
            return self._complete(request, response, correlation_id, causation_id, started)

        downstream_response = await call_next(request)
        return self._complete(request, downstream_response, correlation_id, causation_id, started)

    def _complete(
        self,
        request: Request,
        response: Response,
        correlation_id: UUID,
        causation_id: UUID | None,
        started: float,
    ) -> Response:
        response.headers["X-Correlation-ID"] = str(correlation_id)
        if causation_id is not None:
            response.headers["X-Causation-ID"] = str(causation_id)
        safe_fields: dict[str, str | int] = {
            "correlation_id": str(correlation_id),
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
        if causation_id is not None:
            safe_fields["causation_id"] = str(causation_id)
        logger.info(
            "request_completed",
            extra=safe_fields,
        )
        return response


def create_app(
    settings: Settings | None = None,
    readiness_probe: ReadinessProbe | None = None,
    context_service: ContextService | None = None,
) -> FastAPI:
    """Create the local API with the technical foundation and enabled D slices."""
    runtime_settings = settings or load_settings()
    runtime_probe = readiness_probe or UnconfiguredReadinessProbe()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        result = ReadinessResult(ready=False, reason="startup_pending")
        for attempt in range(runtime_settings.startup_probe_attempts):
            result = runtime_probe.check()
            if result.ready:
                break
            if attempt + 1 < runtime_settings.startup_probe_attempts:
                await asyncio.sleep(runtime_settings.startup_probe_delay_seconds)
        app.state.startup_readiness = result
        yield

    configure_logging()
    app = FastAPI(
        title="Personal AI Trainer technical foundation",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.draining = False
    app.add_middleware(ApiBoundaryMiddleware, settings=runtime_settings)

    def audit_failed_command(request: Request, error_code: str) -> None:
        method_by_path = {
            "/api/v1/commands/onboarding-drafts": "CMD-01",
            "/api/v1/commands/goal-drafts": "CMD-02",
            "/api/v1/commands/inventory-change-drafts": "CMD-09",
            "/api/v1/commands/measurement-session-drafts": "CMD-11",
            "/api/v1/commands/evidence-drafts": "CMD-14",
            "/api/v1/commands/conclusion-verification-drafts": "CMD-15",
            "/api/v1/commands/exercise-catalog-drafts": "CMD-22",
            "/api/v1/commands/familiar-exercise-batches": "CMD-25",
            "/api/v1/commands/program-drafts": "CMD-03",
            "/api/v1/commands/program-publication-drafts": "CMD-04",
            "/api/v1/commands/plan-drafts": "CMD-05",
            "/api/v1/commands/workout-report-drafts": "CMD-06",
            "/api/v1/commands/workout-import-batches": "CMD-26",
            "/api/v1/commands/correction-drafts": "CMD-07",
            "/api/v1/commands/plan-lifecycle-drafts": "CMD-23",
            "/api/v1/commands/previews": "CMD-20",
            "/api/v1/commands/calculations": "CMD-17",
        }
        method = method_by_path.get(request.url.path)
        if method is None and request.url.path.endswith("/revisions"):
            method = "CMD-19"
        if method is None and request.url.path.endswith("/shown"):
            method = "CMD-20"
        if method is None and request.url.path.endswith("/confirm"):
            method = "CMD-00"
        if method is None or context_service is None:
            return
        try:
            context_service.execute(
                "record_failed_request",
                method=method,
                key=request.headers.get("Idempotency-Key"),
                correlation_id=getattr(request.state, "correlation_id", uuid4()),
                causation_id=getattr(request.state, "causation_id", None),
                error_code=error_code,
            )
        except ContextApplicationError:
            logger.warning("failed_request_audit_unavailable")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", uuid4())
        audit_failed_command(request, "validation_error")
        return error_response(
            status_code=400,
            code="validation_error",
            message="request validation failed",
            correlation_id=correlation_id,
        )

    @app.exception_handler(StarletteHTTPException)
    async def route_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", uuid4())
        code = "not_found" if error.status_code == 404 else "validation_error"
        message = "resource not found" if error.status_code == 404 else "method not allowed"
        return error_response(
            status_code=error.status_code,
            code=code,
            message=message,
            correlation_id=correlation_id,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", uuid4())
        logger.exception(
            "unhandled_request_error",
            exc_info=error,
            extra={
                "correlation_id": str(correlation_id),
                "path": request.url.path,
            },
        )
        return error_response(
            status_code=500,
            code="internal_error",
            message="internal server error",
            correlation_id=correlation_id,
            retryable=True,
        )

    @app.exception_handler(ContextApplicationError)
    async def context_error(request: Request, error: ContextApplicationError) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", uuid4())
        if not error.audited:
            audit_failed_command(request, error.code)
        return error_response(
            status_code=error.status_code,
            code=error.code,
            message=str(error),
            correlation_id=correlation_id,
            retryable=error.status_code >= 500 and error.code != "inconsistent_domain_state",
            required_next_action=error.required_next_action,
            field_errors=error.field_errors,
        )

    def require_context_service() -> ContextService:
        if context_service is None:
            raise ContextApplicationError(
                "database_unavailable", "context service is unavailable", status_code=503
            )
        return context_service

    def envelope(data: dict[str, Any], request: Request) -> dict[str, Any]:
        return {
            "data": data,
            "meta": {"api_version": "v1", "correlation_id": str(request.state.correlation_id)},
        }

    @app.post(
        "/api/v1/commands/evidence-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def evidence_draft(
        body: EvidenceCommandRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_evidence_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/conclusion-verification-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def verification_draft(
        body: VerificationDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not request.state.owner_verifier:
            raise ContextApplicationError(
                "capability_required", "owner_verifier credential required", status_code=403
            )
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_verification_draft",
                payload=payload,
                owner_verifier=True,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/queries/conclusions/eligible",
        response_model=DataEnvelope[EligibleConclusionsResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def eligible_conclusions(body: EligibleConclusionsRequest, request: Request) -> dict[str, Any]:
        return envelope(
            require_context_service().execute(
                "query_eligible_conclusions", payload=body.model_dump(mode="json")
            ),
            request,
        )

    @app.post(
        "/api/v1/queries/recommendations/explanation",
        response_model=DataEnvelope[RecommendationExplanationResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def recommendation_explanation(
        body: RecommendationExplanationRequest, request: Request
    ) -> dict[str, Any]:
        return envelope(
            require_context_service().execute(
                "get_recommendation_explanation", payload=body.model_dump(mode="json")
            ),
            request,
        )

    @app.post(
        "/api/v1/queries/profile-context",
        response_model=DataEnvelope[ProfileContextResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def profile_context(body: ProfileContextRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "get_profile_context", effective_at=body.context.effective_at
        )
        return envelope(result, request)

    @app.post(
        "/api/v1/queries/planning-context",
        response_model=DataEnvelope[PlanningContextResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def planning_context(body: PlanningContextRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "get_planning_context", payload=body.model_dump(mode="json")
        )
        return envelope(result, request)

    @app.post(
        "/api/v1/queries/exercises/search",
        response_model=DataEnvelope[ExerciseSearchResponse | ExerciseExactResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def exercises(body: ExerciseQueryRequest, request: Request) -> dict[str, Any]:
        return envelope(
            require_context_service().execute(
                "query_exercises", payload=body.model_dump(mode="json")
            ),
            request,
        )

    @app.post(
        "/api/v1/queries/workouts/history",
        response_model=DataEnvelope[WorkoutHistoryResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def workout_history(body: WorkoutHistoryRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "get_workout_history", payload=body.model_dump(mode="json")
        )
        try:
            result = WorkoutHistoryResponse.model_validate(result).model_dump(mode="json")
        except ValidationError as error:
            raise ContextApplicationError(
                "inconsistent_domain_state",
                "workout history read-back is inconsistent",
                status_code=500,
                required_next_action="run_integrity_check",
            ) from error
        return envelope(result, request)

    @app.post(
        "/api/v1/queries/record-candidates",
        response_model=DataEnvelope[RecordCandidatesResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def record_candidates(body: RecordCandidatesRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "query_record_candidates", payload=body.model_dump(mode="json")
        )
        try:
            result = RecordCandidatesResponse.model_validate(result).model_dump(mode="json")
        except ValidationError as error:
            raise ContextApplicationError(
                "inconsistent_domain_state",
                "record candidate read-back is inconsistent",
                status_code=500,
                required_next_action="run_integrity_check",
            ) from error
        return envelope(result, request)

    @app.post(
        "/api/v1/queries/records",
        response_model=DataEnvelope[RecordResultsResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def records(body: RecordQueryRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "query_records", payload=body.model_dump(mode="json")
        )
        try:
            result = RecordResultsResponse.model_validate(result).model_dump(mode="json")
        except ValidationError as error:
            raise ContextApplicationError(
                "inconsistent_domain_state",
                "record read-back is inconsistent",
                status_code=500,
                required_next_action="run_integrity_check",
            ) from error
        return envelope(result, request)

    @app.post(
        "/api/v1/queries/measurements",
        response_model=DataEnvelope[SessionsResponse | SeriesResponse | BMIResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def measurements(body: MeasurementQueryRequest, request: Request) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        result = require_context_service().execute("get_body_measurements", payload=payload)
        try:
            if payload["mode"] == "sessions":
                result = SessionsResponse.model_validate(result).model_dump(mode="json")
            elif payload["mode"] == "series":
                result = SeriesResponse.model_validate(result).model_dump(mode="json")
            else:
                result = BMIResponse.model_validate(result).model_dump(mode="json")
        except ValidationError as error:
            raise ContextApplicationError(
                "inconsistent_domain_state",
                "measurement read-back is inconsistent",
                status_code=500,
                required_next_action="run_integrity_check",
            ) from error
        return envelope(result, request)

    @app.post(
        "/api/v1/queries/progress",
        response_model=DataEnvelope[ProgressQueryResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def progress(body: ProgressQueryRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "query_progress", payload=body.model_dump(mode="json")
        )
        try:
            result = ProgressQueryResponse.model_validate(result).model_dump(mode="json")
        except ValidationError as error:
            raise ContextApplicationError(
                "inconsistent_domain_state",
                "progress read-back is inconsistent",
                status_code=500,
                required_next_action="run_integrity_check",
            ) from error
        return envelope(result, request)

    @app.post(
        "/api/v1/queries/operations/status",
        response_model=DataEnvelope[OperationStatusResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def operation_status(body: OperationStatusRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "get_operation_status", locator=body.locator.model_dump(mode="json")
        )
        return envelope(result, request)

    @app.post(
        "/api/v1/commands/onboarding-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def onboarding_draft(
        body: OnboardingDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"draft_id", "expected_revision_id"})
        if body.draft_id is not None:
            payload["draft_id"] = str(body.draft_id)
            payload["expected_revision_id"] = str(body.expected_revision_id)
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        normalized = service.prepare_onboarding(
            {k: v for k, v in payload.items() if k != "request_digest"}
        )
        payload = {**normalized, "request_digest": payload["request_digest"]}
        result = service.execute(
            "create_onboarding_draft",
            payload=payload,
            key=idempotency_key,
            correlation_id=request.state.correlation_id,
            causation_id=request.state.causation_id,
        )
        return envelope(result, request)

    @app.post(
        "/api/v1/commands/measurement-session-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def measurement_session_draft(
        body: MeasurementDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_measurement_session_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/inventory-change-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def inventory_change_draft(
        body: InventoryChangeDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_inventory_change_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/goal-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def goal_draft(
        body: GoalDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        service.validate_goal(payload["goal"])
        result = service.execute(
            "create_goal_draft",
            payload=payload,
            key=idempotency_key,
            correlation_id=request.state.correlation_id,
            causation_id=request.state.causation_id,
        )
        return envelope(result, request)

    @app.post(
        "/api/v1/commands/exercise-catalog-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def exercise_catalog_draft(
        body: ExerciseCatalogDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        service.validate_exercise(payload)
        return envelope(
            service.execute(
                "create_exercise_catalog_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/familiar-exercise-batches",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def familiar_exercise_batch_draft(
        body: FamiliarExerciseBatchDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"draft_id", "expected_revision_id"})
        if body.draft_id is not None:
            payload["draft_id"] = str(body.draft_id)
            payload["expected_revision_id"] = str(body.expected_revision_id)
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_familiar_exercise_batch_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/queries/programs/get",
        response_model=DataEnvelope[ProgramResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def get_program(body: ProgramQueryRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute(
            "get_program", payload=body.model_dump(mode="json")
        )
        try:
            result = ProgramResponse.model_validate(result).model_dump(mode="json")
        except ValidationError as error:
            raise ContextApplicationError(
                "inconsistent_domain_state",
                "program read-back is inconsistent",
                status_code=500,
                required_next_action="run_integrity_check",
            ) from error
        return envelope(result, request)

    @app.post(
        "/api/v1/commands/program-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def program_draft(
        body: ProgramDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        service.validate_program(payload)
        return envelope(
            service.execute(
                "create_program_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/drafts/{draft_id}/revisions",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def revise_program(
        draft_id: UUID,
        body: DraftRevisionRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "revise_program_draft",
                draft_id=draft_id,
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/program-publication-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def program_publication(
        body: ProgramPublicationDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_program_publication_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/queries/plans/get",
        response_model=DataEnvelope[PlanResponse],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def get_plan(body: PlanQueryRequest, request: Request) -> dict[str, Any]:
        result = require_context_service().execute("get_plan", payload=body.model_dump(mode="json"))
        return envelope(result, request)

    @app.post(
        "/api/v1/commands/plan-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def plan_draft(
        body: PlanDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_plan_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/workout-report-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def workout_report_draft(
        body: WorkoutReportDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_workout_report_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/workout-import-batches",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def workout_import_batch_draft(
        body: WorkoutImportBatchDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"draft_id", "expected_revision_id"})
        if body.draft_id is not None:
            payload["draft_id"] = str(body.draft_id)
            payload["expected_revision_id"] = str(body.expected_revision_id)
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(service.execute(
            "create_workout_import_batch_draft", payload=payload, key=idempotency_key,
            correlation_id=request.state.correlation_id,
            causation_id=request.state.causation_id,
        ), request)

    @app.post(
        "/api/v1/commands/correction-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def correction_draft(
        body: WorkoutCorrectionRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_correction_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/record-decision-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def record_decision_draft(
        body: RecordDecisionDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_record_decision_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/historical-record-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def historical_record_draft(
        body: HistoricalRecordDraftRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_historical_record_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/calculations",
        response_model=DataEnvelope[CalculationDraftResponse | CalculationResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def calculation(
        body: CalculationRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        result = service.execute(
            "create_calculation_draft",
            payload=payload,
            key=idempotency_key,
            correlation_id=request.state.correlation_id,
            causation_id=request.state.causation_id,
        )
        try:
            result = (
                CalculationDraftResponse.model_validate(result).model_dump(mode="json")
                if result.get("state") == "draft"
                else CalculationResult.model_validate(result).model_dump(mode="json")
            )
        except ValidationError as error:
            raise ContextApplicationError(
                "inconsistent_domain_state",
                "calculation response is inconsistent",
                status_code=500,
                required_next_action="run_integrity_check",
            ) from error
        return envelope(result, request)

    @app.post(
        "/api/v1/commands/plan-lifecycle-drafts",
        response_model=DataEnvelope[DraftResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def plan_lifecycle_draft(
        body: PlanLifecycleRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_plan_lifecycle_draft",
                payload=payload,
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/previews",
        response_model=DataEnvelope[PreviewResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def preview(
        body: PreviewRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json", exclude={"entry_name", "entry_key"})
        if body.entry_name is not None:
            payload["entry_name"] = body.entry_name
        if body.entry_key is not None:
            payload["entry_key"] = body.entry_key
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "create_preview",
                payload=payload,
                **({"owner_verifier": True} if request.state.owner_verifier else {}),
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/previews/{preview_id}/shown",
        response_model=DataEnvelope[PreviewResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def preview_shown(
        preview_id: UUID,
        body: PreviewShownRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "mark_preview_shown",
                preview_id=preview_id,
                payload=payload,
                **({"owner_verifier": True} if request.state.owner_verifier else {}),
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.post(
        "/api/v1/commands/previews/{preview_id}/confirm",
        response_model=DataEnvelope[CommitResult],
        responses=DOMAIN_ERROR_RESPONSES,
    )
    def confirm(
        preview_id: UUID,
        body: ConfirmRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        payload = body.model_dump(mode="json")
        service = require_context_service()
        service.verify_digest(payload, body.request_digest)
        return envelope(
            service.execute(
                "confirm",
                preview_id=preview_id,
                payload=payload,
                **({"owner_verifier": True} if request.state.owner_verifier else {}),
                key=idempotency_key,
                correlation_id=request.state.correlation_id,
                causation_id=request.state.causation_id,
            ),
            request,
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        if app.state.draining:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "stopping"},
            )
        result = runtime_probe.check()
        status_code = 200 if result.ready else 503
        status = "ready" if result.ready else "not_ready"
        return JSONResponse(
            status_code=status_code,
            content={"status": status, "reason": result.reason},
        )

    @app.post("/internal/lifecycle/drain", include_in_schema=False, status_code=204)
    async def begin_drain() -> Response:
        app.state.draining = True
        return Response(status_code=204)

    return app
