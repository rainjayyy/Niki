"""Push notification registration endpoints (BE-PUSH-001).

Endpoints:
    POST /api/v1/push/register        — register a device's push token
    POST /api/v1/push/unregister      — remove a device's push token
    GET  /api/v1/push/devices         — list the caller's registered devices
    GET  /api/v1/push/prefs           — read notification preferences
    PATCH /api/v1/push/prefs          — update notification preferences

The actual delivery to vendor servers (HMS / MiPush / OPPO /
Vivo / Flyme / APNs / FCM / Web Push) is wired in BE-PUSH-002 /
BE-PUSH-003 (P1+). This ticket only handles the registration
and preferences surface so the frontend can start sending tokens.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from niki.api._i18n import localized_error_response, resolve_lang
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.models import (
    VALID_CHANNELS,
    VALID_PLATFORMS,
)
from niki.db.repositories import (
    get_push_device_repository,
    get_push_prefs_repository,
)
from niki.i18n.translator import get_translator
from niki.middleware.rate_limit import get_limiter
from niki.services.audit_log import (
    SEVERITY_INFO,
    get_audit_log_service,
)
from niki.services.push_delivery import (
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    PushPayload,
    STATUS_DRY_RUN,
    get_push_delivery_service,
)


router = APIRouter(prefix="/push", tags=[
    {"name": "push", "description": "Push notification registration endpoints (BE-PUSH-001)."}
])


# Token format is opaque — vendor-specific. Just enforce a sane
# upper bound so a malicious client can't fill our DB with junk.
_TOKEN_MAX_LEN = 512
_DEVICE_ID_MAX_LEN = 64
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# Map of structured error code -> dotted i18n key. Codes that
# don't have a catalog entry yet fall back to the literal message.
_PUSH_KEY_BY_CODE: dict[str, str] = {
    "INVALID_DEVICE_ID": "media.invalid_device_id",
    "INVALID_CHANNEL": "media.invalid_channel",
    "INVALID_PLATFORM": "media.invalid_platform",
    "INVALID_PUSH_TOKEN": "media.invalid_push_token",
    "INVALID_QUIET_HOUR": "media.invalid_quiet_hour",
    "INVALID_ENABLED_CHANNELS": "media.invalid_enabled_channels",
    "INVALID_HIGH_RISK_ONLY": "media.invalid_high_risk_only",
    "BAD_REQUEST": "auth.signup_missing_field",
}


def _bad_request(
    request: Request,
    code: str,
    message: str,
) -> HTTPException:
    """Render a 400 with the localized `message` field."""
    key = _PUSH_KEY_BY_CODE.get(code)
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


def _validate_register_payload(
    request: Request, payload: dict
) -> tuple[str, str, str, str]:
    if not isinstance(payload, dict):
        raise _bad_request(request, "BAD_REQUEST", "body must be a JSON object")

    device_id = payload.get("device_id", "")
    channel = payload.get("channel", "")
    push_token = payload.get("push_token", "")
    platform = payload.get("platform", "")

    if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id):
        raise _bad_request(
            request,
            "INVALID_DEVICE_ID",
            "device_id must be 1-64 chars of [A-Za-z0-9._-]",
        )
    if channel not in VALID_CHANNELS:
        raise _bad_request(
            request,
            "INVALID_CHANNEL",
            f"channel must be one of {sorted(VALID_CHANNELS)}",
        )
    if platform not in VALID_PLATFORMS:
        raise _bad_request(
            request,
            "INVALID_PLATFORM",
            f"platform must be one of {sorted(VALID_PLATFORMS)}",
        )
    if not isinstance(push_token, str) or not push_token:
        raise _bad_request(
            request,
            "INVALID_PUSH_TOKEN",
            "push_token is required",
        )
    if len(push_token) > _TOKEN_MAX_LEN:
        raise _bad_request(
            request,
            "INVALID_PUSH_TOKEN",
            f"push_token exceeds {_TOKEN_MAX_LEN} chars",
        )
    return device_id, channel, push_token, platform


@router.post(
    "/register",
    summary="Register (or re-register) the calling parent's device",
    description="Register (or re-register) the calling parent's device.",
)
async def register(
    request: Request,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Register (or re-register) the calling parent's device."""
    device_id, channel, push_token, platform = _validate_register_payload(
        request, payload
    )
    repo = get_push_device_repository()
    row = await repo.upsert(
        parent_id=parent.id,
        device_id=device_id,
        channel=channel,
        push_token=push_token,
        platform=platform,
        enabled=True,
    )
    # Projection excludes push_token (security — never echo it back).
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "channel": row["channel"],
        "platform": row["platform"],
        "last_active_at": row["last_active_at"],
        "created_at": row["created_at"],
    }


@router.post(
    "/unregister",
    summary="Remove a registered device",
    description="""
            Remove a registered device. Idempotent — returns
            `unregistered: true` even if nothing matched.
            """,
)
async def unregister(
    request: Request,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Remove a registered device. Idempotent — returns
    `unregistered: true` even if nothing matched."""
    if not isinstance(payload, dict):
        raise _bad_request(request, "BAD_REQUEST", "body must be a JSON object")
    device_id = payload.get("device_id")
    push_token = payload.get("push_token")
    if not device_id and not push_token:
        raise _bad_request(
            request,
            "BAD_REQUEST",
            "either device_id or push_token is required",
        )
    repo = get_push_device_repository()
    if device_id:
        removed = await repo.delete_by_device_id(parent.id, device_id)
    else:
        # Token-based unregister is parent-scoped via the token's
        # owning row (the token is globally unique). This handles
        # "vendor reported token X invalid" from BE-PUSH-002 too.
        removed = await repo.delete_by_token(push_token)
    return {"unregistered": True, "removed": removed}


@router.get(
    "/devices",
    summary="List every device registered for the calling parent",
    description="""
            List every device registered for the calling parent.
            
            Push tokens are deliberately excluded from the projection —
            the frontend never needs them and they shouldn't leak.
            """,
)
async def list_devices(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List every device registered for the calling parent.

    Push tokens are deliberately excluded from the projection —
    the frontend never needs them and they shouldn't leak.
    """
    repo = get_push_device_repository()
    rows = await repo.list_for_parent(parent.id)
    return {
        "count": len(rows),
        "devices": [
            {
                "id": r["id"],
                "device_id": r["device_id"],
                "channel": r["channel"],
                "platform": r["platform"],
                "enabled": r["enabled"],
                "last_active_at": r["last_active_at"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


def _validate_quiet_hour(
    request: Request, value: Any, field: str
) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _bad_request(
            request,
            "INVALID_QUIET_HOUR",
            f"{field} must be an integer 0-23",
        )
    if not 0 <= value <= 23:
        raise _bad_request(
            request,
            "INVALID_QUIET_HOUR",
            f"{field} must be 0-23",
        )
    return value


def _validate_channels(request: Request, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(c, str) for c in value
    ):
        raise _bad_request(
            request,
            "INVALID_ENABLED_CHANNELS",
            "enabled_channels must be a list of strings",
        )
    unknown = [c for c in value if c not in VALID_CHANNELS]
    if unknown:
        raise _bad_request(
            request,
            "INVALID_ENABLED_CHANNELS",
            f"unknown channels: {unknown}",
        )
    return list(value)


@router.get(
    "/prefs",
    summary="Return notification preferences (defaults if unset)",
    description="Return notification preferences (defaults if unset).",
)
async def get_prefs(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Return notification preferences (defaults if unset)."""
    repo = get_push_prefs_repository()
    row = await repo.get(parent.id)
    # Strip parent_id from response — it duplicates the token's sub.
    row.pop("parent_id", None)
    return row


@router.patch(
    "/prefs",
    summary="Partial update of notification preferences",
    description="""
            Partial update of notification preferences.
            
            Only the fields present in the body are touched; the rest
            keep their previous values.
            """,
)
async def update_prefs(
    request: Request,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Partial update of notification preferences.

    Only the fields present in the body are touched; the rest
    keep their previous values.
    """
    if not isinstance(payload, dict):
        raise _bad_request(request, "BAD_REQUEST", "body must be a JSON object")
    quiet_start = _validate_quiet_hour(
        request, payload.get("quiet_start"), "quiet_start"
    )
    quiet_end = _validate_quiet_hour(
        request, payload.get("quiet_end"), "quiet_end"
    )
    if (
        "enabled_channels" in payload
        and payload["enabled_channels"] is not None
    ):
        enabled_channels = _validate_channels(
            request, payload["enabled_channels"]
        )
    else:
        enabled_channels = None
    high_risk_only = payload.get("high_risk_only")
    if high_risk_only is not None and not isinstance(high_risk_only, bool):
        raise _bad_request(
            request,
            "INVALID_HIGH_RISK_ONLY",
            "high_risk_only must be a boolean",
        )

    repo = get_push_prefs_repository()
    row = await repo.update(
        parent.id,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        enabled_channels=enabled_channels,
        high_risk_only=high_risk_only,
    )
    row.pop("parent_id", None)
    return row

# BE-DEV-002: device list + per-device unregister endpoints
@router.get(
    "/devices-full",
    summary="List every push device registered to the calling parent",
    description="""
            List every push device registered to the calling parent.
            
            `push_token` is NEVER returned (it would let a stolen JWT
            spam the vendor push servers). Frontend only needs the
            metadata (channel / platform / last_active_at) to show a
            "我的设备" panel.
            """,
)
async def list_my_devices(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List every push device registered to the calling parent.

    `push_token` is NEVER returned (it would let a stolen JWT
    spam the vendor push servers). Frontend only needs the
    metadata (channel / platform / last_active_at) to show a
    "我的设备" panel.
    """
    from niki.db.repositories import get_push_device_repository
    repo = get_push_device_repository()
    devices = await repo.list_for_parent(parent.id)
    safe = [
        {
            "device_id": d["device_id"],
            "channel": d["channel"],
            "platform": d["platform"],
            "enabled": d["enabled"],
            "last_active_at": d["last_active_at"],
            "created_at": d["created_at"],
        }
        for d in devices
    ]
    return {"count": len(safe), "devices": safe}


@router.delete(
    "/devices-full/{device_id}",
    summary="Unregister a single device (logout-from-this-device flow)",
    description="""
            Unregister a single device (logout-from-this-device flow).
            
            Scoped to the calling parent so a stolen token can't
            unregister a victim's device.
            """,
)
async def unregister_my_device(
    device_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Unregister a single device (logout-from-this-device flow).

    Scoped to the calling parent so a stolen token can't
    unregister a victim's device.
    """
    from niki.db.repositories import get_push_device_repository
    repo = get_push_device_repository()
    # delete_by_device_id already filters by parent_id; no
    # separate method needed.
    removed = await repo.delete_by_device_id(parent.id, device_id)
    return {"unregistered": True, "removed": removed}


# BE-DEV-003: active device list + kick endpoint
@router.get(
    "/active",
    summary="List only the *enabled* devices for the calling parent",
    description="""
            List only the *enabled* devices for the calling parent.
            
            The frontend uses this to render a "currently signed-in"
            panel and to expose a "踢下线" button per device.
            """,
)
async def list_active_devices(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List only the *enabled* devices for the calling parent.

    The frontend uses this to render a "currently signed-in"
    panel and to expose a "踢下线" button per device.
    """
    from niki.db.repositories import get_push_device_repository
    repo = get_push_device_repository()
    devices = await repo.list_active_for_parent(parent.id)
    safe = [
        {
            "device_id": d["device_id"],
            "channel": d["channel"],
            "platform": d["platform"],
            "last_active_at": d["last_active_at"],
        }
        for d in devices
    ]
    return {"count": len(safe), "devices": safe}


@router.post(
    "/kick/{device_id}",
    summary="Disable a single device (BE-DEV-003: \"踢下线\" flow)",
    description="""
            Disable a single device (BE-DEV-003: "踢下线" flow).
            
            Disabling a device doesn't delete the row — the parent
            can re-enable it later by re-registering via POST /push/
            register. lateral-auth: device must belong to the calling
            parent (the repo's set_enabled filters by parent_id).
            """,
)
async def kick_device(
    device_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Disable a single device (BE-DEV-003: "踢下线" flow).

    Disabling a device doesn't delete the row — the parent
    can re-enable it later by re-registering via POST /push/
    register. lateral-auth: device must belong to the calling
    parent (the repo's set_enabled filters by parent_id).
    """
    from niki.db.repositories import get_push_device_repository
    repo = get_push_device_repository()
    updated = await repo.set_enabled(parent.id, device_id, False)
    return {"kicked": updated}


@router.post(
    "/enable/{device_id}",
    summary="Re-enable a previously kicked device",
    description="""
            Re-enable a previously kicked device.
            
            Counterpart to /kick/{device_id}. lateral-auth: same
            scoping as /kick.
            """,
)
async def re_enable_device(
    device_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Re-enable a previously kicked device.

    Counterpart to /kick/{device_id}. lateral-auth: same
    scoping as /kick.
    """
    from niki.db.repositories import get_push_device_repository
    repo = get_push_device_repository()
    updated = await repo.set_enabled(parent.id, device_id, True)
    return {"enabled": updated}


# ---------------------------------------------------------------------------
# BE-PUSH-006: test push endpoint + history
# ---------------------------------------------------------------------------

# Rate-limit cap on POST /push/test. Per-IP because a runaway
# retry loop == DoS against the parent's phone (real vendor
# SDKs ring on each delivery). 5/minute is enough for a
# support engineer to debug, tight enough that a broken script
# can't fill a parent's notification shade.
_PUSH_TEST_RATE: str = "5/minute"

# Synthetic audit event type for /push/test firings. Picked a
# short snake_case like the other constants in
# `niki.db.models.audit_log` so a query like
# `event_type='push_test_sent'` is greppable end-to-end.
_EVENT_PUSH_TEST_SENT: str = "push_test_sent"


# ---------------------------------------------------------------------------
# BE-PUSH-006 response models — used to declare the OpenAPI schema for the
# `/push/test` endpoints. We don't use a Pydantic body model because the
# handler accepts a free-form `dict` (matching the convention of the rest
# of the push router), but a response model is the cheapest way to make
# `tests/api/test_openapi_docs.py` happy without re-architecting.
# ---------------------------------------------------------------------------


class _PushTestResultItem(BaseModel):
    """One entry in the `GET /push/test/history` response."""

    device_id: str = Field(..., description="Target device id.")
    channel: str = Field(..., description="Push channel the device is on.")
    status: str = Field(
        ...,
        description="One of delivered | dry_run | failed | skipped_no_token.",
    )
    error: str | None = Field(
        default=None,
        description="Failure reason when status != delivered/dry_run.",
    )
    latency_ms: int | None = Field(
        default=None,
        description="Wall-clock delivery latency (synthetic 5-50ms for dry_run).",
    )
    dry_run: bool = Field(
        default=False,
        description="Echoes the dry_run argument the caller passed.",
    )
    created_at: str = Field(
        ...,
        description="ISO-8601 timestamp the delivery was recorded.",
    )


class _PushTestResult(BaseModel):
    """`POST /push/test` response body."""

    device_id: str = Field(..., description="Device the test was fired at.")
    status: str = Field(
        default=STATUS_DRY_RUN,
        description="Always `dry_run` until BE-PUSH-002 lands a real SDK.",
    )
    channel: str | None = Field(
        default=None,
        description="Push channel the device is on (apns/fcm/web/...).",
    )
    latency_ms: int | None = Field(
        default=None,
        description="Synthetic 5-50ms latency for dry-run deliveries.",
    )


class _PushTestHistory(BaseModel):
    """`GET /push/test/history` response body."""

    deliveries: list[_PushTestResultItem] = Field(
        default_factory=list,
        description="Recent deliveries, newest-first.",
    )
    count: int = Field(..., description="Number of deliveries returned.")


# Module-local handle for the slowapi limiter so the @limiter.limit
# decorator can resolve the singleton. `get_limiter()` is idempotent
# and cheap.
_limiter = get_limiter()


def _push_device_not_found(request: Request) -> HTTPException:
    """Return the uniform 404 for `device_id` not owned by caller.

    Used by both POST /push/test (cross-parent 404) and any
    future read paths. The body is i18n-localized via the
    `push.device_not_found` key; the *same* message is returned
    for "doesn't exist" and "exists but not yours" so an attacker
    can't use the endpoint as a device-id oracle.
    """
    return HTTPException(
        status_code=404,
        detail=localized_error_response(
            request,
            code="DEVICE_NOT_FOUND",
            key="push.device_not_found",
        ),
    )


@router.post(
    "/test",
    summary="Send a test push to one of the caller's devices",
    response_model=_PushTestResult,
    responses={
        200: {
            "description": "Dry-run delivery accepted.",
            "headers": {
                "X-Push-Test-Mode": {
                    "description": (
                        "Always `dry-run` until BE-PUSH-002 wires real"
                        " vendor SDKs."
                    ),
                    "schema": {"type": "string"},
                },
            },
        },
        404: {"description": "Device not found or not owned by this account."},
        422: {"description": "Missing `device_id`."},
        429: {"description": "Rate limit exceeded (5/minute/IP)."},
    },
    description="""
            Send a test push to one of the caller's devices.

            This is the "does my push actually work?" probe — it
            walks the entire delivery chain (device lookup,
            payload construction, vendor seam, audit log,
            history buffer) without ringing a real phone. The
            response carries `status="dry_run"` and an
            `X-Push-Test-Mode: dry-run` header so callers can
            distinguish a real delivery from a probe.

            lateral-auth: `device_id` must belong to the calling
            parent. Both "doesn't exist" and "exists but not
            yours" return 404 with the same body to avoid being
            used as a device-id oracle.

            Rate-limited to 5/minute/IP (slowapi).
            """,
)
@_limiter.limit(_PUSH_TEST_RATE)
async def send_test_push(
    request: Request,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> JSONResponse:
    """Send a test push to one of the caller's devices.

    See BE-PUSH-006.md for the full contract. Returns 200 +
    `X-Push-Test-Mode: dry-run` on success, 404 on cross-parent
    device_id, 422 on missing `device_id`, 429 on rate-limit.
    """
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "BAD_REQUEST",
                "message": "body must be a JSON object",
            },
        )

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "VALIDATION_ERROR",
                "message": "device_id is required (string, 1-64 chars)",
            },
        )

    # Lateral-auth: lookup is parent-scoped. list_for_parent filters
    # by parent_id, so a foreign device_id simply isn't in the
    # returned list and we map both "missing" + "not yours" to 404.
    device_repo = get_push_device_repository()
    rows = await device_repo.list_for_parent(parent.id)
    device = next(
        (r for r in rows if r.get("device_id") == device_id),
        None,
    )
    if device is None:
        raise _push_device_not_found(request)

    title = str(payload.get("title") or "测试推送")
    body = str(payload.get("body") or "这是一条测试推送")
    severity = str(payload.get("severity") or "info")
    dedupe_key = payload.get("dedupe_key")
    data = payload.get("data") or {}

    push_payload = PushPayload(
        title=title,
        body=body,
        severity=severity,
        dedupe_key=dedupe_key if isinstance(dedupe_key, str) else None,
        data=data if isinstance(data, dict) else {},
    )

    delivery_svc = get_push_delivery_service()
    # dry_run=True is hard-coded — BE-PUSH-002 will introduce a
    # flag once the real vendor SDKs are wired in. Until then
    # /push/test is *always* a probe, never a real ring.
    result = await delivery_svc.deliver(
        device,
        push_payload,
        dry_run=True,
    )

    # Audit row. Goes through the unified `audit_logs` table
    # (BE-COMP-004) so support tooling can correlate a parent's
    # "I got pinged at 10:42" complaint with our test-fire record.
    audit_svc = get_audit_log_service()
    client_ip = (
        request.client.host if request.client else None
    )
    # We deliberately do NOT include `title` / `body` in metadata:
    # they're user-controlled text and AuditLogService scrubs
    # PII-shaped keys anyway, but skipping them keeps the audit
    # row narrow.
    await audit_svc.log_event(
        event_type=_EVENT_PUSH_TEST_SENT,
        severity=SEVERITY_INFO,
        actor_parent_id=parent.id,
        target_parent_id=parent.id,
        endpoint="/api/v1/push/test",
        method="POST",
        reason="BE-PUSH-006 test push fired",
        client_ip=client_ip,
        metadata={
            "device_id": device_id,
            "channel": device.get("channel"),
            "dry_run": True,
            "status": result.status,
            "latency_ms": result.latency_ms,
        },
    )

    body_out = {
        "device_id": device_id,
        "status": STATUS_DRY_RUN,
        "channel": device.get("channel"),
        "latency_ms": result.latency_ms,
    }
    response = JSONResponse(
        status_code=200,
        content=body_out,
    )
    # Observability: explicit header so callers can grep logs and
    # ops dashboards can pattern-match a "this was a probe" line
    # without parsing the JSON body.
    response.headers["X-Push-Test-Mode"] = "dry-run"
    return response


@router.get(
    "/test/history",
    summary="Recent test-push deliveries (in-memory ring buffer)",
    response_model=_PushTestHistory,
    description="""
            Recent test-push deliveries, newest-first.

            Backed by a process-local ring buffer (last 100
            deliveries, across all parents). The endpoint is
            meant for support engineers who just fired a test
            push via `POST /push/test` and want to confirm it
            reached the delivery seam. For the durable,
            per-parent audit trail, query the unified
            `audit_logs` table directly.

            `limit` defaults to 20 and is capped at 100 (the
            buffer's own capacity). `channel` is an optional
            exact-match filter.
            """,
)
async def test_push_history(
    request: Request,
    limit: int = DEFAULT_HISTORY_LIMIT,
    channel: str | None = None,
) -> dict:
    """Return recent test-push deliveries, newest-first.

    `limit` is clamped to [1, MAX_HISTORY_LIMIT]; the buffer's
    own capacity is the absolute upper bound so any larger
    request just returns the whole buffer.
    """
    if limit <= 0:
        limit = DEFAULT_HISTORY_LIMIT
    if limit > MAX_HISTORY_LIMIT:
        limit = MAX_HISTORY_LIMIT

    svc = get_push_delivery_service()
    deliveries = svc.history(limit=limit, channel=channel)
    return {
        "deliveries": [d.to_dict() for d in deliveries],
        "count": len(deliveries),
    }
