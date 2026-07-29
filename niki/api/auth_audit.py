"""Auth audit query endpoints (BE-AUTH-001-rollout-audit).

GET /api/v1/auth/audit/recent
  - Returns recent auth audit events for the calling parent.
  - Default scoping: actor_parent_id == token's parent.id
    (prevents callers from probing other parents' audit trails).
  - Optional filters: ?event_type=, ?limit= (cap 200).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.repositories.auth_audit_repo import (
    get_auth_audit_log_repository,
)

router = APIRouter(prefix="/auth/audit", tags=["auth_audit"])


_MAX_LIMIT = 200


@router.get("/recent")
async def list_recent_audit(
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    event_type: Optional[str] = Query(None, max_length=64),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Return the most recent audit events the caller is allowed to see.

    Security-aware default: `actor_parent_id` is forced to the
    calling parent's id, regardless of any query parameter. This
    blocks the obvious "look up someone else's audit" probe via
    this endpoint. Cross-parent audit access will live behind an
    admin role (BE-ADMIN-001).
    """
    if limit > _MAX_LIMIT:
        # Defence in depth — Query(le=...) already enforces this,
        # but keep the explicit check in case the validator is
        # ever bypassed.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "BAD_REQUEST",
                "message": f"limit must be <= {_MAX_LIMIT}",
            },
        )

    repo = get_auth_audit_log_repository()
    rows = await repo.list_recent(
        limit=limit,
        event_type=event_type,
        actor_parent_id=parent.id,
    )

    # Serialise datetime fields to ISO 8601 so the response is JSON-safe.
    serialised = []
    for row in rows:
        item = dict(row)
        if item.get("created_at") is not None:
            item["created_at"] = item["created_at"].isoformat()
        serialised.append(item)

    return {
        "actor_parent_id": parent.id,
        "event_type_filter": event_type,
        "limit": limit,
        "count": len(serialised),
        "events": serialised,
    }