"""Pagination + filter helpers for BE-API-003 / BE-API-004.

Two list endpoints (`/memory/graph/entities`,
`/treehole/summaries/{child_id}`) gained `?page=&page_size=`
support in BE-API-003, and three list endpoints gained
`?since=&until=&sort=&order=&keyword=` support in BE-API-004.

This module owns the parsing + slicing logic so they stay
consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


def _bad_query(message: str, code: str = "BAD_QUERY") -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": code, "message": message},
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


# ---------------------------------------------------------------------------
# BE-API-004: filter / sort / search
# ---------------------------------------------------------------------------


# Field name on the row that holds the primary timestamp used for
# `since` / `until` / default sort. Endpoints that store timestamps
# under a different key override this in the helper.
DEFAULT_TIME_FIELD = "created_at"


@dataclass(frozen=True)
class ListQueryParams:
    """Parsed `?page=&page_size=&since=&until=&sort=&order=&keyword=`.

    Defaults match the pre-BE-API-004 behaviour:
    - `page=1, page_size=20`
    - `sort='created_at', order='desc'` (newest-first)
    - `since=None, until=None`  (no time filter)
    - `keyword=None`           (no keyword filter)

    The `order` value is enforced to be `asc` or `desc` (validated by
    `list_query_params`). The `sort` field is whitelisted per
    endpoint via the `allowed_sorts` argument — the helper itself
    just passes the value through so the endpoint can decide how it
    maps to the underlying repository.
    """

    page: int = 1
    page_size: int = 20
    since: datetime | None = None
    until: datetime | None = None
    sort: str = "created_at"
    order: str = "desc"
    keyword: str | None = None

    @property
    def is_ascending(self) -> bool:
        return self.order == "asc"

    def normalized_keyword(self) -> str | None:
        """Keyword for case-insensitive substring matches."""
        if self.keyword is None:
            return None
        stripped = self.keyword.strip()
        return stripped.lower() if stripped else None


def list_query_params(
    page: int = Query(1, ge=1),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
    ),
    since: datetime | None = Query(
        None,
        description=(
            "Lower bound (inclusive) on the timestamp field "
            "(format: YYYY-MM-DD or ISO 8601)."
        ),
    ),
    until: datetime | None = Query(
        None,
        description=(
            "Upper bound (exclusive) on the timestamp field "
            "(format: YYYY-MM-DD or ISO 8601)."
        ),
    ),
    sort: str = Query(
        "created_at",
        description="Field to sort by (whitelisted per endpoint).",
    ),
    order: str = Query(
        "desc",
        description="Sort direction: 'asc' or 'desc'.",
    ),
    keyword: str | None = Query(
        None,
        max_length=128,
        description="Case-insensitive substring filter.",
    ),
) -> ListQueryParams:
    """FastAPI dependency: parse BE-API-004 query params.

    Returns a `ListQueryParams` dataclass. Raises 400 INVALID_ORDER
    on an unknown sort direction.

    Per-endpoint sort whitelisting lives in the endpoint (this helper
    just passes the value through so the endpoint can decide which
    fields it allows).
    """
    if order not in ("asc", "desc"):
        raise _bad_query(
            "order must be 'asc' or 'desc'", code="INVALID_ORDER",
        )
    if since is not None and until is not None and since > until:
        raise _bad_query(
            "since must be <= until", code="INVALID_RANGE",
        )
    if keyword is not None and not keyword.strip():
        # Treat an all-whitespace keyword the same as None so the
        # default behaviour ("no filter") stays consistent.
        keyword = None
    return ListQueryParams(
        page=page,
        page_size=page_size,
        since=since,
        until=until,
        sort=sort,
        order=order,
        keyword=keyword,
    )


def sort_rows(
    rows: list[T],
    *,
    sort: str,
    order: str,
    time_field: str = DEFAULT_TIME_FIELD,
) -> list[T]:
    """Sort `rows` by `sort` + `order`. Defaults to time_field desc.

    The endpoint is expected to whitelist `sort` against its allowed
    set BEFORE calling this. Unrecognised sort fields fall back to
    sorting by `time_field` (which is the only field every list
    endpoint agrees on).
    """
    reverse = order == "desc"

    def _key(row: Any) -> Any:
        if isinstance(row, dict):
            value = row.get(sort)
        else:
            value = getattr(row, sort, None)
        if value is None:
            # Last-resort fallback so missing fields don't bubble
            # up as a TypeError during sort.
            return getattr(row, time_field, None) if not isinstance(row, dict) else row.get(time_field)
        return value

    return sorted(rows, key=_key, reverse=reverse)
