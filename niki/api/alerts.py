"""Realtime alert streaming endpoint (BE-RT-002).

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
    - Each subsequent `alert` event carries the JSON of one
      `SecurityAlert` for the calling parent.
    - A `heartbeat` event is sent every 30s to keep idle
      proxies from closing the connection.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent, get_owned_child_id
from niki.realtime.alert_broker import get_alert_broker

import structlog

logger = structlog.get_logger(__name__)


router = APIRouter(prefix="/alerts", tags=["alerts"])


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


@router.get("/stream/{child_id}")
async def stream_alerts(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
) -> StreamingResponse:
    """SSE stream of high-risk alerts for the calling parent.

    Lateral-auth: only the parent who owns `child_id` can
    open this stream. Other parents' alerts are NEVER routed
    here (the broker filters by `alert.actor_parent_id`).
    """
    broker = get_alert_broker()
    queue = broker.subscribe(parent.id)

    async def event_stream():
        try:
            # 1) Send a `ready` event immediately so the
            # client knows the subscription is live.
            yield _format(
                "ready",
                {
                    "parent_id": parent.id,
                    "child_id": child_id,
                    "subscribed_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                },
            )
            # 2) Heartbeat + queue-drain loop.
            while True:
                try:
                    alert = await asyncio.wait_for(
                        queue.get(),
                        timeout=HEARTBEAT_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    yield _format(
                        "heartbeat",
                        {"ts": datetime.now(timezone.utc).isoformat()},
                    )
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