"""Tests for BE-RT-002-rollout: alert filter + replay buffer.

Covers:
    - `AlertBroker.recent_alerts` filters by alert_type / severity / since
    - SSE endpoint emits `event: replay` for buffered alerts on connect
    - SSE endpoint continues to stream new alerts after replay
    - Ring buffer caps at `buffer_cap` (oldest dropped)
    - Live new alerts also respect query-param filters
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from niki.realtime.alert_broker import (
    REPLAY_BUFFER_CAP,
    AlertBroker,
    set_alert_broker,
)
from niki.security.alerts import SecurityAlert


def _sample(
    *,
    actor: str = "p-actor",
    alert_type: str = "lateral_auth_burst",
    severity: str = "critical",
    detected_at: datetime | None = None,
) -> SecurityAlert:
    """Build a sample alert.

    `actor` defaults to `"p-actor"` (suitable for unit tests that
    query the buffer directly). SSE integration tests override
    to `"test_parent"` so the alert lands in the buffer that the
    SSE endpoint (subscribed under the fake parent id
    `"test_parent"`) will read from.
    """
    if detected_at is None:
        detected_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    return SecurityAlert(
        alert_type=alert_type,
        severity=severity,
        actor_parent_id=actor,
        target_parent_id="p-victim",
        target_child_id="c-1",
        client_ip="10.0.0.1",
        event_count=3,
        window_seconds=300,
        detected_at=detected_at,
        title=f"{alert_type} sample",
        detail=f"detail for {alert_type}",
    )


_PARENT = "test_parent"  # the SSE endpoint's subscribed parent id


def _parent_alert(**kwargs):
    """Convenience: an alert addressed to the SSE endpoint's
    parent (so it lands in the buffer the endpoint replays)."""
    kwargs.setdefault("actor", _PARENT)
    return _sample(**kwargs)


# ---------------------------------------------------------------------------
# AlertBroker.recent_alerts (unit)
# ---------------------------------------------------------------------------


class TestRecentAlertsFilters:
    def test_filters_by_alert_type(self) -> None:
        broker = AlertBroker()
        broker.publish(_sample(alert_type="lateral_auth_burst"))
        broker.publish(_sample(alert_type="multi_actor_same_ip"))
        broker.publish(_sample(alert_type="lateral_auth_burst"))
        out = broker.recent_alerts("p-actor", alert_type="lateral_auth_burst")
        assert [a.alert_type for a in out] == [
            "lateral_auth_burst",
            "lateral_auth_burst",
        ]

    def test_filters_by_severity(self) -> None:
        broker = AlertBroker()
        broker.publish(_sample(severity="warning"))
        broker.publish(_sample(severity="critical"))
        broker.publish(_sample(severity="warning"))
        out = broker.recent_alerts("p-actor", severity="critical")
        assert [a.severity for a in out] == ["critical"]

    def test_filters_by_since(self) -> None:
        broker = AlertBroker()
        base = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        broker.publish(_sample(detected_at=base - timedelta(minutes=10)))
        broker.publish(_sample(detected_at=base))
        broker.publish(_sample(detected_at=base + timedelta(minutes=10)))
        out = broker.recent_alerts("p-actor", since=base)
        # Two alerts at or after `base`.
        assert len(out) == 2
        assert all(a.detected_at >= base for a in out)
        # Chronological order preserved.
        assert [a.detected_at for a in out] == sorted([a.detected_at for a in out])

    def test_filters_combine_with_and(self) -> None:
        broker = AlertBroker()
        base = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        broker.publish(
            _sample(
                alert_type="lateral_auth_burst",
                severity="warning",
                detected_at=base,
            )
        )
        broker.publish(
            _sample(
                alert_type="lateral_auth_burst",
                severity="critical",
                detected_at=base,
            )
        )
        broker.publish(
            _sample(
                alert_type="multi_actor_same_ip",
                severity="critical",
                detected_at=base,
            )
        )
        out = broker.recent_alerts(
            "p-actor",
            alert_type="lateral_auth_burst",
            severity="critical",
            since=base,
        )
        assert len(out) == 1
        assert out[0].alert_type == "lateral_auth_burst"
        assert out[0].severity == "critical"

    def test_limit_caps_from_most_recent(self) -> None:
        broker = AlertBroker()
        base = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        for i in range(10):
            broker.publish(_sample(detected_at=base + timedelta(seconds=i)))
        out = broker.recent_alerts("p-actor", limit=3)
        assert len(out) == 3
        # Most-recent 3 (oldest-first preserved).
        assert [a.detected_at for a in out] == [
            base + timedelta(seconds=7),
            base + timedelta(seconds=8),
            base + timedelta(seconds=9),
        ]

    def test_returns_empty_for_unknown_parent(self) -> None:
        broker = AlertBroker()
        out = broker.recent_alerts("p-nobody")
        assert out == []

    def test_limit_zero_returns_empty(self) -> None:
        broker = AlertBroker()
        broker.publish(_sample())
        out = broker.recent_alerts("p-actor", limit=0)
        assert out == []


class TestBufferCap:
    def test_buffer_caps_at_buffer_cap(self) -> None:
        """When the buffer is full, the oldest alerts are dropped."""
        cap = 10  # use a small cap for the test
        broker = AlertBroker(buffer_cap=cap)
        for i in range(cap + 5):
            broker.publish(_sample(detected_at=datetime(2026, 7, 30, 12, 0, i, tzinfo=UTC)))
        # Buffer holds exactly `cap` entries.
        assert broker.buffer_size("p-actor") == cap
        out = broker.recent_alerts("p-actor")
        assert len(out) == cap
        # Oldest 5 (i=0..4) were dropped; we kept i=5..14.
        first = out[0].detected_at
        last = out[-1].detected_at
        assert first.second == 5
        assert last.second == 14

    def test_default_buffer_cap_is_replay_buffer_cap(self) -> None:
        broker = AlertBroker()
        for _i in range(REPLAY_BUFFER_CAP + 3):
            broker.publish(_sample())
        # The default cap keeps the buffer bounded.
        assert broker.buffer_size("p-actor") == REPLAY_BUFFER_CAP


# ---------------------------------------------------------------------------
# SSE endpoint integration
# ---------------------------------------------------------------------------


def _parse_sse_chunks(body: str) -> list[tuple[str, dict]]:
    """Parse a complete SSE body string into `(event, data)` pairs."""
    events: list[tuple[str, dict]] = []
    cur_event: str | None = None
    cur_data: list[str] = []
    for line in body.split("\n"):
        if line.startswith(":"):
            cur_event = None
            cur_data = []
            continue
        if line.startswith("event: "):
            cur_event = line[len("event: ") :]
        elif line.startswith("data: "):
            cur_data.append(line[len("data: ") :])
        elif line == "":
            if cur_event is not None and cur_data:
                payload = "\n".join(cur_data)
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    obj = {"_raw": payload}
                events.append((cur_event, obj))
            cur_event = None
            cur_data = []
    return events


def _run_endpoint_in_thread(
    app: Any,
    path: str,
    *,
    timeout_seconds: float,
    on_chunk: Any = None,
) -> str:
    """Drive the SSE endpoint synchronously by feeding it an
    ASGI `receive`/`send` pair.

    Returns the concatenated raw body (event:/data:/blank lines)
    that the endpoint produced.

    With `spec_version < 2.4` Starlette uses
    `listen_for_disconnect` which awaits `receive()` in parallel
    with the streaming task. If `receive()` returns
    `http.disconnect`, the streaming task is cancelled. So we
    must keep returning `http.request` until we're ready to
    close — then return `http.disconnect` once the test timer
    expires.
    """
    from anyio.from_thread import start_blocking_portal

    captured: list[str] = []
    error: list[Exception] = []
    state = {
        "first": True,
        "done": threading.Event(),
    }

    async def receive() -> dict:
        if state["first"]:
            state["first"] = False
            return {"type": "http.request", "body": b"", "more_body": False}
        # Wait until the disconnect timer fires, then return
        # http.disconnect so Starlette cancels the streaming.
        await asyncio.to_thread(state["done"].wait, timeout_seconds)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            captured.append(f"STATUS {message['status']}")
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                captured.append(text)
                if on_chunk is not None:
                    on_chunk(text)

    scope: dict = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "method": "GET",
        "scheme": "http",
        "path": path.split("?")[0],
        "raw_path": path.split("?")[0].encode("latin-1"),
        "query_string": path.split("?", 1)[1].encode("latin-1") if "?" in path else b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 5000),
        "server": ("testserver", 80),
        "root_path": "",
        "http_version": "1.1",
    }

    def runner() -> None:
        try:
            with start_blocking_portal() as portal:
                portal.call(app, scope, receive, send)
        except Exception as exc:  # pragma: no cover
            error.append(exc)
        finally:
            state["done"].set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    def _kick_disconnect() -> None:
        time.sleep(timeout_seconds)
        state["done"].set()

    kicker = threading.Thread(target=_kick_disconnect, daemon=True)
    kicker.start()
    t.join(timeout=timeout_seconds + 5)
    if error:
        raise error[0]
    return "\n".join(captured)


@pytest.fixture
def fresh_broker():
    """Install a fresh AlertBroker for each SSE integration test
    so buffered alerts from prior tests don't leak in."""
    broker = AlertBroker(buffer_cap=REPLAY_BUFFER_CAP)
    set_alert_broker(broker)
    yield broker
    set_alert_broker(None)


@pytest.fixture
def sse_app():
    """Build the app with the env flag set so the fake-parent
    override is installed."""
    import os

    os.environ["NIKI_TEST_FAKE_AUTH"] = "1"
    from niki.main import create_app

    return create_app()


class TestSseReplay:
    def test_replay_events_for_buffered_alerts(
        self, fresh_broker: AlertBroker, sse_app: Any
    ) -> None:
        """Buffered alerts are emitted as `event: replay`, NOT
        as `event: alert`, when a client connects."""
        fresh_broker.publish(_parent_alert(alert_type="lateral_auth_burst"))
        fresh_broker.publish(_parent_alert(alert_type="lateral_auth_burst"))
        body = _run_endpoint_in_thread(
            sse_app,
            "/api/v1/alerts/stream/test-child-1",
            timeout_seconds=0.6,
        )
        events = _parse_sse_chunks(body)
        kinds = [k for k, _ in events]
        assert "ready" in kinds
        assert "replay" in kinds
        # No `alert` event yet — these are buffered history.
        assert "alert" not in kinds
        replay_payloads = [d for k, d in events if k == "replay"]
        assert len(replay_payloads) == 2
        for p in replay_payloads:
            assert p["alert_type"] == "lateral_auth_burst"
            assert p["actor_parent_id"] == _PARENT

    def test_no_replay_when_buffer_empty(self, fresh_broker: AlertBroker, sse_app: Any) -> None:
        body = _run_endpoint_in_thread(
            sse_app,
            "/api/v1/alerts/stream/test-child-1",
            timeout_seconds=0.6,
        )
        events = _parse_sse_chunks(body)
        kinds = [k for k, _ in events]
        assert "ready" in kinds
        assert "replay" not in kinds
        assert "alert" not in kinds

    def test_query_param_filters_replay(self, fresh_broker: AlertBroker, sse_app: Any) -> None:
        """?severity=critical filters the replay buffer to only
        matching alerts."""
        fresh_broker.publish(_parent_alert(severity="critical"))
        fresh_broker.publish(_parent_alert(severity="warning"))
        fresh_broker.publish(_parent_alert(severity="critical"))
        body = _run_endpoint_in_thread(
            sse_app,
            "/api/v1/alerts/stream/test-child-1?severity=critical",
            timeout_seconds=0.6,
        )
        events = _parse_sse_chunks(body)
        replay_payloads = [d for k, d in events if k == "replay"]
        assert len(replay_payloads) == 2
        assert all(p["severity"] == "critical" for p in replay_payloads)

    def test_query_param_filters_live_alert(self, fresh_broker: AlertBroker, sse_app: Any) -> None:
        """A live alert that does NOT match the subscribed
        filter is NOT pushed to the client."""
        from anyio.from_thread import start_blocking_portal

        captured: list[str] = []
        state = {"first": True, "done": threading.Event()}

        async def receive() -> dict:
            if state["first"]:
                state["first"] = False
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            await asyncio.to_thread(state["done"].wait, 2.0)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    captured.append(chunk.decode("utf-8"))

        scope: dict = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/alerts/stream/test-child-1",
            "raw_path": b"/api/v1/alerts/stream/test-child-1",
            "query_string": b"severity=critical",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 5000),
            "server": ("testserver", 80),
            "root_path": "",
            "http_version": "1.1",
        }

        def runner() -> None:
            with start_blocking_portal() as portal:
                portal.call(sse_app, scope, receive, send)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        # Give the consumer time to subscribe.
        time.sleep(0.2)
        # Publish a WARNING — should be filtered out by the
        # server (severity != critical).
        fresh_broker.publish(_parent_alert(severity="warning"))
        time.sleep(0.2)
        # Publish a CRITICAL — should reach the client.
        fresh_broker.publish(_parent_alert(severity="critical"))
        # Wait for the alert to flow through.
        time.sleep(0.3)
        state["done"].set()
        t.join(timeout=3)
        body = "\n".join(captured)
        events = _parse_sse_chunks(body)
        alerts = [d for k, d in events if k == "alert"]
        # Only the critical one made it.
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"

    def test_ready_event_includes_filters(self, fresh_broker: AlertBroker, sse_app: Any) -> None:
        body = _run_endpoint_in_thread(
            sse_app,
            "/api/v1/alerts/stream/test-child-1"
            "?alert_type=lateral_auth_burst"
            "&severity=critical"
            "&since=2026-07-30T00:00:00Z",
            timeout_seconds=0.6,
        )
        events = _parse_sse_chunks(body)
        ready = next(d for k, d in events if k == "ready")
        assert ready["filters"]["alert_type"] == "lateral_auth_burst"
        assert ready["filters"]["severity"] == "critical"
        assert ready["filters"]["since"] == "2026-07-30T00:00:00+00:00"
