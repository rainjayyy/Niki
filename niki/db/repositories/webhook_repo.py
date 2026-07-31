"""WebhookRepository — persistence for webhook subscriptions + deliveries (BE-API-008).

Two domain types: `WebhookSubscription` (configuration, one row
per (owner_id, target_url)) and `WebhookDelivery` (append-only log
of dispatch attempts). One ABC + two impls — the same convention
as the rest of the persistence layer (`InMemory*` for tests /
dev, `Postgres*` for production).

Why a single repo rather than two
---------------------------------
A webhook subscription and its deliveries are queried together
(the deliveries endpoint is always per-subscription) and the
dispatcher writes both inside the same logical step. Splitting
them would force every consumer to juggle two services; the
clarity tradeoff is worth it.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from niki.db.models.webhook import (
    MAX_CONSECUTIVE_FAILURES,
    WebhookDelivery,
    WebhookSubscription,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "WebhookRepository",
    "InMemoryWebhookRepository",
    "PostgresWebhookRepository",
    "get_webhook_repository",
    "set_webhook_repository",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class WebhookRepository(ABC):
    """Persistence contract for webhook subscriptions + deliveries."""

    # ----- subscriptions -------------------------------------------------

    @abstractmethod
    async def create_subscription(
        self,
        *,
        owner_id: str,
        target_url: str,
        secret: str,
        event_types: list[str],
    ) -> dict[str, Any]:
        """Insert a new subscription and return it as a dict.

        The returned dict includes `secret` — the caller (the
        service) decides whether to surface it to the API.
        """

    @abstractmethod
    async def get_subscription(
        self, subscription_id: str
    ) -> dict[str, Any] | None:
        """Lookup by id. Returns the row with `secret` populated."""

    @abstractmethod
    async def list_subscriptions_for_owner(
        self, owner_id: str
    ) -> list[dict[str, Any]]:
        """List every subscription belonging to `owner_id`, newest first."""

    @abstractmethod
    async def list_active_subscriptions_for_event(
        self, event_type: str
    ) -> list[dict[str, Any]]:
        """List every active subscription whose `event_types`
        includes `event_type`. Used by `WebhookDispatcher.dispatch`.
        """

    @abstractmethod
    async def update_subscription(
        self,
        subscription_id: str,
        *,
        owner_id: str,
        target_url: str | None = None,
        event_types: list[str] | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any] | None:
        """Owner-scoped partial update.

        Returns the updated row, or `None` if the subscription
        doesn't exist OR isn't owned by `owner_id`. (The 404
        oracle guard in the API layer relies on this — both cases
        must collapse to None.)
        """

    @abstractmethod
    async def disable_subscription(
        self, subscription_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        """Soft-delete: set `is_active=False` + `disabled_at=now`.

        Same 404-oracle guard as `update_subscription` — `None`
        when no matching owned row.
        """

    @abstractmethod
    async def record_delivery_outcome(
        self,
        subscription_id: str,
        *,
        attempt_count: int,
        delivered: bool,
        status_code: int | None,
        last_error: str | None,
    ) -> None:
        """Update the subscription's `failure_count` /
        `last_delivery_at` based on a single dispatch outcome.

        A successful delivery zeros the failure counter; a
        failed one increments it (clamped at
        `MAX_CONSECUTIVE_FAILURES`) and auto-disables the
        subscription when the threshold is reached.
        """

    # ----- deliveries ----------------------------------------------------

    @abstractmethod
    async def create_delivery(
        self,
        *,
        subscription_id: str,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        target_url: str,
    ) -> dict[str, Any] | None:
        """Insert a delivery row.

        Returns the new row (with its assigned id), or `None`
        if a delivery with the same `(event_id, subscription_id)`
        already exists (idempotency).
        """

    @abstractmethod
    async def update_delivery_attempt(
        self,
        delivery_id: str,
        *,
        attempt_count: int,
        last_status_code: int | None,
        last_error: str | None,
        delivered_at: datetime | None,
    ) -> None:
        """Patch the mutable fields of a delivery row after a
        single HTTP attempt. The dispatcher calls this after
        every retry (1s, 4s, 16s)."""

    @abstractmethod
    async def list_deliveries_for_subscription(
        self,
        subscription_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]] | None:
        """List deliveries, newest first.

        Returns `None` when the subscription doesn't exist OR
        isn't owned by `owner_id` — same 404-oracle pattern as
        the other owner-scoped reads.
        """


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryWebhookRepository(WebhookRepository):
    """Thread-safe dict-backed webhook store.

    Two dicts:
      `_subs`:    id -> dict (subscription row)
      `_delivs`:  (event_id, subscription_id) -> dict (delivery row)
      `_by_id`:   delivery_id -> dict (so the dispatcher can
                  patch a specific row after each retry without
                  re-creating it)
    """

    def __init__(self) -> None:
        self._subs: dict[str, dict[str, Any]] = {}
        self._delivs: dict[tuple[str, str], dict[str, Any]] = {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ---- subscriptions ----

    async def create_subscription(
        self,
        *,
        owner_id: str,
        target_url: str,
        secret: str,
        event_types: list[str],
    ) -> dict[str, Any]:
        with self._lock:
            sub_id = f"wh-{len(self._subs) + 1:08d}"
            now = _now()
            row = {
                "id": sub_id,
                "owner_id": owner_id,
                "target_url": target_url,
                "secret": secret,
                "event_types": list(event_types),
                "is_active": True,
                "failure_count": 0,
                "last_delivery_at": None,
                "disabled_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self._subs[sub_id] = row
            return deepcopy(row)

    async def get_subscription(
        self, subscription_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._subs.get(subscription_id)
            return deepcopy(row) if row else None

    async def list_subscriptions_for_owner(
        self, owner_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                deepcopy(r) for r in self._subs.values()
                if r["owner_id"] == owner_id
            ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows

    async def list_active_subscriptions_for_event(
        self, event_type: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [
                deepcopy(r) for r in self._subs.values()
                if r["is_active"] and event_type in r["event_types"]
            ]

    async def update_subscription(
        self,
        subscription_id: str,
        *,
        owner_id: str,
        target_url: str | None = None,
        event_types: list[str] | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._subs.get(subscription_id)
            if row is None or row["owner_id"] != owner_id:
                return None
            if target_url is not None:
                row["target_url"] = target_url
            if event_types is not None:
                row["event_types"] = list(event_types)
            if is_active is not None:
                row["is_active"] = is_active
                if is_active:
                    # Re-enabling a previously disabled subscription
                    # clears `disabled_at` so the support UI shows
                    # the live "active since X" timestamp.
                    row["disabled_at"] = None
                    row["failure_count"] = 0
            row["updated_at"] = _now()
            return deepcopy(row)

    async def disable_subscription(
        self, subscription_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._subs.get(subscription_id)
            if row is None or row["owner_id"] != owner_id:
                return None
            row["is_active"] = False
            row["disabled_at"] = _now()
            row["updated_at"] = row["disabled_at"]
            return deepcopy(row)

    async def record_delivery_outcome(
        self,
        subscription_id: str,
        *,
        attempt_count: int,
        delivered: bool,
        status_code: int | None,
        last_error: str | None,
    ) -> None:
        with self._lock:
            row = self._subs.get(subscription_id)
            if row is None:
                return
            row["last_delivery_at"] = _now()
            row["updated_at"] = row["last_delivery_at"]
            if delivered:
                row["failure_count"] = 0
                return
            row["failure_count"] = min(
                row["failure_count"] + 1, MAX_CONSECUTIVE_FAILURES
            )
            if row["failure_count"] >= MAX_CONSECUTIVE_FAILURES:
                row["is_active"] = False
                row["disabled_at"] = _now()

    # ---- deliveries ----

    async def create_delivery(
        self,
        *,
        subscription_id: str,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        target_url: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            key = (event_id, subscription_id)
            if key in self._delivs:
                # Idempotent re-dispatch — keep the existing row
                # but do not insert a duplicate.
                return None
            now = _now()
            delivery_id = f"whd-{len(self._delivs) + 1:08d}"
            row = {
                "id": delivery_id,
                "subscription_id": subscription_id,
                "event_id": event_id,
                "event_type": event_type,
                "payload": dict(payload or {}),
                "target_url": target_url,
                "attempt_count": 0,
                "last_attempt_at": None,
                "last_status_code": None,
                "last_error": None,
                "delivered_at": None,
                "created_at": now,
            }
            self._delivs[key] = row
            self._by_id[delivery_id] = row
            return deepcopy(row)

    async def update_delivery_attempt(
        self,
        delivery_id: str,
        *,
        attempt_count: int,
        last_status_code: int | None,
        last_error: str | None,
        delivered_at: datetime | None,
    ) -> None:
        with self._lock:
            row = self._by_id.get(delivery_id)
            if row is None:
                return
            row["attempt_count"] = attempt_count
            row["last_attempt_at"] = _now()
            row["last_status_code"] = last_status_code
            row["last_error"] = last_error
            if delivered_at is not None:
                row["delivered_at"] = delivered_at

    async def list_deliveries_for_subscription(
        self,
        subscription_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]] | None:
        with self._lock:
            sub = self._subs.get(subscription_id)
            if sub is None or sub["owner_id"] != owner_id:
                return None
            rows = [
                deepcopy(r) for r in self._delivs.values()
                if r["subscription_id"] == subscription_id
            ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    def __len__(self) -> int:
        return len(self._subs)


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresWebhookRepository(WebhookRepository):
    """Postgres-backed WebhookRepository (BE-API-008)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- subscriptions ----

    async def create_subscription(
        self,
        *,
        owner_id: str,
        target_url: str,
        secret: str,
        event_types: list[str],
    ) -> dict[str, Any]:
        sub = WebhookSubscription(
            id=str(uuid4()),
            owner_id=owner_id,
            target_url=target_url,
            secret=secret,
            event_types=list(event_types),
            is_active=True,
            failure_count=0,
        )
        self._session.add(sub)
        await self._session.flush()
        return self._sub_to_dict(sub)

    async def get_subscription(
        self, subscription_id: str
    ) -> dict[str, Any] | None:
        result = await self._session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()
        return self._sub_to_dict(sub) if sub else None

    async def list_subscriptions_for_owner(
        self, owner_id: str
    ) -> list[dict[str, Any]]:
        result = await self._session.execute(
            select(WebhookSubscription)
            .where(WebhookSubscription.owner_id == owner_id)
            .order_by(WebhookSubscription.created_at.desc())
        )
        return [self._sub_to_dict(s) for s in result.scalars().all()]

    async def list_active_subscriptions_for_event(
        self, event_type: str
    ) -> list[dict[str, Any]]:
        # The JSONB `event_types` column doesn't have a GIN index
        # in this iteration; for v1 we filter in Python. A future
        # ticket can add the index if this becomes a hotspot.
        result = await self._session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.is_active.is_(True)
            )
        )
        return [
            self._sub_to_dict(s)
            for s in result.scalars().all()
            if event_type in (s.event_types or [])
        ]

    async def update_subscription(
        self,
        subscription_id: str,
        *,
        owner_id: str,
        target_url: str | None = None,
        event_types: list[str] | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any] | None:
        sub = await self._owned(subscription_id, owner_id)
        if sub is None:
            return None
        if target_url is not None:
            sub.target_url = target_url
        if event_types is not None:
            sub.event_types = list(event_types)
        if is_active is not None:
            sub.is_active = is_active
            if is_active:
                sub.disabled_at = None
                sub.failure_count = 0
        sub.updated_at = _now()
        await self._session.flush()
        return self._sub_to_dict(sub)

    async def disable_subscription(
        self, subscription_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        sub = await self._owned(subscription_id, owner_id)
        if sub is None:
            return None
        sub.is_active = False
        sub.disabled_at = _now()
        sub.updated_at = sub.disabled_at
        await self._session.flush()
        return self._sub_to_dict(sub)

    async def record_delivery_outcome(
        self,
        subscription_id: str,
        *,
        attempt_count: int,
        delivered: bool,
        status_code: int | None,
        last_error: str | None,
    ) -> None:
        # We only know the subscription id here; the dispatcher
        # didn't pass owner_id because delivery is system work.
        # If the subscription was just auto-disabled, that's a
        # desirable side effect — a malicious owner can't abuse
        # this path because no PII flows through it.
        result = await self._session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()
        if sub is None:
            return
        now = _now()
        new_failure = 0 if delivered else min(
            (sub.failure_count or 0) + 1, MAX_CONSECUTIVE_FAILURES
        )
        new_active = sub.is_active
        new_disabled_at = sub.disabled_at
        if not delivered and new_failure >= MAX_CONSECUTIVE_FAILURES:
            new_active = False
            new_disabled_at = now
        await self._session.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == subscription_id)
            .values(
                last_delivery_at=now,
                updated_at=now,
                failure_count=new_failure,
                is_active=new_active,
                disabled_at=new_disabled_at,
            )
        )
        await self._session.flush()

    # ---- deliveries ----

    async def create_delivery(
        self,
        *,
        subscription_id: str,
        event_id: str,
        event_type: str,
        payload: dict[str, Any],
        target_url: str,
    ) -> dict[str, Any] | None:
        # Idempotency via `ON CONFLICT DO NOTHING` on the
        # (event_id, subscription_id) UNIQUE constraint. The
        # caller treats `None` as "already existed".
        stmt = pg_insert(WebhookDelivery).values(
            id=str(uuid4()),
            subscription_id=subscription_id,
            event_id=event_id,
            event_type=event_type,
            payload=dict(payload or {}),
            target_url=target_url,
            attempt_count=0,
        ).on_conflict_do_nothing(
            index_elements=[
                WebhookDelivery.event_id,
                WebhookDelivery.subscription_id,
            ],
        ).returning(WebhookDelivery)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.flush()
        return row.to_dict()

    async def update_delivery_attempt(
        self,
        delivery_id: str,
        *,
        attempt_count: int,
        last_status_code: int | None,
        last_error: str | None,
        delivered_at: datetime | None,
    ) -> None:
        await self._session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
            .values(
                attempt_count=attempt_count,
                last_attempt_at=_now(),
                last_status_code=last_status_code,
                last_error=last_error,
                delivered_at=delivered_at,
            )
        )
        await self._session.flush()

    async def list_deliveries_for_subscription(
        self,
        subscription_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]] | None:
        sub = await self._owned(subscription_id, owner_id)
        if sub is None:
            return None
        result = await self._session.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.subscription_id == subscription_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
        return [d.to_dict() for d in result.scalars().all()]

    # ---- helpers ----

    async def _owned(
        self, subscription_id: str, owner_id: str
    ) -> WebhookSubscription | None:
        """Fetch a subscription, asserting owner. Used by every
        write path so a cross-owner request collapses to None
        without an extra query."""
        result = await self._session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()
        if sub is None or sub.owner_id != owner_id:
            return None
        return sub

    @staticmethod
    def _sub_to_dict(s: WebhookSubscription) -> dict[str, Any]:
        return {
            "id": s.id,
            "owner_id": s.owner_id,
            "target_url": s.target_url,
            "secret": s.secret,
            "event_types": list(s.event_types or []),
            "is_active": s.is_active,
            "failure_count": s.failure_count,
            "last_delivery_at": s.last_delivery_at,
            "disabled_at": s.disabled_at,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_repo: WebhookRepository | None = None


def get_webhook_repository() -> WebhookRepository:
    global _repo
    if _repo is None:
        _repo = InMemoryWebhookRepository()
    return _repo


def set_webhook_repository(repo: WebhookRepository) -> None:
    global _repo
    _repo = repo
