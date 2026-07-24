"""Tests for niki.infrastructure.heartbeat (BE-STOR-002)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from niki.infrastructure.heartbeat import HeartbeatService


pytestmark = pytest.mark.asyncio


class TestRegistration:
    def test_register_before_start(self) -> None:
        svc = HeartbeatService()

        async def ping():
            return {"status": "healthy"}

        svc.register("foo", ping, interval_seconds=10)
        assert len(svc._registrations) == 1

    async def test_register_after_start_raises(self) -> None:
        svc = HeartbeatService()

        async def ping():
            return {"status": "healthy"}

        svc.register("foo", ping)
        await svc.start()
        try:
            with pytest.raises(RuntimeError, match="Cannot register after start"):
                svc.register("bar", ping)
        finally:
            await svc.stop()

    def test_register_replaces_same_name(self) -> None:
        svc = HeartbeatService()

        async def ping_a():
            return {"status": "healthy"}

        async def ping_b():
            return {"status": "healthy"}

        svc.register("foo", ping_a)
        svc.register("foo", ping_b)
        assert len(svc._registrations) == 1
        assert svc._registrations[0].ping_fn is ping_b


class TestLifecycle:
    async def test_start_spawns_one_task_per_registration(self) -> None:
        svc = HeartbeatService()

        async def ping():
            return {"status": "healthy", "latency_ms": 1.0}

        svc.register("a", ping)
        svc.register("b", ping)
        await svc.start()
        try:
            assert all(r.task is not None for r in svc._registrations)
            assert all(not r.task.done() for r in svc._registrations)
        finally:
            await svc.stop()

    async def test_stop_cancels_and_awaits(self) -> None:
        svc = HeartbeatService()

        async def ping():
            return {"status": "healthy"}

        svc.register("a", ping)
        svc.register("b", ping)
        await svc.start()
        await svc.stop()
        assert all(r.task.done() for r in svc._registrations)

    async def test_double_start_is_idempotent(self) -> None:
        svc = HeartbeatService()

        async def ping():
            return {"status": "healthy"}

        svc.register("a", ping)
        await svc.start()
        await svc.start()  # no-op
        try:
            assert sum(r.task is not None for r in svc._registrations) == 1
        finally:
            await svc.stop()


class TestCheckExecution:
    async def _wait_for_first_check(self, svc: HeartbeatService, name: str) -> None:
        # First check happens immediately; sleep tiny grace for the snapshot write.
        for _ in range(20):
            snap = await svc.aggregate_health()
            if name in snap:
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"{name} did not appear in snapshot within 0.2s")

    async def test_healthy_check_populates_snapshot(self) -> None:
        svc = HeartbeatService()

        async def ping():
            return {"status": "healthy", "latency_ms": 1.5}

        svc.register("ok", ping, interval_seconds=1.0)
        await svc.start()
        try:
            await self._wait_for_first_check(svc, "ok")
            snap = await svc.aggregate_health()
            assert snap["ok"]["status"] == "healthy"
            assert "checked_at" in snap["ok"]
        finally:
            await svc.stop()

    async def test_unhealthy_check_isolated(self) -> None:
        """A bad ping must NOT affect a neighbouring good ping."""
        svc = HeartbeatService()

        async def ping_good():
            return {"status": "healthy", "latency_ms": 1.0}

        async def ping_bad():
            return {"status": "unhealthy", "latency_ms": None, "error": "boom"}

        svc.register("good", ping_good, interval_seconds=0.05)
        svc.register("bad", ping_bad, interval_seconds=0.05)
        await svc.start()
        try:
            await self._wait_for_first_check(svc, "good")
            await self._wait_for_first_check(svc, "bad")
            snap = await svc.aggregate_health()
            assert snap["good"]["status"] == "healthy"
            assert snap["bad"]["status"] == "unhealthy"
            assert snap["bad"]["error"] == "boom"
        finally:
            await svc.stop()

    async def test_ping_exception_marked_unhealthy(self) -> None:
        """An unexpected raise inside ping_fn must be caught and reported."""
        svc = HeartbeatService()

        async def ping_raises():
            raise RuntimeError("niki-redis-down")

        svc.register("crash", ping_raises, interval_seconds=0.05)
        await svc.start()
        try:
            await self._wait_for_first_check(svc, "crash")
            snap = await svc.aggregate_health()
            assert snap["crash"]["status"] == "unhealthy"
            assert "ping raised" in snap["crash"]["error"]
        finally:
            await svc.stop()

    async def test_loop_keeps_running_after_ping_exception(self) -> None:
        """An exception must not terminate the loop."""
        svc = HeartbeatService()
        counter = {"n": 0}

        async def ping_flaky():
            counter["n"] += 1
            if counter["n"] == 1:
                raise RuntimeError("first call fails")
            return {"status": "healthy", "latency_ms": 1.0}

        svc.register("flaky", ping_flaky, interval_seconds=0.05)
        await svc.start()
        try:
            # Wait for at least 3 successful calls (after the first crash).
            for _ in range(50):
                await asyncio.sleep(0.05)
                if counter["n"] >= 3:
                    return  # pass
            pytest.fail("loop died after first ping exception")
        finally:
            await svc.stop()

    async def test_aggregate_health_returns_independent_snapshots(self) -> None:
        """Each aggregate_health call returns a fresh dict (no aliasing)."""
        svc = HeartbeatService()

        async def ping():
            return {"status": "healthy", "latency_ms": 1.0}

        svc.register("x", ping, interval_seconds=1.0)
        await svc.start()
        try:
            await self._wait_for_first_check(svc, "x")
            snap1 = await svc.aggregate_health()
            snap1["x"]["status"] = "TAMPERED"
            snap2 = await svc.aggregate_health()
            assert snap2["x"]["status"] == "healthy"
        finally:
            await svc.stop()
