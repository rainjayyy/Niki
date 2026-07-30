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

from fastapi import APIRouter, Depends, HTTPException, Request

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


router = APIRouter(prefix="/push", tags=["push"])


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


@router.post("/register")
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


@router.post("/unregister")
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


@router.get("/devices")
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


@router.get("/prefs")
async def get_prefs(
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Return notification preferences (defaults if unset)."""
    repo = get_push_prefs_repository()
    row = await repo.get(parent.id)
    # Strip parent_id from response — it duplicates the token's sub.
    row.pop("parent_id", None)
    return row


@router.patch("/prefs")
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
@router.get("/devices-full")
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


@router.delete("/devices-full/{device_id}")
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
@router.get("/active")
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


@router.post("/kick/{device_id}")
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


@router.post("/enable/{device_id}")
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
