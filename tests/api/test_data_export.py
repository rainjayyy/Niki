"""Tests for the data export endpoint (BE-COMP-001)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.auth.jwt_service import issue_access_token
from niki.db.repositories import (
    InMemoryDashboardRepository,
    InMemoryEmotionBaselineRepository,
    InMemoryTreeholeRepository,
    set_dashboard_repository,
    set_emotion_baseline_repository,
    set_treehole_repository,
)
from niki.main import create_app


@pytest.fixture
def client_with_test_parent(monkeypatch):
    """Test client with get_current_parent overridden to return
    a parent with id='test_parent' (the value other tests use)."""
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    set_dashboard_repository(InMemoryDashboardRepository())
    set_treehole_repository(InMemoryTreeholeRepository())
    set_emotion_baseline_repository(InMemoryEmotionBaselineRepository())

    def _fake_current_parent():
        return ParentAccount(
            id="test_parent",
            name="Test Parent",
            id_number_masked="****",
            phone_masked="****",
        )

    app = create_app()
    app.dependency_overrides[get_current_parent] = _fake_current_parent
    return TestClient(app)


class TestDataExport:
    def test_returns_zip_for_matching_parent(
        self, client_with_test_parent
    ) -> None:
        tok = issue_access_token("test_parent", ttl_seconds=60)
        r = client_with_test_parent.get(
            "/api/v1/parents/test_parent/data-export",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["X-Export-Record-Count"] is not None

    def test_zip_contains_required_files(
        self, client_with_test_parent
    ) -> None:
        tok = issue_access_token("test_parent", ttl_seconds=60)
        r = client_with_test_parent.get(
            "/api/v1/parents/test_parent/data-export",
            headers={"Authorization": f"Bearer {tok}"},
        )
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        # Required files
        assert "README.txt" in names
        assert "manifest.json" in names
        assert "account.json" in names
        assert "alerts.json" in names
        assert "tutoring.json" in names
        assert "knowledge.json" in names
        assert "treehole.json" in names
        assert "emotion_baseline.json" in names
        assert "children.json" in names

    def test_zip_manifest_contains_compliance_marker(
        self, client_with_test_parent
    ) -> None:
        tok = issue_access_token("test_parent", ttl_seconds=60)
        r = client_with_test_parent.get(
            "/api/v1/parents/test_parent/data-export",
            headers={"Authorization": f"Bearer {tok}"},
        )
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        manifest = json.loads(zf.read("manifest.json"))
        assert "PIPL" in manifest["policy"] or "GDPR" in manifest["policy"]
        assert manifest["parent_id"] == "test_parent"

    def test_returns_403_on_parent_mismatch(
        self, client_with_test_parent
    ) -> None:
        # Token says test_parent, but path says a different id.
        tok = issue_access_token("test_parent", ttl_seconds=60)
        r = client_with_test_parent.get(
            "/api/v1/parents/other_parent/data-export",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 403
        body = r.json()
        assert body["detail"]["error"] == "PARENT_MISMATCH"

    def test_returns_401_without_token(self) -> None:
        # No dep override; expect the real JWT path to deny an anonymous request.
        import os
        os.environ.pop("NIKI_TEST_FAKE_AUTH", None)
        try:
            from niki.main import create_app
            from fastapi.testclient import TestClient
            app = create_app()
            client = TestClient(app)
            r = client.get("/api/v1/parents/test_parent/data-export")
            assert r.status_code == 401
        finally:
            os.environ["NIKI_TEST_FAKE_AUTH"] = "1"

    def test_child_id_filter_produces_filtered_export(
        self, client_with_test_parent, monkeypatch
    ) -> None:
        # Pre-populate with 2 alerts for child-A and 1 for child-B.
        from niki.db.repositories import get_dashboard_repository
        repo = get_dashboard_repository()

        async def _seed():
            await repo.add_alert(
                alert_type="safety_valve",
                child_id="child-A", parent_id="test_parent",
                payload={"x": 1},
            )
            await repo.add_alert(
                alert_type="safety_valve",
                child_id="child-A", parent_id="test_parent",
                payload={"x": 2},
            )
            await repo.add_alert(
                alert_type="safety_valve",
                child_id="child-B", parent_id="test_parent",
                payload={"x": 3},
            )
        asyncio_run = __import__("asyncio").run
        asyncio_run(_seed())

        tok = issue_access_token("test_parent", ttl_seconds=60)
        r = client_with_test_parent.get(
            "/api/v1/parents/test_parent/data-export?child_id=child-A",
            headers={"Authorization": f"Bearer {tok}"},
        )
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        alerts = json.loads(zf.read("alerts.json"))
        assert alerts["record_count"] == 2
        assert all(a["child_id"] == "child-A" for a in alerts["items"])

