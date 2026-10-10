"""Tests for BE-API-004 list filter / sort / search.

Three list endpoints now accept `?since=&until=&sort=&order=&keyword=`
on top of the BE-API-003 `?page=&page_size=`:

- `GET /auth/audit/recent` (limit-based)
- `GET /treehole/summaries/{child_id}` (paginated)
- `GET /memory/graph/entities` (paginated)

Validation rules:
- `order` must be `asc` or `desc`; anything else is 400.
- `sort` is whitelisted per endpoint; bad values are 400.
- `keyword` is a case-insensitive substring match.
- Filters compose with `?page=&page_size=`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from niki.api._pagination import (
    ListQueryParams,
    list_query_params,
    sort_rows,
)
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.db.repositories import (
    AuthAuditEvent,
    InMemoryAuthAuditLogRepository,
    get_auth_audit_log_repository,
    set_auth_audit_log_repository,
)
from niki.main import create_app
from niki.services.child_registry import (
    InMemoryChildRegistry,
    set_child_registry,
)


# ---------------------------------------------------------------------------
# Helper: ListQueryParams direct unit tests
# ---------------------------------------------------------------------------


class TestListQueryParamsHelper:
    def test_default_instantiation(self) -> None:
        p = ListQueryParams()
        assert p.page == 1
        assert p.page_size == 20
        assert p.since is None
        assert p.until is None
        assert p.sort == "created_at"
        assert p.order == "desc"
        assert p.keyword is None
        assert p.is_ascending is False

    def test_normalized_keyword_strips_and_lowers(self) -> None:
        p = ListQueryParams(keyword="  Foo  ")
        assert p.normalized_keyword() == "foo"

    def test_normalized_keyword_empty(self) -> None:
        assert ListQueryParams(keyword=None).normalized_keyword() is None
        assert ListQueryParams(keyword="   ").normalized_keyword() is None
        assert ListQueryParams(keyword="").normalized_keyword() is None


class TestListQueryParamsDependency:
    def _build_app_with_route(self) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
            id="p1",
            name="T",
            id_number_masked="****",
            phone_masked="****",
        )
        return TestClient(app)

    def test_invalid_order_returns_400(self) -> None:
        client = self._build_app_with_route()

        @client.app.get("/__test/q_order")  # type: ignore[attr-defined]
        def _q(
            params: ListQueryParams = Depends(list_query_params),
        ) -> dict:
            return {"order": params.order}

        r = client.get("/__test/q_order?order=foo")
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == "INVALID_ORDER"

    def test_invalid_range_returns_400(self) -> None:
        client = self._build_app_with_route()

        @client.app.get("/__test/q_range")  # type: ignore[attr-defined]
        def _q(
            params: ListQueryParams = Depends(list_query_params),
        ) -> dict:
            return {"page": params.page}

        r = client.get(
            "/__test/q_range?since=2026-07-31T00:00:00Z&until=2026-07-01T00:00:00Z",
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_RANGE"

    def test_whitespace_keyword_treated_as_none(self) -> None:
        client = self._build_app_with_route()

        @client.app.get("/__test/q_kw")  # type: ignore[attr-defined]
        def _q(
            params: ListQueryParams = Depends(list_query_params),
        ) -> dict:
            return {"keyword": params.keyword}

        r = client.get("/__test/q_kw?keyword=%20%20%20")
        assert r.status_code == 200
        assert r.json()["keyword"] is None

    def test_default_asc_marker(self) -> None:
        p = ListQueryParams(order="asc")
        assert p.is_ascending is True


class TestSortRowsHelper:
    def test_default_sorts_desc_by_datetime(self) -> None:
        rows = [
            {"created_at": datetime(2026, 7, 1, tzinfo=timezone.utc)},
            {"created_at": datetime(2026, 7, 31, tzinfo=timezone.utc)},
            {"created_at": datetime(2026, 7, 15, tzinfo=timezone.utc)},
        ]
        out = sort_rows(rows, sort="created_at", order="desc")
        assert [r["created_at"].day for r in out] == [31, 15, 1]

    def test_ascending(self) -> None:
        rows = [{"x": 3}, {"x": 1}, {"x": 2}]
        out = sort_rows(rows, sort="x", order="asc", time_field="x")
        assert [r["x"] for r in out] == [1, 2, 3]

    def test_unknown_sort_falls_back_to_time_field(self) -> None:
        rows = [
            {"created_at": datetime(2026, 7, 5, tzinfo=timezone.utc), "x": 99},
            {"created_at": datetime(2026, 7, 1, tzinfo=timezone.utc), "x": 1},
        ]
        out = sort_rows(rows, sort="nonsense", order="asc")
        assert [r["created_at"].day for r in out] == [1, 5]


# ---------------------------------------------------------------------------
# /api/v1/auth/audit/recent
# ---------------------------------------------------------------------------


def _audit_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    set_child_registry(InMemoryChildRegistry())
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p_actor",
        name="T",
        id_number_masked="****",
        phone_masked="****",
    )
    return TestClient(app)


async def _seed_audit(
    rows: list[AuthAuditEvent],
    *,
    offset_seconds: list[int] | None = None,
) -> None:
    repo = get_auth_audit_log_repository()
    if hasattr(repo, "_rows"):
        repo._rows.clear()  # type: ignore[attr-defined]
    for row in rows:
        await repo.add(row)
    if offset_seconds:
        # Override created_at sequentially for deterministic
        # since/until filtering. We rely on insertion order
        # matching the row list.
        keys = list(repo._rows.keys())  # type: ignore[attr-defined]
        base = datetime.now(timezone.utc)
        for key, seconds in zip(keys, offset_seconds, strict=False):
            repo._rows[key]["created_at"] = base - timedelta(  # type: ignore[attr-defined]
                seconds=seconds,
            )


class TestAuthAuditFilter:
    @pytest.fixture(autouse=True)
    def _fresh_repo(self) -> None:
        # Save and restore the global auth-audit repo so seed
        # rows from one test don't leak into the next test class.
        from niki.db.repositories.auth_audit_repo import (
            get_auth_audit_log_repository,
        )

        original = get_auth_audit_log_repository()
        set_auth_audit_log_repository(InMemoryAuthAuditLogRepository())
        yield
        set_auth_audit_log_repository(original)

    def test_default_no_filter_returns_self_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _audit_client(monkeypatch)
        asyncio.run(
            _seed_audit(
                [
                    AuthAuditEvent(
                        event_type="parent_mismatch",
                        actor_parent_id="p_actor",
                        endpoint="/api/v1/foo",
                    ),
                ]
            )
        )
        r = client.get("/api/v1/auth/audit/recent")
        assert r.status_code == 200
        body = r.json()
        # Self-only scoping still applies.
        for ev in body["events"]:
            assert ev["actor_parent_id"] == "p_actor"

    def test_invalid_order_returns_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _audit_client(monkeypatch)
        r = client.get("/api/v1/auth/audit/recent?order=foo")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_ORDER"

    def test_invalid_sort_returns_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _audit_client(monkeypatch)
        r = client.get("/api/v1/auth/audit/recent?sort=evil")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_SORT"

    def test_since_until_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _audit_client(monkeypatch)
        # Insert 3 rows in order; offset each by 10/5/0 seconds
        # in the past so we can confirm the time window cuts them.
        now = datetime.now(timezone.utc)
        asyncio.run(
            _seed_audit(
                [
                    AuthAuditEvent(
                        event_type="parent_mismatch",
                        actor_parent_id="p_actor",
                        endpoint="/api/v1/old",
                    ),
                    AuthAuditEvent(
                        event_type="parent_mismatch",
                        actor_parent_id="p_actor",
                        endpoint="/api/v1/middle",
                    ),
                    AuthAuditEvent(
                        event_type="parent_mismatch",
                        actor_parent_id="p_actor",
                        endpoint="/api/v1/new",
                    ),
                ],
                offset_seconds=[10, 5, 0],
            )
        )

        r = client.get(
            "/api/v1/auth/audit/recent",
            params={
                "since": (now - timedelta(seconds=7)).isoformat(),
                "until": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        body = r.json()
        endpoints = [ev["endpoint"] for ev in body["events"]]
        assert any("middle" in e for e in endpoints)
        assert any("new" in e for e in endpoints)
        assert not any("old" in e for e in endpoints)

    def test_keyword_substring_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _audit_client(monkeypatch)
        asyncio.run(
            _seed_audit(
                [
                    AuthAuditEvent(
                        event_type="parent_mismatch",
                        actor_parent_id="p_actor",
                        endpoint="/api/v1/USERS/p_probe",
                    ),
                    AuthAuditEvent(
                        event_type="child_not_found",
                        actor_parent_id="p_actor",
                        endpoint="/api/v1/other",
                    ),
                ]
            )
        )
        r = client.get(
            "/api/v1/auth/audit/recent?keyword=USERS",
        )
        body = r.json()
        endpoints = [ev["endpoint"] for ev in body["events"]]
        assert len(endpoints) == 1
        assert "users" in endpoints[0].lower()

    def test_sort_order_asc_reverses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _audit_client(monkeypatch)
        asyncio.run(
            _seed_audit(
                [
                    AuthAuditEvent(
                        event_type=f"e{i}",
                        actor_parent_id="p_actor",
                        endpoint=f"/a/{i}",
                    )
                    for i in range(3)
                ],
                offset_seconds=[3600, 1800, 0],
            )
        )
        r = client.get(
            "/api/v1/auth/audit/recent?sort=created_at&order=asc",
        )
        body = r.json()
        timestamps = [ev["created_at"] for ev in body["events"]]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# /api/v1/memory/graph/entities
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_client() -> TestClient:
    from niki.api.memory import _graph

    _graph._entities.clear()
    _graph._edges.clear()
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p1",
        name="T",
        id_number_masked="****",
        phone_masked="****",
    )
    return TestClient(app)


class TestMemoryGraphEntitiesFilter:
    def test_keyword_matches_name_case_insensitive(self, memory_client: TestClient) -> None:
        from niki.api.memory import _graph

        _graph.add_entity(name="Dinosaur", entity_type="concept")
        _graph.add_entity(name="T-Rex", entity_type="concept")
        _graph.add_entity(name="unicorn", entity_type="concept")

        r = memory_client.get(
            "/api/v1/memory/graph/entities?keyword=dino",
        )
        body = r.json()
        names = [e["name"] for e in body["entities"]]
        assert "Dinosaur" in names
        assert "T-Rex" not in names
        assert "unicorn" not in names

    def test_keyword_matches_aliases(self, memory_client: TestClient) -> None:
        from niki.api.memory import _graph

        _graph.add_entity(
            name="霸王龙",
            entity_type="concept",
            aliases=["T-Rex", "Tyrannosaurus"],
        )
        _graph.add_entity(name="unicorn", entity_type="concept")

        r = memory_client.get(
            "/api/v1/memory/graph/entities?keyword=rex",
        )
        body = r.json()
        names = [e["name"] for e in body["entities"]]
        assert "霸王龙" in names
        assert "unicorn" not in names

    def test_sort_name_orders_alphabetically(self, memory_client: TestClient) -> None:
        from niki.api.memory import _graph

        for n in ["charlie", "alpha", "bravo"]:
            _graph.add_entity(name=n, entity_type="concept")

        r = memory_client.get(
            "/api/v1/memory/graph/entities?sort=name&order=asc",
        )
        body = r.json()
        names = [e["name"] for e in body["entities"]]
        assert names == sorted(names)

    def test_filter_composes_with_pagination(self, memory_client: TestClient) -> None:
        from niki.api.memory import _graph

        for i in range(30):
            _graph.add_entity(name=f"target{i}", entity_type="concept")
        for i in range(20):
            _graph.add_entity(name=f"other{i}", entity_type="concept")

        r = memory_client.get(
            "/api/v1/memory/graph/entities?keyword=target&page=2&page_size=10",
        )
        body = r.json()
        assert body["total"] == 30
        assert body["page"] == 2
        assert body["page_size"] == 10
        assert len(body["entities"]) == 10
        # Every entity on the page must match the keyword.
        assert all("target" in e["name"] for e in body["entities"])


# ---------------------------------------------------------------------------
# /api/v1/treehole/summaries/{child_id}
# ---------------------------------------------------------------------------


@pytest.fixture
def treehole_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    set_child_registry(InMemoryChildRegistry())
    from niki.services.child_registry import get_child_registry

    get_child_registry().register("c-flt", "p_flt")
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p_flt",
        name="T",
        id_number_masked="****",
        phone_masked="****",
    )
    return TestClient(app)


def _seed_treehole_manager(
    *,
    family: int = 0,
    school: int = 0,
    other: int = 0,
):
    """Build a TreeholeManager seeded with the requested mix.

    Returns the manager so the endpoint can be patched onto
    `niki.api.treehole._manager`.
    """
    from niki.treehole.manager import TreeholeManager
    from niki.treehole.encryption import TreeholeEncryption

    mgr = TreeholeManager(
        encryption=TreeholeEncryption(key=b"T" * 32),
    )
    loop = asyncio.new_event_loop()
    try:
        for _ in range(family):
            loop.run_until_complete(
                mgr.engage(
                    parent_id="p_flt",
                    child_id="c-flt",
                    original_text="x",
                    emotion="neutral",
                    intensity=3,
                    category="family",
                ),
            )
        for _ in range(school):
            loop.run_until_complete(
                mgr.engage(
                    parent_id="p_flt",
                    child_id="c-flt",
                    original_text="x",
                    emotion="neutral",
                    intensity=3,
                    category="school",
                ),
            )
        for _ in range(other):
            loop.run_until_complete(
                mgr.engage(
                    parent_id="p_flt",
                    child_id="c-flt",
                    original_text="x",
                    emotion="neutral",
                    intensity=3,
                    category="other",
                ),
            )
    finally:
        loop.close()
    return mgr


class TestTreeholeSummariesFilter:
    @pytest.fixture(autouse=True)
    def _restore_manager(self) -> None:
        """Each test seeds a fresh TreeholeManager and patches
        it onto `niki.api.treehole._manager`; without restoring,
        subsequent tests in the suite see the seeded rows."""
        import niki.api.treehole as api_tree

        original = api_tree._manager
        yield
        api_tree._manager = original

    def test_keyword_filter_category(self, treehole_client: TestClient) -> None:
        import niki.api.treehole as api_tree

        mgr = _seed_treehole_manager(family=10, school=10)
        api_tree._manager = mgr

        r = treehole_client.get(
            "/api/v1/treehole/summaries/c-flt?keyword=family",
        )
        body = r.json()
        assert body["total"] == 10
        for s in body["summaries"]:
            assert s["emotion_category"] == "family"

    def test_invalid_order_returns_400(self, treehole_client: TestClient) -> None:
        r = treehole_client.get(
            "/api/v1/treehole/summaries/c-flt?order=foo",
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_ORDER"

    def test_sort_order_asc(self, treehole_client: TestClient) -> None:
        import niki.api.treehole as api_tree

        mgr = _seed_treehole_manager(other=3)
        api_tree._manager = mgr

        # Desc default — first record is the newest timestamp.
        desc = treehole_client.get(
            "/api/v1/treehole/summaries/c-flt",
        ).json()
        # Asc must reverse that order.
        asc = treehole_client.get(
            "/api/v1/treehole/summaries/c-flt?order=asc",
        ).json()
        assert desc["summaries"]
        assert asc["summaries"]
        assert desc["summaries"][0]["timestamp"] >= (desc["summaries"][-1]["timestamp"])
        assert asc["summaries"][0]["timestamp"] <= (asc["summaries"][-1]["timestamp"])

    def test_pagination_composes_with_keyword(self, treehole_client: TestClient) -> None:
        import niki.api.treehole as api_tree

        mgr = _seed_treehole_manager(other=25)
        api_tree._manager = mgr

        r = treehole_client.get(
            "/api/v1/treehole/summaries/c-flt?keyword=other&page=2&page_size=10",
        )
        body = r.json()
        assert body["total"] == 25
        assert body["page"] == 2
        assert len(body["summaries"]) == 10
