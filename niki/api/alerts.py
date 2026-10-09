"""Realtime alert streaming endpoint (BE-RT-002 / BE-RT-002-rollout).

SSE endpoint that lets a parent client subscribe to high-risk
security alerts about their child. Backed by
`niki.realtime.alert_broker`, which is published to by the
`AuthAuditLogger._maybe_alert()` hook (BE-AUTH-001-rollout-alerts).

The contract:
    - The client opens an EventSource on
      `GET /api/v1/alerts/stream/{child_id}` with their bearer
      token.
    - The server immediately sends a `ready` event so the
      client knows the subscription is live.
    - Then the server emits a `replay` event for every alert
      in the in-memory ring buffer that matches the query
      filters (BE-RT-002-rollout). The frontend renders these
      as "history" rather than new push notifications.
    - Each subsequent `alert` event carries the JSON of one
      NEW `SecurityAlert` for the calling parent.
    - A `heartbeat` event is sent every 30s to keep idle
      proxies from closing the connection.

Query params (all optional, all AND-ed together):
    - `alert_type`: exact-match on `SecurityAlert.alert_type`
    - `severity`:   exact-match on `SecurityAlert.severity`
                    (`warning` | `critical`)
    - `since`:      ISO-8601 timestamp; only alerts with
                    `detected_at >= since` are replayed
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent, get_owned_child_id
from niki.realtime.alert_broker import get_alert_broker

logger = structlog.get_logger(__name__)


router = APIRouter(
    prefix="/alerts",
    tags=[
        {
            "name": "alerts",
            "description": ("Realtime alert streaming endpoint (BE-RT-002 / BE-RT-002-rollout)."),
        },
    ],
)


# Heartbeat every 30s — keeps idle proxies / load balancers
# from closing the connection, and surfaces "client is alive"
# for ops dashboards.
HEARTBEAT_INTERVAL_SECONDS: float = 30.0


def _format(event: str, data: dict) -> str:
    """Build one SSE `event: data` block.

    SSE wire format:
        event: <name>\\n
        data: <json>\\n
        \\n
    """
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _parse_since(raw: str | None) -> datetime | None:
    """Parse the `since` query param into an aware UTC datetime.

    Accepts ISO-8601 strings (with or without timezone). A naive
    timestamp is interpreted as UTC. Returns None if `raw` is
    falsy.
    """
    if not raw:
        return None
    # `fromisoformat` accepts the format
    # `2026-07-30T12:34:56+00:00` and (in 3.11+) the trailing `Z`.
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        # Try replacing a trailing 'Z' (Python <3.11 strictness).
        if raw.endswith("Z"):
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            raise
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@router.get(
    "/stream/{child_id}",
    summary="SSE stream of high-risk alerts for the calling parent",
    description="""
            SSE stream of high-risk alerts for the calling parent.
            
            Lateral-auth: only the parent who owns `child_id` can open
            this stream. Other parents' alerts are NEVER routed here
            (the broker filters by `alert.actor_parent_id`).
            
            On connect the endpoint replays alerts from the in-memory
            ring buffer that match the query filters (BE-RT-002-rollout)
            as `event: replay`, then continues with new alerts as
            `event: alert`.
            """,
)
async def stream_alerts(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
    alert_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    since: str | None = Query(default=None),
) -> StreamingResponse:
    """SSE stream of high-risk alerts for the calling parent.

    Lateral-auth: only the parent who owns `child_id` can open
    this stream. Other parents' alerts are NEVER routed here
    (the broker filters by `alert.actor_parent_id`).

    On connect the endpoint replays alerts from the in-memory
    ring buffer that match the query filters (BE-RT-002-rollout)
    as `event: replay`, then continues with new alerts as
    `event: alert`.
    """
    broker = get_alert_broker()
    since_dt = _parse_since(since)
    queue = broker.subscribe(parent.id)

    async def event_stream():
        try:
            # 1) Send a `ready` event immediately so the
            # client knows the subscription is live. The ready
            # payload also tells the client which filters the
            # server applied to the replay so it can warn if its
            # own filter differs.
            yield _format(
                "ready",
                {
                    "parent_id": parent.id,
                    "child_id": child_id,
                    "subscribed_at": datetime.now(UTC).isoformat(),
                    "filters": {
                        "alert_type": alert_type,
                        "severity": severity,
                        "since": since_dt.isoformat() if since_dt is not None else None,
                    },
                },
            )
            # 2) BE-RT-002-rollout: emit buffered alerts as
            # `replay` events BEFORE entering the live loop so
            # the frontend can render them as history (not as
            # new push notifications).
            for buffered in broker.recent_alerts(
                parent.id,
                alert_type=alert_type,
                severity=severity,
                since=since_dt,
            ):
                yield _format("replay", buffered.to_dict())
            # 3) Heartbeat + queue-drain loop (live new alerts).
            while True:
                try:
                    alert = await asyncio.wait_for(
                        queue.get(),
                        timeout=HEARTBEAT_INTERVAL_SECONDS,
                    )
                except TimeoutError:
                    yield _format(
                        "heartbeat",
                        {"ts": datetime.now(UTC).isoformat()},
                    )
                    continue
                # Live alerts — apply the same filter the
                # client subscribed with, so the frontend
                # doesn't have to re-filter on the client.
                if alert_type is not None and alert.alert_type != alert_type:
                    continue
                if severity is not None and alert.severity != severity:
                    continue
                if since_dt is not None and alert.detected_at < since_dt:
                    continue
                yield _format("alert", alert.to_dict())
        except asyncio.CancelledError:
            # Client disconnected.
            logger.info(
                "alerts.stream.client_disconnected",
                parent_id=parent.id,
            )
            raise
        finally:
            broker.unsubscribe(parent.id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
