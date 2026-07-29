"""Pagination helpers for BE-API-003.

Two list endpoints (`/memory/graph/entities`,
`/treehole/summaries/{child_id}`) gained `?page=&page_size=`
support. This module owns the parsing + slicing logic so they
stay consistent.
"""

from __future__ import annotations

from typing import Any, TypeVar

from fastapi import HTTPException, Query

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _bad_pagination(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "BAD_PAGINATION", "message": message},
    )


def parse_pagination(
    page: int = Query(1, ge=1),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
    ),
) -> tuple[int, int]:
    """FastAPI dependency: parse and validate ?page=&page_size=.

    Returns (page, page_size). Raises 422 if either is out of range.
    """
    if page < 1:
        raise _bad_pagination("page must be >= 1")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise _bad_pagination(
            f"page_size must be between 1 and {MAX_PAGE_SIZE}"
        )
    return page, page_size


def paginate(
    items: list[T],
    page: int,
    page_size: int,
    *,
    items_key: str = "items",
    total_key: str = "total",
) -> dict[str, Any]:
    """Slice `items` to the requested page and return a JSON-safe
    pagination envelope.

    Args:
        items: the full result set.
        page: 1-indexed page number (already validated by parse_pagination).
        page_size: items per page (already validated).
        items_key: name of the key holding the sliced list in the
            response (default "items"; endpoints override to keep
            their existing field name like "entities" / "summaries").
        total_key: name of the key holding the total count.

    Returns:
        A dict with `total`, `page`, `page_size`, `has_more`, and
        the sliced list under the requested key.
    """
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        total_key: total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        items_key: items[start:end],
    }