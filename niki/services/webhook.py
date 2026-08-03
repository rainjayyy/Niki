"""WebhookDispatcher — the single seam between Niki and the outside world (BE-API-008).

Two halves:

  WebhookDispatcherService
    Lifecycle of subscriptions (`register` / `list` / `get` /
    `update` / `disable`) and the `dispatch(...)` loop that
    POSTs events to all matching subscribers with HMAC-SHA256
    signing + 1s/4s/16s retry + auto-disable on 5 consecutive
    failures.

  `sign_payload(secret, payload_bytes)`
    Pure helper, exposed so tests can assert the exact format of
    the `X-Niki-Signature` header without reaching into the
    dispatcher.

Why a singleton (and not just a stateless function)
---------------------------------------------------
The dispatcher owns two pieces of mutable state:

  1. The HTTP client (`httpx.AsyncClient`) — kept open across
     calls so each `dispatch` reuses the same connection pool.
  2. The process-wide `WebhookRepository` instance — same
     rationale as the other `*Service` singletons.

The repo + service are split so tests can swap the repo (e.g.
to assert "exactly N rows were written") without touching the
HTTP layer. The HTTP layer itself is monkey-patchable via
`httpx.MockTransport` — see the tests for the pattern.

Failure semantics
-----------------
A "failure" is anything that isn't a 2xx. The dispatcher does
NOT classify 4xx as a permanent failure today — that's a
followup: a target that returns 400 will burn through 5
dispatches and get auto-disabled, which is too aggressive for
"your endpoint requires a different auth scheme". For v1 we
treat every non-2xx the same and rely on the 5-strike budget
to absorb transient errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from niki.db.repositories.webhook_repo import (
    WebhookRepository,
    get_webhook_repository,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "WebhookDispatcherService",
    "get_webhook_dispatcher",
    "set_webhook_dispatcher",
    "sign_payload",
    "DEFAULT_TIMEOUT_SECONDS",
    "RETRY_DELAYS_SECONDS",
    "MAX_ATTEMPTS",
    "USER_AGENT",
    "SIGNATURE_HEADER",
    "EVENT_HEADER",
    "EVENT_ID_HEADER",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Header names. Exposed as module constants so the tests can
# reference the same string without re-declaring it.
SIGNATURE_HEADER: str = "X-Niki-Signature"
EVENT_HEADER: str = "X-Niki-Event"
EVENT_ID_HEADER: str = "X-Niki-Event-Id"
USER_AGENT: str = "Niki-Webhooks/1.0"

# Per-attempt HTTP timeout. 10s is generous — a healthy
# integration target should ack in <2s.
DEFAULT_TIMEOUT_SECONDS: float = 10.0

# Exponential backoff between retries. 1s, 4s, 16s.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (1, 4, 16)

# Total HTTP attempts = 1 (initial) + len(RETRY_DELAYS_SECONDS) = 4.
# The ticket spec says "up to 3 attempts" — interpreted as
# "up to 3 RETRIES" (so 4 attempts total). If the spec wanted
# 3 attempts total we'd set this to (1, 4). 3 retries keeps
# the cumulative window under 25s and matches the "5 consecutive
# failures" disable budget: 5 dispatches × 4 attempts × ~1s
# each ≈ 20s ceiling per failure to triage.
MAX_ATTEMPTS: int = 1 + len(RETRY_DELAYS_SECONDS)


def sign_payload(secret: str, payload_bytes: bytes) -> str:
    """Compute `X-Niki-Signature` for a request body.

    Format: `"sha256=<hex>"` where `hex` is the lowercase
    hexdigest of HMAC-SHA256(secret, payload_bytes). Receivers
    verify by:

        expected = "sha256=" + hmac.new(
            secret.encode(), request.body, hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(expected, request.headers["X-Niki-Signature"])

    The `payload_bytes` MUST be the raw request body, not a
    re-serialised dict — JSON key order or whitespace
    differences would otherwise break verification.
    """
    digest = hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WebhookDispatcherService:
    """Owns subscription lifecycle + the dispatch loop."""

    def __init__(
        self,
        repo: WebhookRepository | None = None,
        *,
        sleep: Any | None = None,
    ) -> None:
        """
        Args:
            repo: Persistence backend. Defaults to the
                process-wide singleton.
            sleep: Override the sleep used between retries.
                Tests pass a no-op (or a captured-call list) so
                the test suite doesn't actually wait 21s.
        """
        self._repo = repo
        # `asyncio.sleep` is bound at call time so tests can
        # patch the module-level reference. Default: real sleep.
        self._sleep = sleep if sleep is not None else asyncio.sleep

    @property
    def repo(self) -> WebhookRepository:
        if self._repo is None:
            self._repo = get_webhook_repository()
        return self._repo

    # ---- subscription lifecycle ---------------------------------------

    async def register(
        self,
        *,
        owner_id: str,
        target_url: str,
        event_types: list[str],
    ) -> dict[str, Any]:
        """Create a new subscription. Generates a fresh secret.

        Returns the full row dict (including the secret). The
        API layer is responsible for stripping `secret` from
        any non-`register` response.
        """
        secret = secrets.token_urlsafe(32)
        return await self.repo.create_subscription(
            owner_id=owner_id,
            target_url=target_url,
            secret=secret,
            event_types=list(event_types),
        )

    async def list_for_owner(
        self, owner_id: str
    ) -> list[dict[str, Any]]:
        return await self.repo.list_subscriptions_for_owner(owner_id)

    async def get(
        self, subscription_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        row = await self.repo.get_subscription(subscription_id)
        if row is None or row["owner_id"] != owner_id:
            # Collapse "not found" + "not yours" to None so the
            # API layer renders 404 (avoid id-existence oracle).
            return None
        return row

    async def update(
        self,
        subscription_id: str,
        owner_id: str,
        *,
        target_url: str | None = None,
        event_types: list[str] | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any] | None:
        return await self.repo.update_subscription(
            subscription_id,
            owner_id=owner_id,
            target_url=target_url,
            event_types=event_types,
            is_active=is_active,
        )

    async def disable(
        self, subscription_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        return await self.repo.disable_subscription(
            subscription_id, owner_id
        )

    async def get_deliveries(
        self,
        subscription_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]] | None:
        return await self.repo.list_deliveries_for_subscription(
            subscription_id, owner_id, limit=limit
        )

    # ---- dispatch -------------------------------------------------------

    async def dispatch(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        http_client: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Deliver `event_type` to every active, matching subscription.

        Returns one entry per (subscription, delivery-row) pair.
        Used by tests to assert "did this fan out?".

        Args:
            event_type: e.g. "account.deleted".
            payload: JSON-serialisable dict.
            event_id: Idempotency key. Defaults to a fresh uuid4.
            http_client: Optional `httpx.AsyncClient` to use for
                the outbound requests. Tests pass a
                `httpx.MockTransport`-backed client; production
                uses the default (no client passed → a fresh
                one per call).

        The function NEVER raises on a target failure — a
        broken endpoint is the target owner's problem, not
        the dispatcher's. We do log it.
        """
        if event_id is None:
            event_id = f"evt-{secrets.token_urlsafe(16)}"

        subs = await self.repo.list_active_subscriptions_for_event(
            event_type
        )
        if not subs:
            return []

        body_bytes = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        results: list[dict[str, Any]] = []
        for sub in subs:
            results.append(
                await self._dispatch_one(
                    sub, event_type, event_id, body_bytes, http_client
                )
            )
        return results

    async def _dispatch_one(
        self,
        sub: dict[str, Any],
        event_type: str,
        event_id: str,
        body_bytes: bytes,
        http_client: Any | None,
    ) -> dict[str, Any]:
        """Dispatch to a single subscription with retries.

        Idempotency: a second call with the same `event_id`
        is a no-op — `create_delivery` returns None and we
        return the existing row.
        """
        delivery = await self.repo.create_delivery(
            subscription_id=sub["id"],
            event_id=event_id,
            event_type=event_type,
            payload=self._decode_body(body_bytes),
            target_url=sub["target_url"],
        )
        if delivery is None:
            # Already-delivered (idempotency hit). Return a
            # sentinel so the caller can tell the difference.
            existing = await self.repo.list_deliveries_for_subscription(
                sub["id"], sub["owner_id"], limit=1000
            )
            existing_match = next(
                (d for d in (existing or [])
                 if d.get("event_id") == event_id),
                None,
            )
            return {
                "subscription_id": sub["id"],
                "event_id": event_id,
                "delivery_id": existing_match["id"] if existing_match else None,
                "delivered": bool(existing_match and existing_match.get("delivered_at")),
                "idempotent_replay": True,
            }

        # Run the retry loop.
        attempt_count = 0
        last_status: int | None = None
        last_error: str | None = None
        delivered_at: datetime | None = None
        for attempt_index in range(MAX_ATTEMPTS):
            attempt_count = attempt_index + 1
            try:
                status, error = await self._post_once(
                    sub, event_type, event_id, body_bytes, http_client
                )
            except Exception as exc:  # network / DNS / etc.
                status, error = None, str(exc)[:1024]
            last_status = status
            last_error = error
            if status is not None and 200 <= status < 300:
                delivered_at = _now()
                break
            # Backoff before next attempt (skip after the last one).
            if attempt_index < MAX_ATTEMPTS - 1:
                await self._sleep(RETRY_DELAYS_SECONDS[attempt_index])

        # Persist the final state.
        await self.repo.update_delivery_attempt(
            delivery["id"],
            attempt_count=attempt_count,
            last_status_code=last_status,
            last_error=last_error,
            delivered_at=delivered_at,
        )
        delivered = delivered_at is not None
        await self.repo.record_delivery_outcome(
            sub["id"],
            attempt_count=attempt_count,
            delivered=delivered,
            status_code=last_status,
            last_error=last_error,
        )
        return {
            "subscription_id": sub["id"],
            "event_id": event_id,
            "delivery_id": delivery["id"],
            "delivered": delivered,
            "idempotent_replay": False,
            "attempts": attempt_count,
            "last_status_code": last_status,
            "last_error": last_error,
        }

    async def _post_once(
        self,
        sub: dict[str, Any],
        event_type: str,
        event_id: str,
        body_bytes: bytes,
        http_client: Any | None,
    ) -> tuple[Optional[int], Optional[str]]:
        """Make a single HTTP attempt.

        Returns (status_code, error). A network exception
        already raises out of the caller (`_dispatch_one`),
        so this returns `(None, "...")` for things like
        `httpx.HTTPStatusError` if the client raises on
        non-2xx.
        """
        import httpx  # local import keeps the module importable
        # even when httpx isn't installed (e.g. minimal CI).

        signature = sign_payload(sub["secret"], body_bytes)
        headers = {
            SIGNATURE_HEADER: signature,
            EVENT_HEADER: event_type,
            EVENT_ID_HEADER: event_id,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_SECONDS
        )
        try:
            response = await client.post(
                sub["target_url"],
                content=body_bytes,
                headers=headers,
            )
            return response.status_code, None
        except httpx.HTTPStatusError as exc:  # pragma: no cover
            # We don't `raise_for_status` so this is unreachable
            # under normal flow; kept as belt-and-braces.
            return exc.response.status_code, str(exc)[:1024]
        except Exception as exc:
            return None, str(exc)[:1024]
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _decode_body(body_bytes: bytes) -> dict[str, Any]:
        """Decode the JSON body for storage in `webhook_deliveries.payload`.

        Falls back to a `{"_raw": "..."}` shape if the body is
        not valid JSON (shouldn't happen — the dispatcher is
        the only writer — but a defence-in-depth net).
        """
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"_raw": body_bytes.decode("utf-8", errors="replace")}


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_dispatcher: WebhookDispatcherService | None = None


def get_webhook_dispatcher() -> WebhookDispatcherService:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = WebhookDispatcherService()
    return _dispatcher


def set_webhook_dispatcher(
    dispatcher: WebhookDispatcherService,
) -> None:
    global _dispatcher
    _dispatcher = dispatcher
