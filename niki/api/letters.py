"""Parent-to-child letter endpoints.

These power the "给 TA 发个信" flow in the parent dashboard and the
inbox polling in the child chat page:

  POST /parents/{parent_id}/letters          - send a letter
  GET  /children/{child_id}/letters          - child inbox
  POST /children/letters/{letter_id}/read    - mark a letter read

Storage is in-memory by default (see `niki.db.repositories.letter_repo`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent, get_owned_child_id
from niki.db.repositories.letter_repo import get_letter_repository
from niki.services.child_registry import get_child_registry

router = APIRouter(
    prefix="",
    tags=[
        {
            "name": "letters",
            "description": (
                "Parent-to-child letters: send from the dashboard, "
                "poll in the child chat, mark as read."
            ),
        }
    ],
)


class SendLetterRequest(BaseModel):
    """Body for `POST /parents/{parent_id}/letters`."""

    child_id: str = Field(..., min_length=1, max_length=128)
    content: str | None = Field(None, max_length=2000)
    text: str | None = Field(None, max_length=2000)


def _letter_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": code, "message": message},
    )


@router.post(
    "/parents/{parent_id}/letters",
    summary="Send a letter to one of your children",
)
async def send_letter(
    parent_id: str,
    payload: SendLetterRequest,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Create a letter for `child_id` from the calling parent."""
    if parent_id != parent.id:
        raise _letter_error(
            403,
            "PARENT_CHILD_MISMATCH",
            "Token parent does not match the letter sender",
        )

    registered_parent = get_child_registry().get_parent_of(payload.child_id)
    if registered_parent is None:
        raise _letter_error(
            404,
            "CHILD_NOT_FOUND",
            f"No child registered with id {payload.child_id!r}",
        )
    if registered_parent != parent.id:
        raise _letter_error(
            403,
            "PARENT_CHILD_MISMATCH",
            "Token parent does not own this child",
        )

    content = (payload.content or payload.text or "").strip()
    if not content:
        raise _letter_error(
            400,
            "LETTER_EMPTY",
            "Letter content must not be empty",
        )

    letter = await get_letter_repository().create(
        child_id=payload.child_id,
        parent_id=parent.id,
        content=content,
    )
    return {"letter": letter}


@router.get(
    "/children/{child_id}/letters",
    summary="List letters in a child's inbox",
)
async def list_letters(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Return the child's letters (oldest first)."""
    letters = await get_letter_repository().list_for_child(child_id)
    return {"letters": letters}


@router.post(
    "/children/letters/{letter_id}/read",
    summary="Mark a letter as read",
)
async def mark_letter_read(
    letter_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Mark a letter read; the caller must own the letter's child."""
    letter = await get_letter_repository().get(letter_id)
    if letter is None:
        raise _letter_error(404, "LETTER_NOT_FOUND", "Letter not found")

    registered_parent = get_child_registry().get_parent_of(letter["child_id"])
    if registered_parent is not None and registered_parent != parent.id:
        raise _letter_error(
            403,
            "PARENT_CHILD_MISMATCH",
            "Token parent does not own this child",
        )

    updated = await get_letter_repository().mark_read(letter_id)
    return {"id": letter_id, "read": bool(updated and updated["read"])}
