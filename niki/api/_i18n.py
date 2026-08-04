"""i18n helpers for API handlers (BE-I18N-001).

These thin wrappers let handlers return localized error payloads
without manually threading the `Translator` through every call
site:

    from niki.api._i18n import localized_error_response

    raise HTTPException(
        status_code=400,
        detail=localized_error_response(
            request,
            code="INVALID_PHONE_FORMAT",
            key="auth.invalid_phone_format",
        ),
    )

The function:
  - reads `lang` off `request.state` if set by `LanguageMiddleware`,
  - otherwise reads the `Accept-Language` header on the fly so
    endpoints stay correct even when the middleware is bypassed
    (e.g. inside TestClient-only flows that don't add the
    middleware),
  - falls back to the configured default if the lang is unknown.

The structured `error` CODE is preserved verbatim; only the
`message` field is rendered.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from niki.i18n.middleware import pick_lang
from niki.i18n.translator import (
    DEFAULT_LANG,
    get_translator,
)


def resolve_lang(request: Request | None) -> str:
    """Best-effort language resolution for the given request.

    Order:
      1. `request.state.lang` if `LanguageMiddleware` ran.
      2. `Accept-Language` header on the request.
      3. `?lang=` query param.
      4. Module default (`zh`).
    """
    if request is None:
        return DEFAULT_LANG
    lang = getattr(request.state, "lang", None)
    if lang:
        return lang
    return pick_lang(
        accept_language=request.headers.get("accept-language"),
        query_lang=request.query_params.get("lang"),
        default=DEFAULT_LANG,
    )


def localized_error_response(
    request: Request | None,
    code: str,
    key: str,
    *,
    extra: dict[str, Any] | None = None,
    fallback_message: str | None = None,
) -> dict[str, Any]:
    """Build the §6.3 error body with a translated `message` field.

    Args:
        request: The incoming FastAPI request (may be None in
            edge contexts like lifespan setup).
        code: The structured error CODE (preserved verbatim, e.g.
            `"INVALID_PHONE_FORMAT"`).
        key: Dotted translation key (e.g.
            `"auth.invalid_phone_format"`).
        extra: Optional extra fields to merge into the response
            (e.g. `{"retry_after_seconds": 30}`). The caller owns
            these — the helper never overwrites `error` or `message`.
        fallback_message: Optional literal string returned when no
            translation for `key` exists in any catalog. If unset,
            the key itself is returned per `Translator` defaults.

    Returns:
        Dict suitable for `HTTPException(detail=...)`.
    """
    lang = resolve_lang(request)
    translator = get_translator()
    message = translator.translate(
        key,
        lang,
        default=fallback_message,
    )
    # Legacy §6.3-inner key: `error` carries the code. New endpoints
    # should use `error_response()` (which builds the full envelope
    # with the `code` key); keeping `error` here preserves the shape
    # for endpoints that still return `HTTPException(detail=...)`.
    body: dict[str, Any] = {"error": code, "message": message}
    if extra:
        body.update(extra)
    return body


def error_response(
    request: Request | None,
    code: str,
    key: str,
    *,
    extra: dict[str, Any] | None = None,
    fallback_message: str | None = None,
) -> dict[str, Any]:
    """Build a complete §6.3 response envelope for an error.

    This is the endpoint-level counterpart to `localized_error_response`
    (which returns only the inner `error` object). The returned dict is
    the full body to pass to `JSONResponse(content=...)`:

        {
          "success": false,
          "data": null,
          "error": {"code": "...", "message": "...", ...extra},
          "error_code": "...",
          "error_detail": ...,
          "request_id": "..."
        }

    Frontend / TS SDK (BE-API-007) reads `response.error.code`; all
    endpoints should return this shape so the interceptor has a single
    contract to parse.
    """
    lang = resolve_lang(request)
    translator = get_translator()
    message = translator.translate(
        key,
        lang,
        default=fallback_message,
    )
    inner: dict[str, Any] = {"code": code, "message": message}
    if extra:
        inner.update(extra)

    request_id = (
        request.headers.get("X-Request-ID")
        if request is not None
        else None
    )
    if not request_id:
        import uuid as _uuid
        request_id = str(_uuid.uuid4())
    return {
        "success": False,
        "data": None,
        "error": inner,
        "error_code": inner.get("code"),
        "error_detail": inner.get("detail"),
        "request_id": request_id,
    }


__all__ = [
    "localized_error_response",
    "error_response",
    "resolve_lang",
]