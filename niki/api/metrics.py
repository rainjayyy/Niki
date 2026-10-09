"""Prometheus exposition endpoint (BE-API-009).

Exposes ``GET /api/v1/metrics`` for Prometheus scrapers. The response
body is the standard text/plain v0.0.4 exposition format produced by
``prometheus_client.generate_latest(registry)``.

Auth: NONE. Prometheus / Grafana Alloy / OTel collectors scrape this
endpoint anonymously from inside the cluster. If we ever need to
gate it behind auth, the convention is to put a separate
``/internal/metrics`` path behind a network policy, NOT to add
bearer-token auth here (scrapers can't carry dynamic tokens).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    generate_latest,
)

from niki.metrics import get_registry

router = APIRouter(
    tags=[
        {
            "name": "metrics",
            "description": (
                "Prometheus exposition endpoint (BE-API-009). "
                "Returns the standard text/plain v0.0.4 format used "
                "by Prometheus / Grafana Alloy / OTel collectors."
            ),
        },
    ],
)


@router.get(
    "/metrics",
    summary="Prometheus metrics exposition",
    description="""
            Returns the standard Prometheus text exposition format
            (Content-Type: ``text/plain; version=0.0.4``).

            Metrics registered:

            - ``niki_chat_total{route,provider,mode}`` — chat requests served
            - ``niki_safety_block_total{reason,severity}`` — safety blocks / escalations
            - ``niki_high_risk_total{alert_type}`` — high-risk alert events
            - ``niki_sms_sent_total{provider,status}`` — SMS dispatch outcomes
            - ``niki_login_success_total{method}`` — successful parent logins
            - ``niki_login_failure_total{method,reason}`` — failed parent logins
            - ``niki_push_delivery_total{channel,status}`` — push notification outcomes
            - ``niki_ocr_requests_total{status}`` — OCR request outcomes
            - ``niki_rate_limited_total{endpoint}`` — 429 responses emitted
            - ``niki_webhook_deliveries_total{event_type,status}`` — webhook deliveries
            - ``niki_auth_audit_total{event_type,severity}`` — auth audit events
            - ``niki_http_request_duration_seconds{method,endpoint}`` — request latency histogram
            - ``niki_active_parents`` — active parent gauge (v1: 0)

            Auth: NONE (scrapers hit this from inside the cluster).
            """,
    response_class=Response,
)
async def metrics_exposition() -> Response:
    """Return the current registry in Prometheus text exposition format."""
    registry = get_registry().registry
    body = generate_latest(registry)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
