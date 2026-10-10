"""Parent-facing notification center (BE-PUSH-005).

Three endpoints around `dashboard_alerts` so parents can review
missed / already-delivered notifications:

    GET  /api/v1/notifications            — cursor-paginated list
    POST /api/v1/notifications/{id}/read  — mark single notification read
    POST /api/v1/notifications/read-all   — mark every unread notification read

The notification record is the existing `DashboardAlert` row — we
don't introduce a new model. A new nullable `read_at` column (added
in the model layer) tracks when the parent actually opened the row.
Severity, title, and body are already present in `payload`.

The `GET /dashboard/alerts/{child_id}` endpoint (BE-RT-002) stays
unchanged — it serves the *child-scoped pending* view (what's still
un-delivered), and the notification center here serves the
*parent-scoped full-history* view (delivered + read).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from niki.api._cursor import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from niki.api._i18n import localized_error_response
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.repositories import get_dashboard_repository


class _EmptyBody(BaseModel):
    """Sentinel body for endpoints that accept no payload.

    FastAPI / OpenAPI require POST/PUT/PATCH endpoints to declare a
    `requestBody` schema, otherwise the auto-generated spec omits
    it and consumers (TS SDK generator, OpenAPI docs test) flag it
    as missing. Wrapping in an empty model keeps the surface honest
    without forcing callers to send `{}`."""

    model_config = {"extra": "forbid"}


router = APIRouter(
    prefix="/notifications",
    tags=[  
        {  # type: ignore[list-item]
            "name": "notifications",
            "description": (
                "Parent-facing notification center (BE-PUSH-005). Cursor-"
                "paginated history of all dashboard alerts owned by the "
                "caller, with mark-single / mark-all read endpoints."
            ),
        }
    ]
)

# Maximum page size (matches `_pagination.MAX_PAGE_SIZE`).
_NOTIF_MAX_LIMIT = 100
_NOTIF_DEFAULT_LIMIT = 20

# Severity set we accept in `?severity=...`. Stored in
# `payload['severity']` (lowercased on read). Anything outside this
# set returns 400 INVALID_SEVERITY.
_VALID_SEVERITIES = frozenset({"info", "warning", "critical", "urgent"})


def _bad_request(
    request: Request,
    code: str,
    key: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=localized_error_response(
            request,
            code=code,
            key=key,
            fallback_message=message,
        ),
    )


def _parse_severity_csv(request: Request, raw: str | None) -> list[str]:
    """Parse `?severity=critical,warning` into a list of lower-cased
    values. Returns `[]` (no filter) when `raw` is None/empty.

    Raises 400 INVALID_SEVERITY on an unknown token.
    """
    if raw is None or not raw.strip():
        return []
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    unknown = [t for t in tokens if t not in _VALID_SEVERITIES]
    if unknown:
        raise _bad_request(
            request,
            "INVALID_SEVERITY",
            "notifications.invalid_severity",
            f"unknown severity values: {unknown}",
        )
    return tokens


def _project_notification(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten an alert row into the parent-facing shape.

    Strips `parent_id` (duplicates the token), keeps `id` (frontend
    needs it to call `/read`), surfaces severity/title/body from
    `payload`.
    """
    payload = dict(row.get("payload") or {})
    return {
        "id": row.get("id"),
        "child_id": row.get("child_id"),
        "alert_type": row.get("alert_type"),
        "severity": payload.get("severity"),
        "title": payload.get("title")
        or payload.get("problem_summary")
        or payload.get("original_text")
        or "",
        "body": payload.get("body")
        or payload.get("action_suggestion")
        or payload.get("niki_response_summary")
        or "",
        "created_at": (
            row["created_at"].isoformat()
            if isinstance(row.get("created_at"), datetime)
            else row.get("created_at")
        ),
        "delivered_at": (
            row["delivered_at"].isoformat()
            if isinstance(row.get("delivered_at"), datetime)
            else row.get("delivered_at")
        ),
        "read_at": (
            row["read_at"].isoformat()
            if isinstance(row.get("read_at"), datetime)
            else row.get("read_at")
        ),
    }


@router.get(
    "",
    summary="List notifications for the calling parent",
    description="""
            List notifications for the calling parent (BE-PUSH-005).

            Cursor-paginated, scoped to `parent.id`. Severity filter is
            case-insensitive CSV (`?severity=critical,warning`). Use
            `unread_only=true` for the "消息中心" red-dot badge count.

            Cursor format is opaque (HMAC-signed). Pass the
            `next_cursor` from the previous response verbatim; do not
            construct cursors client-side.

            Returns at most `limit` rows. `has_more=true` indicates
            another page is available — keep paging until
            `has_more=false` (or `next_cursor=null`).
            """,
)
async def list_notifications(
    request: Request,
    cursor: str | None = Query(
        None,
        description="Opaque cursor from previous response (HMAC-signed).",
    ),
    limit: int = Query(
        _NOTIF_DEFAULT_LIMIT,
        ge=1,
        le=_NOTIF_MAX_LIMIT,
        description=f"Page size, 1..{_NOTIF_MAX_LIMIT} (default {_NOTIF_DEFAULT_LIMIT}).",
    ),
    severity: str | None = Query(
        None,
        description="Comma-sep severity filter, e.g. 'critical,warning'.",
    ),
    unread_only: bool = Query(
        False,
        description="If true, only rows with read_at IS NULL.",
    ),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List notifications for the calling parent (BE-PUSH-005)."""
    severity_filter = _parse_severity_csv(request, severity)

    after_id: str | None = None
    after_ts: datetime | None = None
    if cursor:
        try:
            position = decode_cursor(cursor)
        except CursorError:
            raise _bad_request(
                request,
                "INVALID_CURSOR",
                "notifications.invalid_cursor",
                "cursor is malformed or has been tampered with",
            )
        after_id = position.id
        after_ts = position.ts

    repo = get_dashboard_repository()
    rows = await repo.list_for_parent(
        parent_id=parent.id,
        severity_filter=severity_filter or None,
        unread_only=unread_only,
        after_id=after_id,
        after_ts=after_ts,
        limit=limit,
    )

    has_more = len(rows) == limit
    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        last_ts = last.get("created_at")
        if isinstance(last_ts, datetime):
            next_cursor = encode_cursor(last["id"], last_ts)
        else:
            # Should not happen — every row has a created_at — but
            # fall back to "now" so the cursor is always usable.
            next_cursor = encode_cursor(
                last["id"],
                datetime.now(timezone.utc),
            )

    return {
        "notifications": [_project_notification(r) for r in rows],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.post(
    "/{notification_id}/read",
    summary="Mark a single notification as read",
    description="""
            Mark a single notification as read (BE-PUSH-005).

            Lateral-auth: returns 404 NOTIFICATION_NOT_FOUND if the id
            belongs to another parent (or doesn't exist). 403 would
            leak id-existence, so 404 is intentional.

            Idempotent — calling on an already-read id is a no-op.
            """,
)
async def mark_notification_read(
    request: Request,
    notification_id: str,
    _body: _EmptyBody | None = Body(default=None),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Mark a single notification as read (BE-PUSH-005)."""
    repo = get_dashboard_repository()
    ok = await repo.mark_alert_read(notification_id, parent.id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=localized_error_response(
                request,
                code="NOTIFICATION_NOT_FOUND",
                key="notifications.not_found",
                fallback_message="notification not found",
            ),
        )
    return {"id": notification_id, "read": True}


@router.post(
    "/read-all",
    summary="Mark every unread notification as read (BE-PUSH-005)",
    description="""
            Mark every unread notification as read (BE-PUSH-005).

            Bulk counterpart to `POST /{id}/read`. Idempotent — a
            second call returns `marked: 0`. The caller never has to
            reason about partial success; this endpoint is a single
            round-trip "全部已读" gesture.
            """,
)
async def mark_all_notifications_read(
    _body: _EmptyBody | None = Body(default=None),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Mark every unread notification as read (BE-PUSH-005)."""
    repo = get_dashboard_repository()
    marked = await repo.mark_all_read_for_parent(parent.id)
    return {"marked": marked, "parent_id": parent.id}
