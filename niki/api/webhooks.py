"""Webhook subscription API endpoints (BE-API-008).

Endpoints (all under `/api/v1/webhooks`):

    POST   /                       — register a subscription
    GET    /                       — list caller's subscriptions
    GET    /{id}                   — fetch a single subscription
    PATCH  /{id}                   — partial update
    DELETE /{id}                   — soft delete (is_active=false)
    GET    /{id}/deliveries        — recent delivery attempts

Security model (see `docs/be-issues/BE-API-008.md` for the full design):

  - All reads/writes are owner-scoped; cross-owner access
    collapses to 404 (NOT 403) to avoid being used as an id oracle.
  - `secret` is returned ONLY in the `POST` response. The
    service is responsible for not echoing it on list / get /
    update / deliveries.
  - `POST` is rate-limited to 10/minute/IP.
  - URL must be HTTPS in production unless
    `Settings.allow_insecure_webhook_targets=True`.
  - `event_types` is bounded to the closed set
    `niki.db.models.webhook.VALID_EVENT_TYPES` and capped at
    `MAX_EVENT_TYPES_PER_SUBSCRIPTION` (10).

i18n keys live in `niki.i18n.messages` and follow the dotted
`webhook.*` namespace. The `error` field is the structured
CODE the frontend branches on; the `message` is rendered from
the active language catalog.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import HttpUrl

from niki.api._i18n import localized_error_response
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.config.settings import get_settings
from niki.db.models.audit_log import SEVERITY_INFO
from niki.db.models.webhook import (
    MAX_EVENT_TYPES_PER_SUBSCRIPTION,
    VALID_EVENT_TYPES,
)
from niki.middleware.rate_limit import get_limiter
from niki.services.audit_log import get_audit_log_service
from niki.services.webhook import get_webhook_dispatcher


router = APIRouter(
    prefix="/webhooks",
    tags=[
        {
            "name": "webhooks",
            "description": (
                "Webhook callback subscriptions (BE-API-008). "
                "Parents can register HTTP endpoints to receive "
                "signed POSTs for async events (account.deleted, "
                "deletion.completed, safety.escalated, …)."
            ),
        },
    ],
)


# Module-local handle for slowapi; mirrors the convention used in
# `niki/api/push.py`. The decorator is applied per route below.
_limiter = get_limiter()


# Map of structured error code -> dotted i18n key. Codes that
# don't have a catalog entry yet fall back to a literal message.
_WEBHOOK_KEY_BY_CODE: dict[str, str] = {
    "WEBHOOK_INVALID_URL": "webhook.invalid_url",
    "WEBHOOK_URL_NOT_HTTPS": "webhook.url_not_https",
    "WEBHOOK_INVALID_EVENT_TYPE": "webhook.invalid_event_type",
    "WEBHOOK_TOO_MANY_EVENT_TYPES": "webhook.too_many_event_types",
    "WEBHOOK_NOT_FOUND": "webhook.not_found",
    "WEBHOOK_RATE_LIMITED": "webhook.rate_limited",
    "WEBHOOK_BAD_REQUEST": "webhook.invalid_url",  # reuse generic
}


def _bad_request(
    request: Request,
    code: str,
    message: str,
) -> HTTPException:
    """Render a 400 with the localized `message` field."""
    key = _WEBHOOK_KEY_BY_CODE.get(code)
    if key is not None:
        return HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code=code,
                key=key,
                fallback_message=message,
            ),
        )
    return HTTPException(
        status_code=400,
        detail={"error": code, "message": message},
    )


def _not_found(request: Request) -> HTTPException:
    """Return the uniform 404 — same body for "doesn't exist"
    and "exists but not yours" so an attacker can't use the
    endpoint as an id oracle."""
    return HTTPException(
        status_code=404,
        detail=localized_error_response(
            request,
            code="WEBHOOK_NOT_FOUND",
            key="webhook.not_found",
        ),
    )


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP for the audit trail.

    Mirrors the helper in `niki/api/auth.py` — `X-Forwarded-For`
    wins when present; first hop is the real client. None rather
    than a placeholder so the audit row honestly records
    "we don't know".
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_url(request: Request, raw: Any) -> str:
    """Validate + normalise the target URL.

    Pydantic `HttpUrl` is used first (catches non-URL garbage)
    and then we enforce the scheme rule. Returns the canonical
    string form (Pydantic adds a trailing slash on bare hostnames
    like `https://example.com` → `https://example.com/`, which
    we'd rather have explicit).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise _bad_request(request, "WEBHOOK_INVALID_URL", "target_url is required")
    try:
        parsed = HttpUrl(raw)
    except Exception as exc:
        raise _bad_request(
            request,
            "WEBHOOK_INVALID_URL",
            f"target_url is not a valid URL ({exc})",
        ) from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise _bad_request(
            request,
            "WEBHOOK_INVALID_URL",
            f"target_url scheme must be http or https (got {scheme!r})",
        )
    settings = get_settings()
    if scheme == "http" and not settings.allow_insecure_webhook_targets:
        raise _bad_request(
            request,
            "WEBHOOK_URL_NOT_HTTPS",
            "target_url must be https in production",
        )
    return str(parsed)


def _validate_event_types(request: Request, value: Any) -> list[str]:
    """Validate `event_types`.

    Three rules, in order:
      1. Non-empty list of strings.
      2. Each entry is in `VALID_EVENT_TYPES`.
      3. Total length ≤ `MAX_EVENT_TYPES_PER_SUBSCRIPTION`.
    """
    if not isinstance(value, list) or not value:
        raise _bad_request(
            request,
            "WEBHOOK_BAD_REQUEST",
            "event_types must be a non-empty list",
        )
    if not all(isinstance(e, str) and e for e in value):
        raise _bad_request(
            request,
            "WEBHOOK_BAD_REQUEST",
            "event_types entries must be non-empty strings",
        )
    if len(value) > MAX_EVENT_TYPES_PER_SUBSCRIPTION:
        raise _bad_request(
            request,
            "WEBHOOK_TOO_MANY_EVENT_TYPES",
            f"event_types exceeds the {MAX_EVENT_TYPES_PER_SUBSCRIPTION}-item cap",
        )
    unknown = [e for e in value if e not in VALID_EVENT_TYPES]
    if unknown:
        raise _bad_request(
            request,
            "WEBHOOK_INVALID_EVENT_TYPE",
            f"unknown event types: {sorted(set(unknown))}",
        )
    # Preserve caller order, de-dupe.
    seen: set[str] = set()
    deduped: list[str] = []
    for e in value:
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped


def _public_projection(row: dict[str, Any]) -> dict[str, Any]:
    """Strip `secret` from a row for any non-creation response."""
    return {k: v for k, v in row.items() if k != "secret"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/",
    summary="Register a webhook subscription",
    description=(
        "Register a new webhook subscription. The response is "
        "the ONLY place the HMAC `secret` is ever returned — "
        "save it client-side; subsequent reads strip it. "
        "Rate-limited to 10/minute/IP."
    ),
    status_code=201,
)
@_limiter.limit("10/minute")
async def register_webhook(
    request: Request,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Register a new webhook subscription."""
    if not isinstance(payload, dict):
        raise _bad_request(request, "WEBHOOK_BAD_REQUEST", "body must be a JSON object")

    target_url = _validate_url(request, payload.get("target_url"))
    event_types = _validate_event_types(request, payload.get("event_types"))

    dispatcher = get_webhook_dispatcher()
    row = await dispatcher.register(
        owner_id=parent.id,
        target_url=target_url,
        event_types=event_types,
    )

    # BE-COMP-004: every state change → audit row. metadata
    # deliberately omits `secret` (PII + replay risk).
    await get_audit_log_service().log_event(
        "webhook_registered",
        severity=SEVERITY_INFO,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint="/api/v1/webhooks/",
        method="POST",
        reason="Webhook subscription registered",
        metadata={
            "subscription_id": row["id"],
            "target_url": target_url,
            "event_types_count": len(event_types),
        },
        client_ip=_client_ip(request),
    )

    # Response includes `secret` (one-shot). `to_created_dict`
    # is the only public projection that surfaces it.
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "target_url": row["target_url"],
        "secret": row["secret"],
        "event_types": list(row["event_types"]),
        "is_active": row["is_active"],
        "failure_count": row["failure_count"],
        "last_delivery_at": (
            row["last_delivery_at"].isoformat() if row.get("last_delivery_at") else None
        ),
        "disabled_at": (row["disabled_at"].isoformat() if row.get("disabled_at") else None),
        "created_at": (row["created_at"].isoformat() if row.get("created_at") else None),
        "updated_at": (row["updated_at"].isoformat() if row.get("updated_at") else None),
    }


@router.get(
    "/",
    summary="List the caller's webhook subscriptions",
    description=(
        "List every subscription owned by the caller. The `secret` field is NEVER included."
    ),
)
async def list_webhooks(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List the caller's webhook subscriptions (no secrets)."""
    dispatcher = get_webhook_dispatcher()
    rows = await dispatcher.list_for_owner(parent.id)
    return {
        "count": len(rows),
        "subscriptions": [_public_projection(r) for r in rows],
    }


@router.get(
    "/{subscription_id}",
    summary="Fetch a single subscription",
    description=(
        "Fetch a single subscription by id. Cross-owner access "
        "returns 404 (NOT 403) to avoid id-existence oracle. "
        "`secret` is NEVER included."
    ),
)
async def get_webhook(
    subscription_id: str,
    request: Request,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Fetch a single subscription (no secret)."""
    dispatcher = get_webhook_dispatcher()
    row = await dispatcher.get(subscription_id, parent.id)
    if row is None:
        raise _not_found(request)
    return _public_projection(row)


@router.patch(
    "/{subscription_id}",
    summary="Partially update a subscription",
    description=(
        "Partially update a subscription. Only the fields "
        "present in the body are touched. Cross-owner access "
        "returns 404."
    ),
)
async def update_webhook(
    request: Request,
    subscription_id: str,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Partial update of a subscription."""
    if not isinstance(payload, dict):
        raise _bad_request(request, "WEBHOOK_BAD_REQUEST", "body must be a JSON object")
    target_url: str | None = None
    event_types: list[str] | None = None
    is_active: bool | None = None
    fields_changed: list[str] = []
    if "target_url" in payload and payload["target_url"] is not None:
        target_url = _validate_url(request, payload["target_url"])
        fields_changed.append("target_url")
    if "event_types" in payload and payload["event_types"] is not None:
        event_types = _validate_event_types(request, payload["event_types"])
        fields_changed.append("event_types")
    if "is_active" in payload and payload["is_active"] is not None:
        if not isinstance(payload["is_active"], bool):
            raise _bad_request(
                request,
                "WEBHOOK_BAD_REQUEST",
                "is_active must be a boolean",
            )
        is_active = payload["is_active"]
        fields_changed.append("is_active")

    if not fields_changed:
        raise _bad_request(
            request,
            "WEBHOOK_BAD_REQUEST",
            "no updatable fields provided",
        )

    dispatcher = get_webhook_dispatcher()
    row = await dispatcher.update(
        subscription_id,
        parent.id,
        target_url=target_url,
        event_types=event_types,
        is_active=is_active,
    )
    if row is None:
        raise _not_found(request)

    await get_audit_log_service().log_event(
        "webhook_updated",
        severity=SEVERITY_INFO,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint=f"/api/v1/webhooks/{subscription_id}",
        method="PATCH",
        reason="Webhook subscription updated",
        metadata={
            "subscription_id": subscription_id,
            "target_url": row.get("target_url"),
            "fields_changed": sorted(fields_changed),
        },
        client_ip=_client_ip(request),
    )
    return _public_projection(row)


@router.delete(
    "/{subscription_id}",
    summary="Soft-delete a subscription",
    description=(
        "Soft-delete a subscription (`is_active=false` + "
        "`disabled_at=now`). The row is preserved for audit. "
        "Cross-owner access returns 404."
    ),
)
async def disable_webhook(
    request: Request,
    subscription_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Soft-delete a subscription."""
    dispatcher = get_webhook_dispatcher()
    row = await dispatcher.disable(subscription_id, parent.id)
    if row is None:
        raise _not_found(request)
    await get_audit_log_service().log_event(
        "webhook_disabled",
        severity=SEVERITY_INFO,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint=f"/api/v1/webhooks/{subscription_id}",
        method="DELETE",
        reason="Webhook subscription disabled",
        metadata={
            "subscription_id": subscription_id,
            "target_url": row.get("target_url"),
        },
        client_ip=_client_ip(request),
    )
    return _public_projection(row)


@router.get(
    "/{subscription_id}/deliveries",
    summary="Recent delivery attempts for a subscription",
    description=(
        "Recent delivery attempts, newest first. `limit` "
        "defaults to 50 and is capped at 100. Cross-owner "
        "access returns 404."
    ),
)
async def list_deliveries(
    subscription_id: str,
    request: Request,
    limit: int = 50,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List recent delivery attempts."""
    if limit <= 0:
        limit = 50
    if limit > 100:
        limit = 100
    dispatcher = get_webhook_dispatcher()
    rows = await dispatcher.get_deliveries(subscription_id, parent.id, limit=limit)
    if rows is None:
        raise _not_found(request)
    return {
        "count": len(rows),
        "deliveries": rows,
    }
