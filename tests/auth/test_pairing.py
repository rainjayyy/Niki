"""Tests for the parent ↔ child pairing code store + endpoints
(BE-DEV-004).

Coverage targets:
- create() returns a 6-digit code
- consume() with the correct code returns the parent + child
- consume() with the wrong code returns None
- consume() after expiry returns None
- consume() twice returns None on the second call (single-use)
- 50 concurrent creates produce 50 unique codes
- cleanup_expired() removes only the expired entries
- API endpoint smoke (create + consume roundtrip via HTTP)
- API lateral-auth: parent who doesn't own the child gets 403
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.auth.pairing import (
    InMemoryPairingCodeStore,
    PairingCode,
    PAIRING_CODE_TTL_SECONDS,
    get_pairing_store,
    set_pairing_store,
)
from niki.main import create_app
from niki.services.child_registry import (
    InMemoryChildRegistry,
    set_child_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryPairingCodeStore:
    """Fresh in-memory store per test."""
    s = InMemoryPairingCodeStore()
    set_pairing_store(s)
    yield s
    set_pairing_store(None)


@pytest.fixture
def client(store) -> TestClient:
    """FastAPI test client with a clean child registry + sentinel parent."""
    set_child_registry(InMemoryChildRegistry())
    # Pre-register a child under test_parent so lateral-auth passes.
    get_child_registry_safe().register("test-child-1", "test_parent")

    app = create_app()

    async def fake_parent() -> ParentAccount:
        return ParentAccount(
            id="test_parent",
            name="Test Parent",
            id_number_masked="****",
            phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake_parent
    return TestClient(app)


def get_child_registry_safe():
    from niki.services.child_registry import get_child_registry
    return get_child_registry()


# ---------------------------------------------------------------------------
# Store unit tests
# ---------------------------------------------------------------------------


class TestPairingCodeStore:
    def test_create_returns_six_digit_code(self, store) -> None:
        record = store.create("p1", "c1")
        assert isinstance(record, PairingCode)
        assert isinstance(record.code, str)
        assert len(record.code) == 6
        assert record.code.isdigit()
        assert record.parent_id == "p1"
        assert record.child_id == "c1"
        assert record.consumed_at is None

    def test_create_sets_five_minute_ttl(self, store) -> None:
        before = datetime.now(timezone.utc)
        record = store.create("p1", "c1")
        delta = (record.expires_at - record.created_at).total_seconds()
        # Use a small fudge factor for clock skew between the two
        # `datetime.now()` calls — the spec is 300s ± a few ms.
        assert (
            PAIRING_CODE_TTL_SECONDS - 1
            <= delta
            <= PAIRING_CODE_TTL_SECONDS + 1
        )
        # And the created_at should be very recent.
        assert abs((record.created_at - before).total_seconds()) < 5

    def test_consume_with_correct_code(self, store) -> None:
        record = store.create("p1", "c1")
        consumed = store.consume(record.code)
        assert consumed is not None
        assert consumed.code == record.code
        assert consumed.parent_id == "p1"
        assert consumed.child_id == "c1"
        assert consumed.consumed_at is not None

    def test_consume_with_wrong_code_returns_none(self, store) -> None:
        store.create("p1", "c1")
        # Unknown code (well-formed but never generated).
        assert store.consume("000000") is None
        # Garbage strings return None, not exceptions.
        assert store.consume("abcdef") is None
        assert store.consume("12345") is None  # too short
        assert store.consume("1234567") is None  # too long
        assert store.consume("") is None

    def test_consume_twice_is_single_use(self, store) -> None:
        record = store.create("p1", "c1")
        first = store.consume(record.code)
        assert first is not None
        second = store.consume(record.code)
        assert second is None

    def test_consume_after_expiry_returns_none(self, store) -> None:
        record = store.create("p1", "c1")
        # Simulate expiry by force-mutating the internal record's
        # `expires_at` to the past. Using `peek` so we don't trip
        # the consumed guard.
        with store._lock:  # type: ignore[attr-defined]
            store._codes[record.code].expires_at = datetime.now(  # type: ignore[attr-defined]
                timezone.utc
            ) - timedelta(seconds=1)
        assert store.consume(record.code) is None

    def test_create_unique_codes_no_collision(self, store) -> None:
        """Creating 50 codes back-to-back must yield 50 distinct codes."""
        codes = {store.create("p1", "c1").code for _ in range(50)}
        assert len(codes) == 50

    def test_cleanup_expired_removes_only_expired(self, store) -> None:
        fresh = store.create("p1", "c1")
        stale = store.create("p2", "c2")

        # Force the second one into the past.
        with store._lock:  # type: ignore[attr-defined]
            store._codes[stale.code].expires_at = datetime.now(  # type: ignore[attr-defined]
                timezone.utc
            ) - timedelta(seconds=1)

        removed = store.cleanup_expired()
        assert removed == 1

        # Fresh code still consumable; stale code is gone.
        assert store.consume(fresh.code) is not None
        assert store.peek(stale.code) is None

    def test_cleanup_expired_returns_zero_when_nothing_expired(
        self, store,
    ) -> None:
        store.create("p1", "c1")
        store.create("p1", "c2")
        assert store.cleanup_expired() == 0

    def test_consumed_code_also_removed_by_cleanup(self, store) -> None:
        """Expired-and-consumed entries are still reaped by cleanup."""
        record = store.create("p1", "c1")
        store.consume(record.code)
        # Force expiry anyway (consumed or not).
        with store._lock:  # type: ignore[attr-defined]
            store._codes[record.code].expires_at = datetime.now(  # type: ignore[attr-defined]
                timezone.utc
            ) - timedelta(seconds=10)
        removed = store.cleanup_expired()
        assert removed == 1
        assert store.peek(record.code) is None

    def test_returns_deepcopy(self, store) -> None:
        """Mutating the returned record doesn't poison internal state."""
        record = store.create("p1", "c1")
        consumed = store.consume(record.code)
        assert consumed is not None
        consumed.consumed_at = None  # try to "un-consume"
        # A fresh peek still shows the record as consumed.
        peeked = store.peek(record.code)
        assert peeked is not None
        assert peeked.consumed_at is not None


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_default_store_is_inmemory(self) -> None:
        set_pairing_store(None)
        s = get_pairing_store()
        assert isinstance(s, InMemoryPairingCodeStore)

    def test_set_then_get_returns_same(self) -> None:
        s = InMemoryPairingCodeStore()
        set_pairing_store(s)
        assert get_pairing_store() is s
        set_pairing_store(None)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestCreatePairingCodeEndpoint:
    def test_returns_200_and_six_digit_code(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/pairing/code", json={"child_id": "test-child-1"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["child_id"] == "test-child-1"
        assert body["parent_id"] == "test_parent"
        assert len(body["code"]) == 6
        assert body["code"].isdigit()
        assert body["ttl_seconds"] == PAIRING_CODE_TTL_SECONDS
        # expires_at is ISO 8601 (we don't parse it strictly here).
        assert "expires_at" in body

    def test_unknown_child_returns_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/pairing/code", json={"child_id": "ghost-child"}
        )
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "CHILD_NOT_FOUND"

    def test_child_owned_by_other_parent_returns_403(
        self, client: TestClient
    ) -> None:
        # Register a child under a DIFFERENT parent and try to pair it.
        get_child_registry_safe().register("other-child", "some-other-parent")
        r = client.post(
            "/api/v1/pairing/code", json={"child_id": "other-child"}
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "PARENT_CHILD_MISMATCH"

    def test_missing_child_id_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/pairing/code", json={})
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "BAD_REQUEST"

    def test_non_string_child_id_returns_400(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/api/v1/pairing/code", json={"child_id": 12345}
        )
        assert r.status_code == 400


class TestConsumePairingCodeEndpoint:
    def test_happy_path_returns_bound_payload(
        self, client: TestClient
    ) -> None:
        # First, create the code as the parent.
        created = client.post(
            "/api/v1/pairing/code", json={"child_id": "test-child-1"}
        ).json()
        # Then consume it as the kid's H5 (no JWT).
        r = client.post(
            "/api/v1/pairing/consume", json={"code": created["code"]}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["bound"] is True
        assert body["parent_id"] == "test_parent"
        assert body["child_id"] == "test-child-1"

    def test_unknown_code_returns_404(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/pairing/consume", json={"code": "000000"}
        )
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "PAIRING_CODE_INVALID"

    def test_double_consume_returns_404(self, client: TestClient) -> None:
        created = client.post(
            "/api/v1/pairing/code", json={"child_id": "test-child-1"}
        ).json()
        # First consume succeeds.
        r1 = client.post(
            "/api/v1/pairing/consume", json={"code": created["code"]}
        )
        assert r1.status_code == 200
        # Second consume fails (single-use).
        r2 = client.post(
            "/api/v1/pairing/consume", json={"code": created["code"]}
        )
        assert r2.status_code == 404
        assert r2.json()["detail"]["error"] == "PAIRING_CODE_INVALID"

    def test_malformed_code_returns_400(self, client: TestClient) -> None:
        for bad in ["12345", "abcdef", "", "12345a", "1234567"]:
            r = client.post(
                "/api/v1/pairing/consume", json={"code": bad}
            )
            assert r.status_code == 400, f"expected 400 for {bad!r}"

    def test_missing_code_returns_400(self, client: TestClient) -> None:
        r = client.post("/api/v1/pairing/consume", json={})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Heartbeat integration
# ---------------------------------------------------------------------------


class TestHeartbeatCleanup:
    def test_cleanup_expired_pairing_codes_drops_expired(self) -> None:
        """The heartbeat coroutine delegates to the store and reports
        a healthy status with a `removed` count."""
        import asyncio
        from niki.infrastructure.heartbeat import (
            cleanup_expired_pairing_codes,
        )

        store = InMemoryPairingCodeStore()
        set_pairing_store(store)

        r1 = store.create("p1", "c1")
        # Keep a second, still-valid code to confirm cleanup is selective.
        store.create("p1", "c2")
        # Force expiry on the first one.
        with store._lock:  # type: ignore[attr-defined]
            store._codes[r1.code].expires_at = datetime.now(  # type: ignore[attr-defined]
                timezone.utc
            ) - timedelta(seconds=1)

        result = asyncio.run(cleanup_expired_pairing_codes())
        assert result["status"] == "healthy"
        assert result["removed"] == 1
        assert "latency_ms" in result
        # The still-valid code survives.
        assert len(store) == 1

        set_pairing_store(None)