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
from fastapi import FastAPI, HTTPException, Request
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


def _classify_validation_error(raw_errors: list[dict[str, Any]]) -> str:
    """Translate a Pydantic RequestValidationError into a domain error code.

    The endpoints used to do manual `if not validate_phone(phone): raise
    HTTPException(400, INVALID_PHONE_FORMAT)`. After the BE-PUSH-006 /
    BE-AUTH-006 sweep they switched to typed Pydantic models
    (`SMSSendRequest`, etc.) which raise Pydantic's generic
    ValueError that FastAPI surfaces as REQUEST_VALIDATION_ERROR.

    This helper maps the structured `loc` + `msg` back to the
    domain-specific code the frontend already keys off, so existing
    error handlers keep working without changes.

    Mapping (first match wins):
      body.phone  + format → INVALID_PHONE_FORMAT
      body.phone  + length → INVALID_PHONE_FORMAT
      body.code   → SMS_CODE_INVALID
      body.child_id       → MISSING_CHILD_ID
      body.problem_summary → MISSING_PROBLEM_SUMMARY
      body.original_text   → MISSING_ORIGINAL_TEXT
    """
    if not raw_errors:
        return "REQUEST_VALIDATION_ERROR"

    # Translate the first error that has a recognizable field + message.
    for err in raw_errors:
        loc = [str(p) for p in err.get("loc", [])]
        # Drop the leading "body" / "query" / "path" prefix.
        field_loc = [p for p in loc if p not in ("body", "query", "path", "__root__")]
        msg = (err.get("msg") or "").lower()
        err_type = (err.get("type") or "").lower()

        # field=phone + any validation error → INVALID_PHONE_FORMAT
        if field_loc == ["phone"] or (field_loc and field_loc[-1] == "phone"):
            return "INVALID_PHONE_FORMAT"

        # field=code → SMS_CODE_INVALID (login code)
        if field_loc == ["code"] or (field_loc and field_loc[-1] == "code"):
            return "SMS_CODE_INVALID"

        # dashboard alert endpoints
        if field_loc == ["child_id"] or (field_loc and field_loc[-1] == "child_id"):
            return "MISSING_CHILD_ID"
        if "problem_summary" in field_loc:
            return "MISSING_PROBLEM_SUMMARY"
        if "original_text" in field_loc:
            return "MISSING_ORIGINAL_TEXT"

        # media thumbnail
        if field_loc in (["w"], ["h"]):
            return "INVALID_THUMB_SIZE"

    return "REQUEST_VALIDATION_ERROR"


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
    async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        request_id = _get_request_id(request)
        return _structured(
            status_code=422,
            code="VALIDATION_ERROR",
            message=exc.message,
            request_id=request_id,
        )

    @app.exception_handler(SafetyViolationError)
    async def safety_violation_handler(request: Request, exc: SafetyViolationError) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.warning("safety_violation", code="SAFETY_VIOLATION", message=exc.message)
        return _structured(
            status_code=400,
            code="SAFETY_VIOLATION",
            message=exc.message,
            request_id=request_id,
        )

    @app.exception_handler(LLMRateLimitError)
    async def rate_limit_handler(request: Request, exc: LLMRateLimitError) -> JSONResponse:
        request_id = _get_request_id(request)
        return _structured(
            status_code=429,
            code="LLM_RATE_LIMIT",
            message=str(exc),
            request_id=request_id,
        )

    @app.exception_handler(LLMTimeoutError)
    async def timeout_handler(request: Request, exc: LLMTimeoutError) -> JSONResponse:
        request_id = _get_request_id(request)
        return _structured(
            status_code=504,
            code="LLM_TIMEOUT",
            message=str(exc),
            request_id=request_id,
        )

    @app.exception_handler(LLMAuthenticationError)
    async def llm_auth_error_handler(request: Request, exc: LLMAuthenticationError) -> JSONResponse:
        request_id = _get_request_id(request)
        logger.error("llm_auth_failed", model=exc.model)
        return _structured(
            status_code=500,
            code="LLM_AUTH_FAILED",
            message="Internal configuration error",
            request_id=request_id,
        )

    @app.exception_handler(InfrastructureError)
    async def infra_error_handler(request: Request, exc: InfrastructureError) -> JSONResponse:
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
    async def config_error_handler(request: Request, exc: ConfigurationError) -> JSONResponse:
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

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle `HTTPException` with §6.3 consistency.

        FastAPI's default HTTPException handler wraps `detail` under
        `{"detail": ...}`. That breaks the §6.3 contract the frontend
        reads (`response.error.code`). We intercept:

        1. If `detail` is ALREADY a §6.3 envelope (has `success` +
           `error.code`), pass it through as-is — used by
           dependencies that raise `HTTPException(detail=envelope)`
           (e.g. `require_2fa_for_sensitive_ops`).
        2. If `detail` is a legacy `{"error": code, "message": ...}`
           object (from `localized_error_response`), wrap it into a
           fresh envelope so `response.error.code` still works.
        3. Otherwise fall back to FastAPI's default `{"detail": ...}`
           so nothing else regresses.
        """
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("success") is False:
            # Already enveloped — emit verbatim.
            return JSONResponse(
                status_code=exc.status_code,
                content=detail,
                headers=exc.headers,
            )
        if isinstance(detail, dict) and isinstance(detail.get("error"), str):
            # Legacy `{"error": code, "message": ..., ...extra}` → wrap
            # into the §6.3 envelope. Preserve any extra fields (e.g.
            # `max_bytes`, `limit`, `received`) under the inner
            # `error` object so callers that read `error.max_bytes`
            # keep working.
            from niki.api._i18n import error_response

            extra = {k: v for k, v in detail.items() if k not in ("error", "message")}
            body = error_response(
                request,
                code=detail["error"],
                key="",
                fallback_message=str(detail.get("message", "")),
                extra=extra or None,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=body,
                headers=exc.headers,
            )
        # Fall back to FastAPI default.
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422 raised by FastAPI when request body / params fail Pydantic."""
        request_id = _get_request_id(request)
        # Translate specific field validation errors to the
        # domain-specific error codes the frontend already keys off
        # (INVALID_PHONE_FORMAT, SMS_CODE_INVALID, ...). This keeps
        # backwards compatibility with endpoints that previously did
        # manual `if not validate_phone(phone): raise 400(...)` and
        # the new Pydantic-typed endpoints produce identical error
        # codes from the frontend's perspective.
        raw_errors = exc.errors() if hasattr(exc, "errors") else []
        code = _classify_validation_error(raw_errors)
        # First error message is usually the most actionable.
        msg = (
            "; ".join(
                ".".join(str(p) for p in err.get("loc", [])[1:]) + ": " + err.get("msg", "")
                for err in raw_errors
            )
        ) or "Request validation failed"
        # exc.errors() can carry the raw input body which may contain
        # `bytes` objects that the JSON encoder can't serialise. Strip
        # the `input` field — the structured loc/msg/type is enough.
        safe_errors: list[dict[str, Any]] = []
        if hasattr(exc, "errors"):
            for err in exc.errors():
                # `input` can carry the raw body bytes; `ctx.error`
                # is a Python `ValueError` object. Both are not JSON-
                # serialisable. Drop them and keep the structured
                # loc/msg/type which is what callers actually need.
                safe_errors.append({k: v for k, v in err.items() if k not in ("input", "ctx")})
        # Frontend expects 400 (not 422) for body validation errors so it
        # can treat them like every other 4xx in its interceptor. The
        # detail body still carries the structured `errors` list so
        # callers can inspect field-level info if they care. The
        # top-level `code` is the classified domain-specific error
        # (INVALID_PHONE_FORMAT / SMS_CODE_INVALID / ...) so legacy
        # clients that read `error.code` keep working without changes.
        return _structured(
            status_code=400,
            code=code,
            message=msg,
            request_id=request_id,
            detail={"errors": safe_errors},
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

        return _structured(
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected error occurred",
            request_id=request_id,
        )


def _get_request_id(request: Request) -> str:
    """Extract or generate a request ID."""
    return request.headers.get("X-Request-ID", str(uuid.uuid4()))
