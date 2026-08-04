"""Tests for BE-API-005 batch operations endpoints.

Three new batch endpoints sit alongside the existing single-item
endpoints; they collapse N round trips into 1:

  POST /api/v1/memory/graph/entities/batch       -> {created, skipped}
  POST /api/v1/memory/graph/relations/batch      -> {created, skipped}
  POST /api/v1/dashboard/alerts/batch/dismiss    -> {dismissed, skipped}

Validation rules:
- 100-item cap per request; 101+ items -> 400 BATCH_TOO_LARGE.
- Each item is independent; partial success is the contract.
- duplicate entity name -> skipped[reason="duplicate"].
- missing alert id    -> skipped[reason="not_found"].
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.api.memory import BATCH_MAX_ITEMS
from niki.dashboard.service import DashboardService
from niki.db.repositories import (
    InMemoryDashboardRepository,
    set_dashboard_repository,
)
from niki.memory.graph import CognitiveGraph


# ---------------------------------------------------------------------------
# Service-layer helpers (unit tests — no HTTP)
# ---------------------------------------------------------------------------


class TestAddEntitiesBatchHelper:
    def test_happy_path_5_entities(self) -> None:
        """5 fresh entities are all created (BE-API-005 AC)."""
        graph = CognitiveGraph()
        items = [
            {"name": "A", "entity_type": "concept", "aliases": []},
            {"name": "B", "entity_type": "person", "aliases": []},
            {"name": "C", "entity_type": "place", "aliases": []},
            {"name": "D", "entity_type": "topic", "aliases": []},
            {"name": "E", "entity_type": "thing", "aliases": []},
        ]
        out = graph.add_entities_batch(items)
        assert len(out["created"]) == 5
        assert out["skipped"] == []
        assert {e["name"] for e in out["created"]} == {"A", "B", "C", "D", "E"}

    def test_one_duplicate_skipped(self) -> None:
        """4 created + 1 skipped[reason='duplicate'] (BE-API-005 AC)."""
        graph = CognitiveGraph()
        graph.add_entity("B")  # Pre-existing < collision.
        items = [
            {"name": "A", "entity_type": "concept"},
            {"name": "B", "entity_type": "concept"},   # dup
            {"name": "C", "entity_type": "concept"},
            {"name": "D", "entity_type": "concept"},
            {"name": "E", "entity_type": "concept"},
        ]
        out = graph.add_entities_batch(items)
        assert len(out["created"]) == 4
        assert {e["name"] for e in out["created"]} == {"A", "C", "D", "E"}
        assert len(out["skipped"]) == 1
        assert out["skipped"][0]["name"] == "B"
        assert out["skipped"][0]["reason"] == "duplicate"

    def test_invalid_name_skipped(self) -> None:
        graph = CognitiveGraph()
        items = [
            {"name": "ok"},
            {"name": ""},            # empty -> invalid
            {"name": None},          # null  -> invalid
            {},                      # missing -> invalid
        ]
        out = graph.add_entities_batch([{"name": "ok"}] + items[1:])
        assert len(out["created"]) == 1
        assert len(out["skipped"]) == 3
        assert all(s["reason"] == "invalid" for s in out["skipped"])


class TestAddRelationsBatchHelper:
    def test_happy_path_4_relations(self) -> None:
        """4 relations all created (BE-API-005 AC)."""
        graph = CognitiveGraph()
        items = [
            {"source": "A", "target": "B", "relation": "related_to"},
            {"source": "B", "target": "C", "relation": "related_to"},
            {"source": "C", "target": "D", "relation": "related_to"},
            {"source": "D", "target": "E", "relation": "related_to"},
        ]
        out = graph.add_relations_batch(items)
        assert len(out["created"]) == 4
        assert out["skipped"] == []
        # All endpoints created as entities along the way.
        for name in ["A", "B", "C", "D", "E"]:
            assert graph.get_entity(name) is not None

    def test_invalid_relation_value_skipped(self) -> None:
        graph = CognitiveGraph()
        items = [
            {"source": "A", "target": "B", "relation": "related_to"},
            {"source": "B", "target": "C", "relation": "bogus"},
            {"source": "C", "target": "D"},  # missing relation
        ]
        out = graph.add_relations_batch(items)
        assert len(out["created"]) == 1
        assert len(out["skipped"]) == 2
        assert out["skipped"][0]["reason"] == "invalid_relation"
        assert out["skipped"][1]["reason"] == "invalid"


class TestDismissAlertsBatchHelper:
    async def test_happy_path_3_dismissed(self) -> None:
        """3 alerts all dismissed (BE-API-005 AC)."""
        repo = InMemoryDashboardRepository()
        ids = [
            await repo.add_alert(
                alert_type="high_risk",
                child_id="c1",
                parent_id="p1",
                payload={"x": "a"},
            ),
            await repo.add_alert(
                alert_type="high_risk",
                child_id="c1",
                parent_id="p1",
                payload={"x": "b"},
            ),
            await repo.add_alert(
                alert_type="high_risk",
                child_id="c1",
                parent_id="p1",
                payload={"x": "c"},
            ),
        ]
        service = DashboardService(repo=repo)
        out = await service.dismiss_alerts_batch(ids)
        assert out["dismissed"] == 3
        assert out["skipped"] == []

    async def test_one_missing(self) -> None:
        """2 dismissed + 1 skipped[reason='not_found'] (BE-API-005 AC)."""
        repo = InMemoryDashboardRepository()
        ids = [
            await repo.add_alert(
                alert_type="high_risk",
                child_id="c1",
                parent_id="p1",
                payload={"x": "a"},
            ),
            await repo.add_alert(
                alert_type="high_risk",
                child_id="c1",
                parent_id="p1",
                payload={"x": "b"},
            ),
            "does-not-exist",
        ]
        service = DashboardService(repo=repo)
        out = await service.dismiss_alerts_batch(ids)
        assert out["dismissed"] == 2
        assert len(out["skipped"]) == 1
        assert out["skipped"][0]["alert_id"] == "does-not-exist"
        assert out["skipped"][0]["reason"] == "not_found"


# ---------------------------------------------------------------------------
# HTTP / endpoint tests (integration)
# ---------------------------------------------------------------------------


class TestBatchEndpoints:
    """Hit the FastAPI endpoints via TestClient."""

    def test_entities_batch_happy_path(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/memory/graph/entities/batch",
            json={
                "items": [
                    {"name": "ba-A", "entity_type": "concept"},
                    {"name": "ba-B", "entity_type": "person"},
                    {"name": "ba-C", "entity_type": "place"},
                    {"name": "ba-D", "entity_type": "topic"},
                    {"name": "ba-E", "entity_type": "thing"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 5
        assert data["skipped"] == []
        assert {e["name"] for e in data["created"]} == {
            "ba-A", "ba-B", "ba-C", "ba-D", "ba-E",
        }

    def test_entities_batch_with_duplicate(
        self, client: TestClient,
    ) -> None:
        """Pre-call singular endpoint to seed, then batch with a dup."""
        # Seed
        client.post(
            "/api/v1/memory/graph/entities",
            json={"name": "dup-target"},
        )
        resp = client.post(
            "/api/v1/memory/graph/entities/batch",
            json={
                "items": [
                    {"name": "eb-1"},
                    {"name": "eb-2"},
                    {"name": "dup-target"},   # dup
                    {"name": "eb-3"},
                    {"name": "eb-4"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 4
        assert len(data["skipped"]) == 1
        assert data["skipped"][0]["name"] == "dup-target"
        assert data["skipped"][0]["reason"] == "duplicate"

    def test_relations_batch_happy_path(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/memory/graph/relations/batch",
            json={
                "items": [
                    {"source": "rb-A", "target": "rb-B",
                     "relation": "related_to"},
                    {"source": "rb-B", "target": "rb-C",
                     "relation": "related_to"},
                    {"source": "rb-C", "target": "rb-D",
                     "relation": "related_to"},
                    {"source": "rb-D", "target": "rb-E",
                     "relation": "related_to"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 4
        assert data["skipped"] == []

    def test_alerts_batch_dismiss_happy_path(
        self, client: TestClient,
    ) -> None:
        # Create 3 alerts via the existing record endpoints.
        ids: list[str] = []
        for text in ("a-1", "a-2", "a-3"):
            r = client.post(
                "/api/v1/dashboard/alerts/record/high-risk",
                json={"child_id": "child-001", "original_text": text},
            )
            assert r.status_code == 200
            ids.append(r.json()["id"])

        resp = client.post(
            "/api/v1/dashboard/alerts/batch/dismiss",
            json={"alert_ids": ids},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dismissed"] == 3
        assert data["skipped"] == []

    def test_alerts_batch_dismiss_with_missing(
        self, client: TestClient,
    ) -> None:
        """2 dismissed + 1 skipped[reason='not_found']."""
        # Create 2 real alerts.
        ids: list[str] = []
        for text in ("m-1", "m-2"):
            r = client.post(
                "/api/v1/dashboard/alerts/record/high-risk",
                json={"child_id": "child-001", "original_text": text},
            )
            ids.append(r.json()["id"])
        ids.append("really-truly-missing")

        resp = client.post(
            "/api/v1/dashboard/alerts/batch/dismiss",
            json={"alert_ids": ids},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dismissed"] == 2
        assert len(data["skipped"]) == 1
        assert data["skipped"][0]["alert_id"] == "really-truly-missing"
        assert data["skipped"][0]["reason"] == "not_found"

    # ---- 100-item cap ----

    def test_entities_batch_over_cap_returns_400(
        self, client: TestClient,
    ) -> None:
        too_many = [
            {"name": f"cap-e-{i}"} for i in range(BATCH_MAX_ITEMS + 1)
        ]
        resp = client.post(
            "/api/v1/memory/graph/entities/batch",
            json={"items": too_many},
        )
        assert resp.status_code == 400
        body = resp.json()
        # §6.3 shape — error.code lives in `detail.error`.
        assert body["error"]["code"] == "BATCH_TOO_LARGE"
        assert body["error"]["limit"] == BATCH_MAX_ITEMS
        assert body["error"]["received"] == BATCH_MAX_ITEMS + 1
        assert body["error"]["field"] == "items"

    def test_relations_batch_over_cap_returns_400(
        self, client: TestClient,
    ) -> None:
        too_many = [
            {"source": "x", "target": "y", "relation": "related_to"}
            for _ in range(BATCH_MAX_ITEMS + 1)
        ]
        resp = client.post(
            "/api/v1/memory/graph/relations/batch",
            json={"items": too_many},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "BATCH_TOO_LARGE"

    def test_alerts_batch_dismiss_over_cap_returns_400(
        self, client: TestClient,
    ) -> None:
        too_many = [
            f"alert-{i}" for i in range(BATCH_MAX_ITEMS + 1)
        ]
        resp = client.post(
            "/api/v1/dashboard/alerts/batch/dismiss",
            json={"alert_ids": too_many},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "BATCH_TOO_LARGE"
        assert body["error"]["field"] == "alert_ids"

    def test_entities_batch_at_cap_is_accepted(
        self, client: TestClient,
    ) -> None:
        """Boundary: exactly 100 items must succeed (not 400)."""
        boundary = [
            {"name": f"cap-ok-{i}"} for i in range(BATCH_MAX_ITEMS)
        ]
        resp = client.post(
            "/api/v1/memory/graph/entities/batch",
            json={"items": boundary},
        )
        assert resp.status_code == 200
        assert len(resp.json()["created"]) == BATCH_MAX_ITEMS
