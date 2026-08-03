"""Global exception handlers for FastAPI (BE-API-001).

All handlers return responses shaped per §6.3:

    Success: {"success": true, "data": {...}, "request_id": "uuid"}
    Error:   {"success": false,
              "error": {"code": "...", "message": "...", "detail": {...}},
              "error_code": "...",      # back-compat flat
              "error_detail": {...},    # back-compat flat
              "request_id": "uuid"}

Back-compat: legacy frontends may continue reading the top-level
`error` string and `error_code` string. New frontends should read
`error.code` / `error.message`.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
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


def _structured(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str | None = None,
    detail: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the canonical error JSON response per §6.3 + back-compat."""
    body = APIResponse.from_error(
        code=code,
        message=message,
        request_id=request_id,
        detail=detail,
    ).model_dump()
    return JSONResponse(status_code=status_code, content=body, headers=headers or {})


def _code_for_exception(exc: NikiException) -> str:
    """Extract a stable error code from a NikiException.

    Subclasses set ``self.code`` if they have a stable identifier;
    otherwise we fall back to the class name in upper snake case.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    cls_name = type(exc).__name__
    # ConfigError -> CONFIG_ERROR
    return cls_name.upper().replace("ERROR", "_ERROR").rstrip("_")


def setup_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI app."""

    # --------------------------------------------------------------------
    # Generic Niki exception hierarchy
    # --------------------------------------------------------------------

    @app.exception_handler(NikiException)
    async def niki_exception_handler(request: Request, exc: NikiException) -> JSONResponse:
        """Handle all Niki-specific exceptions."""
        request_id = _get_request_id(request)
        code = _code_for_exception(exc)

        logger.warning(
            "niki_exception",
            request_id=request_id,
            exception_type=type(exc).__name__,
            code=code,
            message=exc.message,
        )

        return _structured(
            status_code=400,
            code=code,
            message=exc.message,
            request_id=request_id,
        )

    @app.exception_handler(ValidationError)
    async def validation_error_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        return _structured(
            status_code=422,
            code="VALIDATION_ERROR",
            message=exc.message,
            request_id=request_id,
        )

    @app.exception_handler(SafetyViolationError)
    async def safety_violation_handler(
        request: Request, exc: SafetyViolationError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.warning(
            "safety_violation", code="SAFETY_VIOLATION", message=exc.message
        )
        return _structured(
            status_code=400,
            code="SAFETY_VIOLATION",
            message=exc.message,
            request_id=request_id,
        )

    @app.exception_handler(LLMRateLimitError)
    async def rate_limit_handler(
        request: Request, exc: LLMRateLimitError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        return _structured(
            status_code=429,
            code="LLM_RATE_LIMIT",
            message=str(exc),
            request_id=request_id,
        )

    @app.exception_handler(LLMTimeoutError)
    async def timeout_handler(
        request: Request, exc: LLMTimeoutError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        return _structured(
            status_code=504,
            code="LLM_TIMEOUT",
            message=str(exc),
            request_id=request_id,
        )

    @app.exception_handler(LLMAuthenticationError)
    async def llm_auth_error_handler(
        request: Request, exc: LLMAuthenticationError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.error("llm_auth_failed", model=exc.model)
        return _structured(
            status_code=500,
            code="LLM_AUTH_FAILED",
            message="Internal configuration error",
            request_id=request_id,
        )

    @app.exception_handler(InfrastructureError)
    async def infra_error_handler(
        request: Request, exc: InfrastructureError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.error(
            "infrastructure_error",
            code="INFRASTRUCTURE_UNAVAILABLE",
            message=exc.message,
        )
        return _structured(
            status_code=503,
            code="INFRASTRUCTURE_UNAVAILABLE",
            message="Service temporarily unavailable",
            request_id=request_id,
        )

    @app.exception_handler(ConfigurationError)
    async def config_error_handler(
        request: Request, exc: ConfigurationError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.error(
            "configuration_error",
            code="CONFIGURATION_ERROR",
            message=exc.message,
        )
        return _structured(
            status_code=500,
            code="CONFIGURATION_ERROR",
            message="Server configuration error",
            request_id=request_id,
        )

    # --------------------------------------------------------------------
    # FastAPI built-ins — wrap into the §6.3 shape too
    # --------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422 raised by FastAPI when request body / params fail Pydantic."""
        request_id = _get_request_id(request)
        # First error message is usually the most actionable.
        msg = (
            "; ".join(
                ".".join(str(p) for p in err.get("loc", [])[1:]) + ": " + err.get("msg", "")
                for err in exc.errors()
            )
            if hasattr(exc, "errors")
            else str(exc)
        ) or "Request validation failed"
        # exc.errors() can carry the raw input body which may contain
        # `bytes` objects that the JSON encoder can't serialise. Strip
        # the `input` field — the structured loc/msg/type is enough.
        safe_errors: list[dict[str, Any]] = []
        if hasattr(exc, "errors"):
            for err in exc.errors():
                safe_errors.append({k: v for k, v in err.items() if k != "input"})
        return _structured(
            status_code=422,
            code="REQUEST_VALIDATION_ERROR",
            message=msg,
            request_id=request_id,
            detail={"errors": safe_errors},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Catch-all for unexpected exceptions."""
        request_id = _get_request_id(request)

        logger.error(
            "unhandled_exception",
            request_id=request_id,
            exception_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )

        return _structured(
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected error occurred",
            request_id=request_id,
        )


def _get_request_id(request: Request) -> str:
    """Extract or generate a request ID."""
    return request.headers.get("X-Request-ID", str(uuid.uuid4()))
