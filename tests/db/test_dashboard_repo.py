"""Unit tests for niki.db.repositories.dashboard_repo (BE-STORG-001-ext-C).

Both the InMemory and the Postgres (via SQLite-async) implementations
are exercised against the same contract.
"""

from __future__ import annotations

import pytest

from niki.db.repositories import (
    InMemoryDashboardRepository,
    PostgresDashboardRepository,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class TestInMemoryRepo:
    async def test_add_alert_returns_id(self) -> None:
        repo = InMemoryDashboardRepository()
        row_id = await repo.add_alert(
            alert_type="safety_valve",
            child_id="c1",
            parent_id="p1",
            payload={"problem_summary": "x"},
        )
        assert isinstance(row_id, str) and len(row_id) > 0

    async def test_list_alerts_filters_by_type(self) -> None:
        repo = InMemoryDashboardRepository()
        await repo.add_alert(
            alert_type="safety_valve",
            child_id="c1",
            parent_id="p1",
            payload={"problem_summary": "x"},
        )
        await repo.add_alert(
            alert_type="high_risk",
            child_id="c1",
            parent_id="p1",
            payload={"original_text": "y"},
        )
        sv = await repo.list_alerts(alert_type="safety_valve", child_id="c1")
        hr = await repo.list_alerts(alert_type="high_risk", child_id="c1")
        assert len(sv) == 1
        assert len(hr) == 1
        assert sv[0]["alert_type"] == "safety_valve"
        assert hr[0]["alert_type"] == "high_risk"

    async def test_list_alerts_filters_by_child(self) -> None:
        repo = InMemoryDashboardRepository()
        for c in ("c1", "c2", "c1"):
            await repo.add_alert(
                alert_type="safety_valve",
                child_id=c,
                parent_id="p",
                payload={"x": 1},
            )
        assert len(await repo.list_alerts(alert_type="safety_valve", child_id="c1")) == 2
        assert len(await repo.list_alerts(alert_type="safety_valve", child_id="c2")) == 1

    async def test_mark_alert_delivered(self) -> None:
        repo = InMemoryDashboardRepository()
        row_id = await repo.add_alert(
            alert_type="high_risk",
            child_id="c1",
            parent_id="p",
            payload={"x": 1},
        )
        ok = await repo.mark_alert_delivered(row_id)
        assert ok is True
        rows = await repo.list_alerts(alert_type="high_risk", child_id="c1")
        assert rows[0]["delivered_at"] is not None

    async def test_mark_unknown_alert_returns_false(self) -> None:
        repo = InMemoryDashboardRepository()
        assert await repo.mark_alert_delivered("nope") is False

    async def test_tutoring_event_round_trip(self) -> None:
        repo = InMemoryDashboardRepository()
        row_id = await repo.add_tutoring_event(
            child_id="c1",
            parent_id="p",
            payload={"problem": "q1", "solved": True},
        )
        rows = await repo.list_tutoring_events("c1")
        assert len(rows) == 1
        assert rows[0]["id"] == row_id
        assert rows[0]["payload"]["solved"] is True

    async def test_tutoring_event_isolates_per_child(self) -> None:
        repo = InMemoryDashboardRepository()
        await repo.add_tutoring_event(child_id="c1", parent_id="p", payload={"p": 1})
        await repo.add_tutoring_event(child_id="c2", parent_id="p", payload={"p": 2})
        assert len(await repo.list_tutoring_events("c1")) == 1
        assert len(await repo.list_tutoring_events("c2")) == 1

    async def test_knowledge_query_round_trip(self) -> None:
        repo = InMemoryDashboardRepository()
        await repo.add_knowledge_query(
            child_id="c1",
            parent_id="p",
            payload={"question": "why?", "topic": "physics"},
        )
        rows = await repo.list_knowledge_queries("c1")
        assert len(rows) == 1
        assert rows[0]["payload"]["topic"] == "physics"

    async def test_len_counts_all_record_types(self) -> None:
        repo = InMemoryDashboardRepository()
        for i in range(2):
            await repo.add_alert(
                alert_type="safety_valve", child_id=f"c{i}", parent_id="p", payload={}
            )
            await repo.add_tutoring_event(child_id=f"c{i}", parent_id="p", payload={})
            await repo.add_knowledge_query(child_id=f"c{i}", parent_id="p", payload={})
        # 2 alerts + 2 events + 2 queries = 6
        assert len(repo) == 6


# ---------------------------------------------------------------------------
# Postgres implementation (against SQLite-async for portability)
# ---------------------------------------------------------------------------


class TestPostgresRepoSqlite:
    """Smoke test the SQLAlchemy impl via in-memory SQLite.

    Production uses real Postgres; the SQL surface is identical for
    these operations (insert / select / delete by predicate).
    """

    async def test_round_trip_alert(self, async_session_factory) -> None:
        async with async_session_factory() as session:
            repo = PostgresDashboardRepository(session)
            await repo.add_alert(
                alert_type="safety_valve",
                child_id="c1",
                parent_id="p1",
                payload={"problem_summary": "fractions"},
            )
            await session.commit()

        async with async_session_factory() as session:
            repo2 = PostgresDashboardRepository(session)
            rows = await repo2.list_alerts(alert_type="safety_valve", child_id="c1")
        assert len(rows) == 1
        assert rows[0]["child_id"] == "c1"
        assert rows[0]["payload"]["problem_summary"] == "fractions"
        assert rows[0]["delivered_at"] is None

    async def test_mark_alert_delivered_via_postgres(self, async_session_factory) -> None:
        async with async_session_factory() as session:
            repo = PostgresDashboardRepository(session)
            row_id = await repo.add_alert(
                alert_type="high_risk",
                child_id="c1",
                parent_id="p1",
                payload={"x": 1},
            )
            await session.commit()

        async with async_session_factory() as session:
            repo2 = PostgresDashboardRepository(session)
            ok = await repo2.mark_alert_delivered(row_id)
            assert ok is True
            await session.commit()

        async with async_session_factory() as session:
            repo3 = PostgresDashboardRepository(session)
            rows = await repo3.list_alerts(alert_type="high_risk", child_id="c1")
        assert rows[0]["delivered_at"] is not None
