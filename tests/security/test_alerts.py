"""Tests for SecurityAlert + AlertDispatcher sinks
(BE-AUTH-001-rollout-alerts)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from niki.security.alerts import (
    CompositeAlertDispatcher,
    ConsoleAlertDispatcher,
    SecurityAlert,
    WebhookAlertDispatcher,
)


def _sample_alert(**overrides) -> SecurityAlert:
    base = dict(
        alert_type="lateral_auth_burst",
        severity="critical",
        actor_parent_id="p1",
        target_parent_id="p2",
        target_child_id="c1",
        client_ip="10.0.0.1",
        event_count=5,
        window_seconds=300,
        detected_at=datetime.now(timezone.utc),
        title="Lateral-auth burst: p1 x5",
        detail="Actor 'p1' accumulated 5 violations within 300s.",
    )
    base.update(overrides)
    return SecurityAlert(**base)


class TestSecurityAlert:
    def test_invalid_severity_raises(self) -> None:
        with pytest.raises(ValueError):
            SecurityAlert(
                alert_type="x",
                severity="bogus",
                actor_parent_id=None,
                target_parent_id=None,
                target_child_id=None,
                client_ip=None,
                event_count=0,
                window_seconds=0,
                detected_at=datetime.now(timezone.utc),
                title="",
                detail="",
            )

    def test_to_dict_serialises_datetime(self) -> None:
        a = _sample_alert()
        d = a.to_dict()
        assert isinstance(d["detected_at"], str)
        assert "T" in d["detected_at"]


class TestConsoleAlertDispatcher:
    async def test_dispatch_does_not_raise(self) -> None:
        d = ConsoleAlertDispatcher()
        # Just exercise the path — structlog is configured globally
        # so the call returns cleanly.
        await d.dispatch(_sample_alert())


class TestWebhookAlertDispatcher:
    async def test_post_to_configured_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = AsyncMock()
        mock_response = AsyncMock(status_code=200)
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response
        mock_client.aclose = AsyncMock()

        # Override httpx.AsyncClient construction.
        import niki.security.alerts as alerts_mod
        monkeypatch.setattr(
            alerts_mod.httpx, "AsyncClient",
            lambda *a, **kw: mock_client,
        )

        d = WebhookAlertDispatcher(
            url="https://example.com/hook", timeout_seconds=1.0
        )
        await d.dispatch(_sample_alert())

        mock_client.post.assert_awaited_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == "https://example.com/hook"
        assert "json" in kwargs
        assert kwargs["json"]["alert_type"] == "lateral_auth_burst"

    async def test_empty_url_raises(self) -> None:
        with pytest.raises(ValueError):
            WebhookAlertDispatcher(url="")

    async def test_post_failure_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A webhook failure must never propagate to the caller."""

        class BoomClient:
            def __init__(self, *a, **kw) -> None: pass
            async def post(self, *a, **kw):
                raise httpx.ConnectError("boom")
            async def aclose(self):
                pass

        import niki.security.alerts as alerts_mod
        monkeypatch.setattr(
            alerts_mod.httpx, "AsyncClient", BoomClient
        )

        d = WebhookAlertDispatcher(url="https://example.com/hook")
        # Must NOT raise.
        await d.dispatch(_sample_alert())

    async def test_non_2xx_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_response = AsyncMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.aclose = AsyncMock()

        import niki.security.alerts as alerts_mod
        monkeypatch.setattr(
            alerts_mod.httpx, "AsyncClient",
            lambda *a, **kw: mock_client,
        )

        d = WebhookAlertDispatcher(url="https://example.com/hook")
        await d.dispatch(_sample_alert())
        # We expect the alert to be logged as error but not raised.


class TestCompositeAlertDispatcher:
    async def test_fan_out_calls_all_sinks(self) -> None:
        a = AsyncMock()
        b = AsyncMock()
        c = CompositeAlertDispatcher([a, b])
        alert = _sample_alert()
        await c.dispatch(alert)
        a.dispatch.assert_awaited_once_with(alert)
        b.dispatch.assert_awaited_once_with(alert)

    async def test_one_sink_failure_does_not_block_others(self) -> None:
        """A broken sink must NOT stop the others from receiving."""
        a = AsyncMock(side_effect=RuntimeError("broken"))
        b = AsyncMock()
        c = CompositeAlertDispatcher([a, b])
        await c.dispatch(_sample_alert())
        b.dispatch.assert_awaited_once()

    async def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError):
            CompositeAlertDispatcher([])