"""Children API endpoints (BE-STOR-001-ext-E).

POST /api/v1/parents/{parent_id}/children
  - Registers a child under the calling parent.
  - Lateral-auth check: the parent_id in the path must match the
    token's parent.id (403 PARENT_MISMATCH otherwise), and the child
    cannot already be owned by another parent (409 CHILD_OWNED).
  - PARENT_MISMATCH is recorded via AuthAuditLogger
    (BE-AUTH-001-rollout-audit).

GET /api/v1/parents/{parent_id}/children
  - Lists child_ids owned by the calling parent.
  - PARENT_MISMATCH is recorded via AuthAuditLogger.
"""

from __future__ import annotations

from typing import Optional

import structlog

from fastapi import APIRouter
from fastapi import Depends, HTTPException, Request

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.services.auth_audit import get_auth_audit_logger

logger = structlog.get_logger(__name__)

from niki.services.child_registry import (
    get_child_registry,
    persist_child_to_postgres,
)

router = APIRouter(
    prefix="/children",
    tags=[{"name": "children", "description": "Children API endpoints (BE-STOR-001-ext-E)."}],
)


async def _audit_parent_mismatch(
    request: Request,
    parent: ParentAccount,
    target_parent_id: str,
    endpoint_suffix: str,
) -> None:
    """Emit a `parent_mismatch` audit event for the children admin routes."""
    endpoint = f"{request.method} /api/v1/children/{endpoint_suffix}"
    client_ip: Optional[str] = request.client.host if request.client else None
    await get_auth_audit_logger().log_violation(
        "parent_mismatch",
        actor_parent_id=parent.id,
        target_parent_id=target_parent_id,
        reason=(
            f"Parent {parent.id!r} tried to act as {target_parent_id!r} via children admin endpoint"
        ),
        endpoint=endpoint,
        method=request.method,
        client_ip=client_ip,
    )


@router.post(
    "/register/{parent_id}",
    summary="Register a child under a parent",
    description="""
            Register a child under a parent.
            
            Body: {"child_id": "<uuid>", "name": "<optional>"}
            
            Authorization: the path parent_id must equal the token's
            parent.id; the child_id must not already be owned by another
            parent.
            """,
)
async def register_child(
    request: Request,
    parent_id: str,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Register a child under a parent.

    Body: {"child_id": "<uuid>", "name": "<optional>"}

    Authorization: the path parent_id must equal the token's
    parent.id; the child_id must not already be owned by another
    parent.
    """
    if parent.id != parent_id:
        await _audit_parent_mismatch(request, parent, parent_id, f"register/{parent_id}")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_MISMATCH",
                "message": "Token does not own this parent_id",
            },
        )

    child_id = payload.get("child_id")
    if not child_id or not isinstance(child_id, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "BAD_REQUEST", "message": "child_id is required"},
        )
    name = payload.get("name", "(child)")

    registry = get_child_registry()
    existing = registry.get_parent_of(child_id)
    if existing is not None and existing != parent_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "CHILD_OWNED",
                "message": f"child {child_id!r} already owned by another parent",
            },
        )
    registry.register(child_id, parent_id, name=name)
    try:
        await persist_child_to_postgres(child_id, parent_id, name=name)
    except Exception as exc:
        logger.warning("children.persist_failed", error=str(exc), child_id=child_id)
    return {
        "child_id": child_id,
        "parent_id": parent_id,
        "name": name,
        "status": "registered",
    }


@router.get(
    "/list/{parent_id}",
    summary="List child_ids owned by the calling parent",
    description="List child_ids owned by the calling parent.",
)
async def list_children(
    request: Request,
    parent_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List child_ids owned by the calling parent."""
    if parent.id != parent_id:
        await _audit_parent_mismatch(request, parent, parent_id, f"list/{parent_id}")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_MISMATCH",
                "message": "Token does not own this parent_id",
            },
        )
    child_ids = get_child_registry().list_children_of(parent_id)
    children: list[dict[str, str]] = [{"id": cid, "name": ""} for cid in child_ids]
    if child_ids:
        try:
            from sqlalchemy import text

            from niki.infrastructure.postgres import PostgresClient

            async for session in PostgresClient.get_session():
                rows = (
                    (
                        await session.execute(
                            text("SELECT id, name FROM children WHERE parent_id = :pid"),
                            {"pid": parent_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                by_id = {row["id"]: row["name"] for row in rows}
                children = [{"id": cid, "name": by_id.get(cid, "")} for cid in child_ids]
        except Exception:
            pass
    return {
        "parent_id": parent_id,
        "count": len(child_ids),
        "child_ids": child_ids,
        "children": children,
    }
