"""Tests for the EmailAlertDispatcher + parallel composite (BE-AUTH-001-rollout-alerts-rollout)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from niki.security.alerts import (
    AlertDispatcher,
    CompositeAlertDispatcher,
    EmailAlertDispatcher,
    SecurityAlert,
)


def _sample_alert() -> SecurityAlert:
    return SecurityAlert(
        alert_type="parent_child_mismatch",
        severity="critical",
        actor_parent_id="p-actor",
        target_parent_id="p-victim",
        target_child_id="c-1",
        client_ip="10.0.0.5",
        event_count=3,
        window_seconds=300,
        detected_at=datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc),
        title="Lateral-auth burst: p-actor x3",
        detail="Actor 'p-actor' accumulated 3 lateral-auth violations within 300s.",
    )


class TestEmailAlertDispatcher:
    async def test_skips_when_smtp_not_configured(self) -> None:
        """Empty host/port → log warning, never raise."""
        sink = EmailAlertDispatcher()
        # Should not raise.
        await sink.dispatch(_sample_alert())

    async def test_skips_when_no_recipients(self) -> None:
        sink = EmailAlertDispatcher(
            smtp_host="smtp.example.com",
            from_addr="niki@example.com",
            to_addrs=[],
        )
        await sink.dispatch(_sample_alert())

    async def test_subject_contains_alert_type_and_actor(self) -> None:
        sink = EmailAlertDispatcher(
            smtp_host="smtp.example.com",
            from_addr="niki@example.com",
            to_addrs=["sec@example.com"],
        )
        subject, body = sink._render(_sample_alert())
        assert "parent_child_mismatch" in subject
        assert "p-actor" in subject
        assert "Lateral-auth burst" in body
        assert "p-victim" in body
        assert "c-1" in body

    async def test_sends_via_smtp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the actual SMTP call is made with the right
        subject and body via asyncio.to_thread."""
        # Patch smtplib.SMTP at the module level the sink imports it from.
        mock_smtp_class = MagicMock()
        mock_smtp_instance = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr("smtplib.SMTP", mock_smtp_class)
        import asyncio

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

        sink = EmailAlertDispatcher(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="u",
            password="p",
            from_addr="niki@example.com",
            to_addrs=["sec@example.com"],
        )
        await sink.dispatch(_sample_alert())

        mock_smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=5)
        mock_smtp_instance.starttls.assert_called_once()
        mock_smtp_instance.login.assert_called_once_with("u", "p")
        mock_smtp_instance.send_message.assert_called_once()
        sent = mock_smtp_instance.send_message.call_args.args[0]
        assert "Niki 安全告警" in sent["Subject"]

    async def test_smtp_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failing SMTP call must raise so the Composite
        dispatcher can log it as a sink failure."""
        mock_smtp_class = MagicMock()
        mock_smtp_class.side_effect = ConnectionRefusedError("smtp down")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr("smtplib.SMTP", mock_smtp_class)
        import asyncio

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

        sink = EmailAlertDispatcher(
            smtp_host="smtp.example.com",
            from_addr="niki@example.com",
            to_addrs=["sec@example.com"],
        )
        with pytest.raises(ConnectionRefusedError):
            await sink.dispatch(_sample_alert())


class TestCompositeAlertDispatcherParallelFanOut:
    async def test_all_sinks_called_in_parallel(self) -> None:
        """All sinks should be invoked once. The implementation
        uses asyncio.gather so they run concurrently."""
        a = AsyncMock(spec=AlertDispatcher)
        b = AsyncMock(spec=AlertDispatcher)
        c = AsyncMock(spec=AlertDispatcher)
        composite = CompositeAlertDispatcher([a, b, c])
        await composite.dispatch(_sample_alert())
        a.dispatch.assert_awaited_once()
        b.dispatch.assert_awaited_once()
        c.dispatch.assert_awaited_once()

    async def test_one_sink_failure_does_not_block_others(self) -> None:
        """A failing sink must not stop the others. The
        Composite collects exceptions via return_exceptions=True
        and only logs them — no propagation."""
        a = AsyncMock(spec=AlertDispatcher)
        b = AsyncMock(
            spec=AlertDispatcher,
            side_effect=ConnectionError("webhook down"),
        )
        c = AsyncMock(spec=AlertDispatcher)
        composite = CompositeAlertDispatcher([a, b, c])
        # Must not raise even though b fails.
        await composite.dispatch(_sample_alert())
        a.dispatch.assert_awaited_once()
        c.dispatch.assert_awaited_once()

    async def test_sink_exception_returned_not_propagated(self) -> None:
        """Even a RuntimeError in one sink must not escape the
        composite (and must not skip the other sinks)."""
        a = AsyncMock(
            spec=AlertDispatcher,
            side_effect=RuntimeError("boom"),
        )
        b = AsyncMock(spec=AlertDispatcher)
        composite = CompositeAlertDispatcher([a, b])
        # No exception
        await composite.dispatch(_sample_alert())
        b.dispatch.assert_awaited_once()

    async def test_composite_with_email_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A composite that includes an email sink calls both
        the email and other sinks in parallel."""

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        mock_smtp_class = MagicMock()
        mock_smtp_class.return_value.__enter__.return_value = MagicMock()
        monkeypatch.setattr("smtplib.SMTP", mock_smtp_class)
        import asyncio

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

        webhook = AsyncMock(spec=AlertDispatcher)
        composite = CompositeAlertDispatcher(
            [
                webhook,
                EmailAlertDispatcher(
                    smtp_host="smtp.example.com",
                    from_addr="niki@example.com",
                    to_addrs=["sec@example.com"],
                ),
            ]
        )
        await composite.dispatch(_sample_alert())
        webhook.dispatch.assert_awaited_once()
        mock_smtp_class.assert_called_once()
