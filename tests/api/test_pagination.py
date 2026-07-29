"""Tests for the BE-API-003 list-pagination helper."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.api._pagination import paginate, parse_pagination


class TestPaginateHelper:
    def test_default_page_returns_first_slice(self) -> None:
        items = list(range(25))
        result = paginate(items, page=1, page_size=10)
        assert result["total"] == 25
        assert result["page"] == 1
        assert result["page_size"] == 10
        assert result["has_more"] is True
        assert result["items"] == list(range(0, 10))

    def test_last_page_no_more(self) -> None:
        items = list(range(25))
        result = paginate(items, page=3, page_size=10)
        # Page 3 = items 20..24
        assert result["items"] == [20, 21, 22, 23, 24]
        assert result["has_more"] is False

    def test_beyond_last_page_returns_empty(self) -> None:
        items = list(range(5))
        result = paginate(items, page=10, page_size=10)
        assert result["items"] == []
        assert result["total"] == 5
        assert result["has_more"] is False

    def test_empty_input(self) -> None:
        result = paginate([], page=1, page_size=20)
        assert result["total"] == 0
        assert result["has_more"] is False
        assert result["items"] == []

    def test_custom_items_key(self) -> None:
        """End-points override the items key to keep their
        existing field name (entities / summaries)."""
        result = paginate(
            [{"id": 1}, {"id": 2}],
            page=1,
            page_size=20,
            items_key="entities",
            total_key="total",
        )
        assert "entities" in result
        assert result["entities"] == [{"id": 1}, {"id": 2}]
        # Custom total key is still surfaced
        assert "total" in result


class TestMemoryGraphEntitiesPagination:
    @pytest.fixture(autouse=True)
    def _reset_graph(self) -> None:
        """Clear the in-process cognitive graph before each test
        so cross-test state doesn't leak into `test_graph_add_*`."""
        from niki.api.memory import _graph
        _graph._entities.clear()
        _graph._edges.clear()
        yield
        _graph._entities.clear()
        _graph._edges.clear()

    @pytest.fixture
    def client(self) -> TestClient:
        from niki.auth.account import ParentAccount
        from niki.auth.dependencies import get_current_parent
        from niki.main import create_app
        app = create_app()
        app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
            id="p1", name="T",
            id_number_masked="****", phone_masked="****",
        )
        return TestClient(app)

    def test_default_page_size(self, client: TestClient) -> None:
        from niki.api.memory import _graph
        for i in range(25):
            _graph.add_entity(
                name=f"e{i}",
                entity_type="concept",
            )
        r = client.get("/api/v1/memory/graph/entities")
        assert r.status_code == 200
        body = r.json()
        # Default page_size = 20
        assert body["total"] == 25
        assert body["page_size"] == 20
        assert body["page"] == 1
        assert body["has_more"] is True
        assert len(body["entities"]) == 20
        # Back-compat
        assert body["count"] == 25

    def test_explicit_pagination(self, client: TestClient) -> None:
        from niki.api.memory import _graph
        for i in range(25):
            _graph.add_entity(name=f"e{i}", entity_type="concept")
        r = client.get(
            "/api/v1/memory/graph/entities?page=2&page_size=10"
        )
        body = r.json()
        assert body["page"] == 2
        assert body["page_size"] == 10
        assert body["has_more"] is True  # 25 > 20
        assert len(body["entities"]) == 10
        # First entity on page 2 should be the 11th overall
        assert body["entities"][0]["name"] == "e10"

    def test_invalid_page_size_returns_422(
        self, client: TestClient
    ) -> None:
        r = client.get(
            "/api/v1/memory/graph/entities?page_size=500"
        )
        assert r.status_code == 422

    def test_invalid_page_returns_422(self, client: TestClient) -> None:
        r = client.get("/api/v1/memory/graph/entities?page=0")
        assert r.status_code == 422


class TestTreeholeSummariesPagination:
    @pytest.fixture(autouse=True)
    def _reset(self) -> None:
        """Each test uses its own manager so cross-test state
        is contained; restore the module-level _manager after."""
        import niki.api.treehole as api_tree
        original = api_tree._manager
        yield
        api_tree._manager = original

    @pytest.fixture
    def client(self) -> TestClient:
        from niki.auth.account import ParentAccount
        from niki.auth.dependencies import get_current_parent
        from niki.main import create_app
        from niki.services.child_registry import (
            InMemoryChildRegistry,
            set_child_registry,
        )
        set_child_registry(InMemoryChildRegistry())
        from niki.services.child_registry import get_child_registry
        get_child_registry().register("c-pag", "p_pag")
        app = create_app()
        app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
            id="p_pag", name="T",
            id_number_masked="****", phone_masked="****",
        )
        return TestClient(app)

    def test_default_pagination(self, client: TestClient) -> None:
        from niki.treehole.manager import TreeholeManager
        from niki.treehole.encryption import TreeholeEncryption
        mgr = TreeholeManager(encryption=TreeholeEncryption(key=b"T" * 32))
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            for i in range(30):
                loop.run_until_complete(
                    mgr.engage(
                        parent_id="p_pag",
                        child_id="c-pag",
                        original_text=f"secret {i}",
                        emotion="negative" if i % 2 == 0 else "positive",
                        intensity=3,
                    )
                )
        finally:
            loop.close()
        # Patch the module-level _manager so the endpoint reads
        # from our seeded manager.
        import niki.api.treehole as api_tree
        api_tree._manager = mgr

        r = client.get("/api/v1/treehole/summaries/c-pag")
        body = r.json()
        assert body["child_id"] == "c-pag"
        assert body["total"] == 30
        assert body["page_size"] == 20
        assert body["has_more"] is True
        assert body["count"] == 30
        assert len(body["summaries"]) == 20

    def test_explicit_page(self, client: TestClient) -> None:
        from niki.treehole.manager import TreeholeManager
        from niki.treehole.encryption import TreeholeEncryption
        mgr = TreeholeManager(encryption=TreeholeEncryption(key=b"T" * 32))
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            for i in range(30):
                loop.run_until_complete(
                    mgr.engage(
                        parent_id="p_pag",
                        child_id="c-pag",
                        original_text=f"x{i}",
                        emotion="neutral",
                        intensity=3,
                    )
                )
        finally:
            loop.close()
        import niki.api.treehole as api_tree
        api_tree._manager = mgr

        r = client.get(
            "/api/v1/treehole/summaries/c-pag?page=2&page_size=10"
        )
        body = r.json()
        assert body["page"] == 2
        assert body["page_size"] == 10
        assert body["has_more"] is True  # 30 > 20
        assert len(body["summaries"]) == 10