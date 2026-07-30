"""Tests for the AlertBroker (BE-RT-002)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from niki.realtime.alert_broker import (
    MAX_QUEUE_SIZE,
    AlertBroker,
)
from niki.security.alerts import SecurityAlert


def _sample_alert(
    actor: str = "p-actor",
    alert_type: str = "lateral_auth_burst",
) -> SecurityAlert:
    return SecurityAlert(
        alert_type=alert_type,
        severity="critical",
        actor_parent_id=actor,
        target_parent_id="p-victim",
        target_child_id="c-1",
        client_ip="10.0.0.1",
        event_count=3,
        window_seconds=300,
        detected_at=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        title="Lateral-auth burst",
        detail="actor 'p-actor' did something bad",
    )


class TestAlertBroker:
    async def test_publish_routes_to_subscriber(self) -> None:
        broker = AlertBroker()
        q = broker.subscribe("p-actor")
        broker.publish(_sample_alert("p-actor"))
        alert = q.get_nowait()
        assert alert.alert_type == "lateral_auth_burst"
        assert alert.actor_parent_id == "p-actor"

    async def test_publish_isolated_by_parent_id(self) -> None:
        """Parent A's alerts must NOT reach Parent B's queue."""
        broker = AlertBroker()
        q_a = broker.subscribe("p-a")
        q_b = broker.subscribe("p-b")
        broker.publish(_sample_alert("p-a"))
        # q_b should be empty.
        with pytest.raises(asyncio.QueueEmpty):
            q_b.get_nowait()
        # q_a got the alert.
        assert q_a.get_nowait().actor_parent_id == "p-a"

    async def test_publish_no_subscribers_is_noop(self) -> None:
        broker = AlertBroker()
        # No subscribers.
        accepted = broker.publish(_sample_alert("p-x"))
        assert accepted == 0

    async def test_publish_no_actor_id(self) -> None:
        broker = AlertBroker()
        q = broker.subscribe("p-actor")
        alert = _sample_alert(actor="p-actor")
        # Manually clear the actor_parent_id to test the no-actor
        # path.
        object.__setattr__(alert, "actor_parent_id", None)
        accepted = broker.publish(alert)
        assert accepted == 0
        # Subscriber received nothing.
        with pytest.raises(asyncio.QueueEmpty):
            q.get_nowait()

    async def test_multiple_subscribers_per_parent(self) -> None:
        """Two subscribers for the same parent (e.g. two tabs)
        both get the alert."""
        broker = AlertBroker()
        q1 = broker.subscribe("p-1")
        q2 = broker.subscribe("p-1")
        accepted = broker.publish(_sample_alert("p-1"))
        assert accepted == 2
        assert q1.get_nowait().actor_parent_id == "p-1"
        assert q2.get_nowait().actor_parent_id == "p-1"

    async def test_unsubscribe(self) -> None:
        broker = AlertBroker()
        q1 = broker.subscribe("p-1")
        q2 = broker.subscribe("p-1")
        broker.unsubscribe("p-1", q1)
        accepted = broker.publish(_sample_alert("p-1"))
        assert accepted == 1
        # q1 should be empty (unsubscribed), q2 got it.
        with pytest.raises(asyncio.QueueEmpty):
            q1.get_nowait()
        assert q2.get_nowait().actor_parent_id == "p-1"

    async def test_unsubscribe_removes_empty_set(self) -> None:
        broker = AlertBroker()
        q = broker.subscribe("p-1")
        broker.unsubscribe("p-1", q)
        # subscriber_count for p-1 is now 0.
        assert broker.subscriber_count("p-1") == 0
        # And p-1 has no entry in the dict at all.
        assert "p-1" not in broker._subscribers  # noqa: SLF001

    async def test_subscriber_count(self) -> None:
        broker = AlertBroker()
        broker.subscribe("p-1")
        broker.subscribe("p-1")
        broker.subscribe("p-2")
        assert broker.subscriber_count("p-1") == 2
        assert broker.subscriber_count("p-2") == 1
        assert broker.subscriber_count("p-3") == 0
        assert broker.subscriber_count() == 3

    async def test_queue_full_drops_alert(self) -> None:
        """A subscriber with a full queue is dropped, not
        blocked. We use a small queue cap to trigger the path."""
        broker = AlertBroker(max_queue_size=2)
        q = broker.subscribe("p-1")
        # Fill the queue
        broker.publish(_sample_alert("p-1"))
        broker.publish(_sample_alert("p-1"))
        # Next publish should log a warning and return 0 (drop).
        # But the broker doesn't actually drop the OTHER queues,
        # so this is just a no-op for the full queue.
        accepted = broker.publish(_sample_alert("p-1"))
        # The other subscriber (none here) would still get it.
        assert accepted == 0  # only q1 was subscribed, and it's full

    async def test_subscriber_id_appears_only_once(self) -> None:
        """A second subscribe from the same caller should add
        another queue (not deduplicate)."""
        broker = AlertBroker()
        q1 = broker.subscribe("p-1")
        q2 = broker.subscribe("p-1")
        # Two different queue objects.
        assert q1 is not q2
        assert broker.subscriber_count("p-1") == 2