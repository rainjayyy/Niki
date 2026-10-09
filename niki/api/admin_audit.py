"""Unified audit-log query endpoint (BE-COMP-004).

GET /api/v1/admin/audit?event_type=&since=&actor_parent_id=&limit=50
  → the caller's own cross-domain audit history, newest-first.

Why "admin" when v1 only lets a parent read their own rows? Because
the *shape* is the admin shape: one endpoint, actor-scoped, filterable.
The follow-up ticket (BE-ADMIN-001) adds an admin role that lifts the
scoping check below and lets support staff pass an arbitrary
`actor_parent_id`. Putting the route at its final URL now means the
frontend doesn't have to re-point later.

Until then the rule is absolute: `actor_parent_id` is forced to the
JWT's parent id. Passing someone else's id is a 403, not a silent
rewrite — a caller probing for another parent's history should learn
that it was refused, and we want the attempt in the audit log itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from niki.api._i18n import localized_error_response
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.services.audit_log import get_audit_log_service

router = APIRouter(
    prefix="/admin",
    tags=[
        {
            "name": "admin_audit",
            "description": (
                "Unified cross-domain audit log query (BE-COMP-004) — "
                "consent, deletion, safety and auth events in one "
                "history."
            ),
        },
    ],
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


@router.get(
    "/audit",
    summary="Query the unified cross-domain audit log",
    description="""
            Query the unified cross-domain audit log (BE-COMP-004).

            Returns the calling parent's audit history newest-first —
            consent signatures, deletion requests, safety blocks,
            high-risk alerts, logins and lateral-auth violations, all
            from a single table so the parent (or support) does not
            have to stitch four domains together.

            Scoping: `actor_parent_id` is forced to the caller's own
            id. Supplying a different parent's id returns 403
            FORBIDDEN; cross-parent reads land with the admin role in
            BE-ADMIN-001.

            Filters:
            - `event_type` — exact match (e.g. `consent_signed`,
              `safety_block`, `deletion_requested`).
            - `since` — ISO 8601 / `YYYY-MM-DD` lower bound
              (inclusive) on `created_at`.
            - `limit` — 1..200, default 50.

            The rows contain metadata only — no child utterances, no
            parent phone numbers (PIPL data minimisation).
            """,
)
async def query_audit_log(
    request: Request,
    event_type: Optional[str] = Query(None, max_length=64),
    since: Optional[datetime] = Query(
        None,
        description=("Lower bound (inclusive) on created_at (format: YYYY-MM-DD or ISO 8601)."),
    ),
    actor_parent_id: Optional[str] = Query(
        None,
        max_length=16,
        description=(
            "Must equal the caller's own parent id in v1 (cross-parent reads: BE-ADMIN-001)."
        ),
    ),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Query the unified cross-domain audit log (BE-COMP-004).

    Returns the calling parent's audit history newest-first. See the
    endpoint `description` for the full contract.
    """
    if actor_parent_id is not None and actor_parent_id != parent.id:
        raise HTTPException(
            status_code=403,
            detail=localized_error_response(
                request,
                code="FORBIDDEN",
                key="audit.cross_parent_forbidden",
                fallback_message=("You may only query your own audit history"),
            ),
        )

    rows = await get_audit_log_service().list_events(
        limit=limit,
        event_type=event_type,
        actor_parent_id=parent.id,
        since=since,
    )

    return {
        "actor_parent_id": parent.id,
        "event_type_filter": event_type,
        "since": since.isoformat() if since is not None else None,
        "limit": limit,
        "count": len(rows),
        "events": [_serialise(row) for row in rows],
    }


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of an audit row."""
    item = dict(row)
    created = item.get("created_at")
    if isinstance(created, datetime):
        item["created_at"] = created.isoformat()
    item.setdefault("metadata", {})
    return item
