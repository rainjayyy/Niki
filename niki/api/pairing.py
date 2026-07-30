"""Parent ↔ Child pairing code endpoints (BE-DEV-004).

Endpoints:

    POST /api/v1/pairing/code      — parent mints a code for a child
    POST /api/v1/pairing/consume   — kid's H5 redeems a code

See `docs/frontend-integration.md` §2.2 (binding flow) and §4.2
(push confirmation to existing parent devices).

Authorization model:

- `POST /pairing/code` requires `get_current_parent`. The parent
  must own the `child_id` in the body (lateral-auth check via the
  existing `ChildRegistry`); otherwise we return 403
  `PARENT_CHILD_MISMATCH`.
- `POST /pairing/consume` is **public** — anyone holding the
  6-digit code can redeem it (the kid's H5 has no JWT yet, since
  it's the very thing that's being bound). The 5-minute TTL keeps
  brute-force impractical (10^6 / 300s ≈ 3.3k guesses/s to hit in
  the window, which the slowapi limiter on this route will reject
  long before).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.auth.pairing import get_pairing_store
from niki.services.child_registry import get_child_registry

router = APIRouter(prefix="/pairing", tags=["pairing"])


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": code, "message": message},
    )


def _require_child_id(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise _bad_request("BAD_REQUEST", "body must be a JSON object")
    child_id = payload.get("child_id")
    if not isinstance(child_id, str) or not child_id:
        raise _bad_request(
            "BAD_REQUEST", "child_id is required and must be a string"
        )
    return child_id


def _require_code(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise _bad_request("BAD_REQUEST", "body must be a JSON object")
    code = payload.get("code")
    if not isinstance(code, str) or not code:
        raise _bad_request(
            "BAD_REQUEST", "code is required and must be a string"
        )
    if len(code) != 6 or not code.isdigit():
        raise _bad_request(
            "BAD_REQUEST", "code must be a 6-digit numeric string"
        )
    return code


@router.post("/code")
async def create_pairing_code(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Mint a 6-digit pairing code for one of the caller's children.

    Body: ``{"child_id": "..."}``

    Returns ``{"code": "123456", "expires_at": "...", "ttl_seconds": 300}``.

    Errors:
        400 BAD_REQUEST              - malformed body
        403 PARENT_CHILD_MISMATCH    - child isn't owned by caller
        404 CHILD_NOT_FOUND          - child isn't registered at all
    """
    child_id = _require_child_id(payload)

    registry = get_child_registry()
    registered_parent = registry.get_parent_of(child_id)
    if registered_parent is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "CHILD_NOT_FOUND",
                "message": f"No child registered with id {child_id!r}",
            },
        )
    if registered_parent != parent.id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "PARENT_CHILD_MISMATCH",
                "message": "Token does not own this child_id",
            },
        )

    record = get_pairing_store().create(
        parent_id=parent.id, child_id=child_id
    )
    return {
        "code": record.code,
        "child_id": record.child_id,
        "parent_id": record.parent_id,
        "expires_at": record.expires_at.isoformat(),
        "ttl_seconds": int(
            (record.expires_at - record.created_at).total_seconds()
        ),
    }


@router.post("/consume")
async def consume_pairing_code(payload: dict) -> dict:
    """Redeem a 6-digit code (called by the kid's H5 — unauthenticated).

    Body: ``{"code": "123456"}``

    Returns ``{"parent_id": "...", "child_id": "...", "bound": true}``
    on success. The code is marked consumed atomically inside the
    store so two concurrent calls cannot both succeed.

    Errors:
        400 BAD_REQUEST              - malformed body
        404 PAIRING_CODE_INVALID     - code unknown, expired, or used
    """
    code = _require_code(payload)
    record = get_pairing_store().consume(code)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "PAIRING_CODE_INVALID",
                "message": "Code is unknown, expired, or already used",
            },
        )
    # NOTE: confirmation push to parent's existing devices lives in
    # BE-PUSH-002 (real vendor delivery) — this ticket only flips
    # the binding flag.
    return {
        "parent_id": record.parent_id,
        "child_id": record.child_id,
        "bound": True,
    }