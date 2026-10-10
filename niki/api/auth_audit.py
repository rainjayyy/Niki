"""Auth audit query endpoints (BE-AUTH-001-rollout-audit).

GET /api/v1/auth/audit/recent
  - Returns recent auth audit events for the calling parent.
  - Default scoping: actor_parent_id == token's parent.id
    (prevents callers from probing other parents' audit trails).
  - Optional filters: ?event_type=, ?limit= (cap 200),
    plus the BE-API-004 generic ?since=&until=&sort=&order=&keyword=.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from niki.api._i18n import localized_error_response
from niki.api._pagination import (
    ListQueryParams,
    list_query_params,
    sort_rows,
)
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.repositories.auth_audit_repo import (
    get_auth_audit_log_repository,
)

router = APIRouter(
    prefix="/auth/audit",
    tags=[  
        {  # type: ignore[list-item]
            "name": "auth_audit",
            "description": "Auth audit query endpoints (BE-AUTH-001-rollout-audit).",
        }
    ]
)


_MAX_LIMIT = 200

# Allowed sort fields for /recent. `created_at` is the default
# (matches the pre-BE-API-004 behaviour). Unrecognised values
# rejected as 400 INVALID_SORT.
_AUDIT_ALLOWED_SORTS = ("created_at", "severity")


@router.get(
    "/recent",
    summary="""
            Return the most recent audit events the caller is allowed to see
            """,
    description="""
            Return the most recent audit events the caller is allowed to see.
            
            Security-aware default: `actor_parent_id` is forced to the
            calling parent's id, regardless of any query parameter. This
            blocks the obvious "look up someone else's audit" probe via
            this endpoint. Cross-parent audit access will live behind an
            admin role (BE-ADMIN-001).
            
            BE-API-004: ?since=&until= filter the time window,
            ?keyword= case-insensitive substring over
            actor_parent_id / target_parent_id / target_child_id /
            endpoint, ?sort=&order= choose direction.
            """,
)
async def list_recent_audit(
    request: Request,
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    event_type: Optional[str] = Query(None, max_length=64),
    params: ListQueryParams = Depends(list_query_params),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Return the most recent audit events the caller is allowed to see.

    Security-aware default: `actor_parent_id` is forced to the
    calling parent's id, regardless of any query parameter. This
    blocks the obvious "look up someone else's audit" probe via
    this endpoint. Cross-parent audit access will live behind an
    admin role (BE-ADMIN-001).

    BE-API-004: ?since=&until= filter the time window,
    ?keyword= case-insensitive substring over
    actor_parent_id / target_parent_id / target_child_id /
    endpoint, ?sort=&order= choose direction.
    """
    if limit > _MAX_LIMIT:
        # Defence in depth — Query(le=...) already enforces this,
        # but keep the explicit check in case the validator is
        # ever bypassed.
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="BAD_REQUEST",
                key="auth_audit.bad_request",
                fallback_message=f"limit must be <= {_MAX_LIMIT}",
            ),
        )

    # Whitelist sort field (helper itself does not enforce it).
    if params.sort not in _AUDIT_ALLOWED_SORTS:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="INVALID_SORT",
                key="auth_audit.invalid_sort",
                fallback_message=(f"sort must be one of {sorted(_AUDIT_ALLOWED_SORTS)}"),
            ),
        )

    repo = get_auth_audit_log_repository()
    # Pull a wide window from the repo — the per-endpoint filter
    # (since / until / keyword) is applied here so the in-memory
    # implementation can rely on a simple scan rather than
    # learning a richer query contract.
    rows = await repo.list_recent(
        limit=_MAX_LIMIT,
        event_type=event_type,
        actor_parent_id=parent.id,
    )

    # ---- time-window filter (since inclusive, until exclusive) ----
    if params.since is not None:
        rows = [
            r for r in rows if r.get("created_at") is not None and r["created_at"] >= params.since
        ]
    if params.until is not None:
        rows = [
            r for r in rows if r.get("created_at") is not None and r["created_at"] < params.until
        ]

    # ---- keyword substring filter (case-insensitive) ----
    keyword = params.normalized_keyword()
    if keyword is not None:

        def _matches(row: dict) -> bool:
            for field in (
                "actor_parent_id",
                "target_parent_id",
                "target_child_id",
                "endpoint",
            ):
                value = row.get(field)
                if value is None:
                    continue
                if keyword in str(value).lower():
                    return True
            return False

        rows = [r for r in rows if _matches(r)]

    # ---- sort ----
    rows = sort_rows(
        rows,
        sort=params.sort,
        order=params.order,
        time_field="created_at",
    )

    # ---- apply the limit AFTER filter+sort so a filtered list
    # always returns the newest relevant rows, not random ones.
    rows = rows[:limit]

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
        # Echo the BE-API-004 params so the frontend can confirm
        # what the server understood (matches the convention used
        # by /treehole/summaries + /memory/graph/entities).
        "sort": params.sort,
        "order": params.order,
        "keyword": params.keyword,
    }
