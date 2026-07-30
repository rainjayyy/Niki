"""Tests for the deletion-receipt PDF + endpoint (BE-COMP-005)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.compliance.deletion_receipt import (
    _canonical_text,
    _hash,
    render_deletion_receipt,
)
from niki.main import create_app
from datetime import datetime, timezone


PDF_MAGIC = b"%PDF-"


class TestCanonicalText:
    def test_canonical_format(self) -> None:
        s = _canonical_text(
            request_id="r-1",
            parent_id="p-1",
            requested_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            completed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        assert "request_id=r-1" in s
        assert "parent_id=p-1" in s
        assert "requested_at=2026-07-01T00:00:00+00:00" in s
        assert "completed_at=2026-07-31T00:00:00+00:00" in s

    def test_completed_at_pending_marker(self) -> None:
        s = _canonical_text(
            request_id="r-1",
            parent_id="p-1",
            requested_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            completed_at=None,
        )
        assert "completed_at=PENDING" in s


class TestRender:
    def test_returns_valid_pdf(self) -> None:
        pdf = render_deletion_receipt(
            request_id="r-1",
            parent_id="p-1",
            requested_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            completed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        assert pdf.startswith(PDF_MAGIC)
        assert b"%%EOF" in pdf[-32:]

    def test_includes_cjk_font(self) -> None:
        pdf = render_deletion_receipt(
            request_id="r-1",
            parent_id="p-1",
            requested_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            completed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        assert b"STSong-Light" in pdf


class TestHashDeterminism:
    def test_same_inputs_same_hash(self) -> None:
        ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
        a = _hash(_canonical_text("r-1", "p-1", ts, ts))
        b = _hash(_canonical_text("r-1", "p-1", ts, ts))
        assert a == b

    def test_different_inputs_different_hash(self) -> None:
        ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
        a = _hash(_canonical_text("r-1", "p-1", ts, ts))
        b = _hash(_canonical_text("r-2", "p-1", ts, ts))
        assert a != b


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
        id="p-1", name="T",
        id_number_masked="****", phone_masked="****",
    )
    return TestClient(app)


class TestReceiptEndpoint:
    def test_returns_pdf(self, client: TestClient) -> None:
        r = client.get("/api/v1/deletion/req-abc/receipt")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(PDF_MAGIC)
        cd = r.headers["content-disposition"]
        assert "attachment" in cd
        assert "req-abc" in cd

    def test_no_cache_header(self, client: TestClient) -> None:
        r = client.get("/api/v1/deletion/req-1/receipt")
        assert "no-store" in r.headers["cache-control"]

    def test_requires_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from niki.main import create_app
        from fastapi.testclient import TestClient
        # Disable the conftest fake-auth so get_current_parent
        # actually runs and rejects the unauthenticated request.
        monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
        app = create_app()
        client2 = TestClient(app)
        r = client2.get("/api/v1/deletion/req-1/receipt")
        assert r.status_code == 401
