"""Global exception handlers for FastAPI."""

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from niki.schemas.common import APIResponse
from niki.utils.exceptions import (
    ConfigurationError,
    InfrastructureError,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
    NikiException,
    SafetyViolationError,
    ValidationError,
)

logger = structlog.get_logger(__name__)


def setup_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI app."""

    @app.exception_handler(NikiException)
    async def niki_exception_handler(request: Request, exc: NikiException) -> JSONResponse:
        """Handle all Niki-specific exceptions."""
        request_id = _get_request_id(request)

        logger.warning(
            "niki_exception",
            request_id=request_id,
            exception_type=type(exc).__name__,
            message=exc.message,
        )

        return JSONResponse(
            status_code=400,
            content=APIResponse(
                success=False,
                error=exc.message,
                request_id=request_id,
            ).model_dump(),
        )

    @app.exception_handler(ValidationError)
    async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=APIResponse(success=False, error=exc.message).model_dump(),
        )

    @app.exception_handler(SafetyViolationError)
    async def safety_violation_handler(request: Request, exc: SafetyViolationError) -> JSONResponse:
        logger.warning("safety_violation", message=exc.message)
        return JSONResponse(
            status_code=400,
            content=APIResponse(success=False, error=exc.message).model_dump(),
        )

    @app.exception_handler(LLMRateLimitError)
    async def rate_limit_handler(request: Request, exc: LLMRateLimitError) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content=APIResponse(success=False, error=str(exc)).model_dump(),
        )

    @app.exception_handler(LLMTimeoutError)
    async def timeout_handler(request: Request, exc: LLMTimeoutError) -> JSONResponse:
        return JSONResponse(
            status_code=504,
            content=APIResponse(success=False, error=str(exc)).model_dump(),
        )

    @app.exception_handler(LLMAuthenticationError)
    async def auth_error_handler(request: Request, exc: LLMAuthenticationError) -> JSONResponse:
        logger.error("llm_auth_failed", model=exc.model)
        return JSONResponse(
            status_code=500,
            content=APIResponse(success=False, error="Internal configuration error").model_dump(),
        )

    @app.exception_handler(InfrastructureError)
    async def infra_error_handler(request: Request, exc: InfrastructureError) -> JSONResponse:
        logger.error("infrastructure_error", message=exc.message)
        return JSONResponse(
            status_code=503,
            content=APIResponse(success=False, error="Service temporarily unavailable").model_dump(),
        )

    @app.exception_handler(ConfigurationError)
    async def config_error_handler(request: Request, exc: ConfigurationError) -> JSONResponse:
        logger.error("configuration_error", message=exc.message)
        return JSONResponse(
            status_code=500,
            content=APIResponse(success=False, error="Server configuration error").model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all for unexpected exceptions."""
        request_id = _get_request_id(request)

        logger.error(
            "unhandled_exception",
            request_id=request_id,
            exception_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )

        return JSONResponse(
            status_code=500,
            content=APIResponse(
                success=False,
                error="An unexpected error occurred",
                request_id=request_id,
            ).model_dump(),
        )


def _get_request_id(request: Request) -> str:
    """Extract or generate a request ID."""
    return request.headers.get("X-Request-ID", str(uuid.uuid4()))
