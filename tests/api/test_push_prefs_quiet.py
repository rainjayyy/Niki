"""Tests for the BE-PUSH-004 quiet hours / DND / per-severity filter.

Covers:
  - AC-1: 6 new fields surface in GET /push/prefs
  - AC-2: PATCH /push/prefs accepts all 8 fields
  - AC-3: validation errors return §6.3 shape with the right code
  - AC-4: `is_quiet_now` handles the 4 contract cases
  - AC-5: `should_deliver` combines the three gates
  - AC-8: i18n keys exist in MESSAGES_ZH + MESSAGES_EN

Helpers (is_quiet_now, should_deliver) are unit-tested directly
without the HTTP layer so the wrap-around / DND logic is
exercised deterministically.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from niki.api.push import is_quiet_now, should_deliver
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.i18n.messages import MESSAGES_EN, MESSAGES_ZH
from niki.main import create_app


# A fixed "now" so wrap-around and DND tests are deterministic.
NOW = datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def client() -> TestClient:
    """TestClient with a deterministic parent id 'p-quiet'."""
    app = create_app()

    def fake():
        return ParentAccount(
            id="p-quiet",
            name="Quiet",
            id_number_masked="****",
            phone_masked="****",
        )

    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


# ---------------------------------------------------------------------------
# AC-1: new fields appear in GET /push/prefs
# ---------------------------------------------------------------------------


class TestDefaultsSurface:
    def test_get_prefs_returns_all_eight_fields(self, client: TestClient) -> None:
        r = client.get("/api/v1/push/prefs")
        assert r.status_code == 200
        body = r.json()
        # BE-PUSH-001 fields
        assert body["quiet_start"] is None
        assert body["quiet_end"] is None
        assert body["high_risk_only"] is False
        assert "apns" in body["enabled_channels"]
        # BE-PUSH-004 fields (the 4 new ones)
        assert body["dnd_enabled"] is False
        assert body["dnd_until"] is None
        # Default severity whitelist excludes `info`
        assert body["severities"] == ["warning", "critical"]
        # Default event-type mute is empty
        assert body["muted_event_types"] == []

    def test_get_prefs_returns_eight_keys(self, client: TestClient) -> None:
        body = client.get("/api/v1/push/prefs").json()
        expected = {
            "quiet_start",
            "quiet_end",
            "enabled_channels",
            "high_risk_only",
            "dnd_enabled",
            "dnd_until",
            "severities",
            "muted_event_types",
            "updated_at",
        }
        assert expected.issubset(body.keys())


# ---------------------------------------------------------------------------
# AC-2: PATCH accepts all 8 fields and returns full prefs
# ---------------------------------------------------------------------------


class TestPatchRoundTrip:
    def test_patch_dnd_enabled(self, client: TestClient) -> None:
        r = client.patch("/api/v1/push/prefs", json={"dnd_enabled": True})
        assert r.status_code == 200
        assert r.json()["dnd_enabled"] is True

    def test_patch_dnd_until_iso_string(self, client: TestClient) -> None:
        iso = "2027-01-01T00:00:00+00:00"
        r = client.patch("/api/v1/push/prefs", json={"dnd_until": iso})
        assert r.status_code == 200
        # Server normalises to ISO; the string round-trips.
        assert r.json()["dnd_until"].startswith("2027-01-01")

    def test_patch_dnd_until_null_is_noop(self, client: TestClient) -> None:
        # First set dnd_until to something concrete.
        client.patch(
            "/api/v1/push/prefs",
            json={"dnd_until": "2030-01-01T00:00:00+00:00"},
        )
        # `null` follows the partial-update convention: "leave
        # unchanged". Clearing isn't a use case BE-PUSH-004 ships
        # for (the dnd_until is self-clearing once expired).
        r = client.patch("/api/v1/push/prefs", json={"dnd_until": None})
        assert r.status_code == 200
        assert r.json()["dnd_until"] is not None

    def test_patch_severities(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"severities": ["info", "warning", "critical"]},
        )
        assert r.status_code == 200
        assert r.json()["severities"] == [
            "info",
            "warning",
            "critical",
        ]

    def test_patch_muted_event_types(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"muted_event_types": ["safety_block", "session_pause"]},
        )
        assert r.status_code == 200
        assert r.json()["muted_event_types"] == [
            "safety_block",
            "session_pause",
        ]

    def test_patch_all_eight_fields_at_once(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={
                "quiet_start": 22,
                "quiet_end": 7,
                "enabled_channels": ["apns", "fcm"],
                "high_risk_only": True,
                "dnd_enabled": False,
                "dnd_until": "2027-06-01T00:00:00+00:00",
                "severities": ["warning", "critical"],
                "muted_event_types": ["safety_block"],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["quiet_start"] == 22
        assert body["quiet_end"] == 7
        assert body["enabled_channels"] == ["apns", "fcm"]
        assert body["high_risk_only"] is True
        assert body["dnd_enabled"] is False
        assert body["dnd_until"].startswith("2027-06-01")
        assert body["severities"] == ["warning", "critical"]
        assert body["muted_event_types"] == ["safety_block"]

    def test_patch_partial_preserves_unrelated_fields(self, client: TestClient) -> None:
        # Set severity whitelist first
        client.patch(
            "/api/v1/push/prefs",
            json={"severities": ["info", "warning", "critical"]},
        )
        # Now patch only dnd_enabled — severity should survive.
        r = client.patch("/api/v1/push/prefs", json={"dnd_enabled": True})
        body = r.json()
        assert body["dnd_enabled"] is True
        assert body["severities"] == [
            "info",
            "warning",
            "critical",
        ]


# ---------------------------------------------------------------------------
# AC-3: validation errors return §6.3 shape with correct codes
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_severity_returns_400(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"severities": ["info", "panic"]},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_SEVERITY"

    def test_invalid_event_type_returns_400(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"muted_event_types": [""]},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_EVENT_TYPE"

    def test_event_type_must_be_list(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"muted_event_types": "safety_block"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_EVENT_TYPE"

    def test_severities_must_be_list(self, client: TestClient) -> None:
        r = client.patch("/api/v1/push/prefs", json={"severities": "critical"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_SEVERITY"

    def test_quiet_start_equal_quiet_end_rejected(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"quiet_start": 8, "quiet_end": 8},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_QUIET_HOUR"

    def test_dnd_until_naive_iso_rejected(self, client: TestClient) -> None:
        # No timezone offset → ambiguous → reject.
        r = client.patch(
            "/api/v1/push/prefs",
            json={"dnd_until": "2027-01-01T00:00:00"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_QUIET_HOUR"


# ---------------------------------------------------------------------------
# AC-4: is_quiet_now handles the 4 contract cases
# ---------------------------------------------------------------------------


class TestIsQuietNow:
    def test_no_quiet_set_returns_false(self) -> None:
        assert is_quiet_now({}, now=NOW) is False

    def test_within_quiet_window_returns_true(self) -> None:
        # Same-day window, hour 13 → 14. NOW is 14:30.
        prefs = {"quiet_start": 13, "quiet_end": 14}
        at_noon = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        at_thirteen_thirty = datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc)
        assert is_quiet_now(prefs, now=at_noon) is False
        assert is_quiet_now(prefs, now=at_thirteen_thirty) is True

    def test_wrap_around_window(self) -> None:
        # 22 → 7 covers 22:00-23:59 + 00:00-06:59.
        prefs = {"quiet_start": 22, "quiet_end": 7}
        at_5am = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc)
        at_23 = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
        at_15 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
        at_7 = datetime(2026, 7, 31, 7, 0, tzinfo=timezone.utc)
        assert is_quiet_now(prefs, now=at_5am) is True
        assert is_quiet_now(prefs, now=at_23) is True
        assert is_quiet_now(prefs, now=at_15) is False
        # Boundary: end hour itself is exclusive.
        assert is_quiet_now(prefs, now=at_7) is False

    def test_dnd_until_in_future_returns_true(self) -> None:
        prefs = {
            "dnd_enabled": True,
            "dnd_until": datetime(2099, 1, 1, tzinfo=timezone.utc),
        }
        assert is_quiet_now(prefs, now=NOW) is True

    def test_dnd_until_in_past_returns_false(self) -> None:
        prefs = {
            "dnd_enabled": True,
            "dnd_until": datetime(2020, 1, 1, tzinfo=timezone.utc),
        }
        assert is_quiet_now(prefs, now=NOW) is False

    def test_dnd_persistent_when_until_is_null(self) -> None:
        # dnd_enabled=True with no expiry → forever quiet.
        assert is_quiet_now({"dnd_enabled": True}, now=NOW) is True

    def test_dnd_overrides_quiet_window_release(self) -> None:
        # Even outside the quiet window, DND keeps it silent.
        prefs = {
            "dnd_enabled": True,
            "dnd_until": datetime(2099, 1, 1, tzinfo=timezone.utc),
            "quiet_start": 22,
            "quiet_end": 7,
        }
        # 14:30 — way outside the 22→7 window. DND still wins.
        assert is_quiet_now(prefs, now=NOW) is True

    def test_equal_quiet_hours_treated_as_disabled(self) -> None:
        prefs = {"quiet_start": 8, "quiet_end": 8}
        at_8_30 = datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc)
        assert is_quiet_now(prefs, now=at_8_30) is False


# ---------------------------------------------------------------------------
# AC-5: should_deliver combines all three gates
# ---------------------------------------------------------------------------


class TestShouldDeliver:
    def test_no_gates_blocks_nothing(self) -> None:
        assert should_deliver({}, "info", "x", now=NOW) is True

    def test_severity_not_in_whitelist_blocks(self) -> None:
        prefs = {"severities": ["warning", "critical"]}
        assert should_deliver(prefs, "info", "x", now=NOW) is False
        assert should_deliver(prefs, "warning", "x", now=NOW) is True
        assert should_deliver(prefs, "critical", "x", now=NOW) is True

    def test_event_type_in_muted_blocks(self) -> None:
        prefs = {"muted_event_types": ["safety_block"]}
        assert should_deliver(prefs, "warning", "safety_block", now=NOW) is False
        assert should_deliver(prefs, "warning", "session_pause", now=NOW) is True

    def test_quiet_window_blocks(self) -> None:
        prefs = {
            "quiet_start": 13,
            "quiet_end": 15,
        }
        # NOW is 14:30 → inside window.
        assert should_deliver(prefs, "critical", "x", now=NOW) is False
        # Move outside the window.
        at_noon = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        assert should_deliver(prefs, "critical", "x", now=at_noon) is True

    def test_empty_severity_whitelist_blocks_everything(self) -> None:
        # An explicit empty whitelist is "mute everything".
        prefs = {"severities": []}
        assert should_deliver(prefs, "critical", "x", now=NOW) is False

    def test_dnd_blocks_regardless_of_other_settings(self) -> None:
        prefs = {
            "dnd_enabled": True,
            "severities": ["info", "warning", "critical"],
            "muted_event_types": [],
        }
        assert should_deliver(prefs, "critical", "anything", now=NOW) is False

    def test_all_three_gates_combined(self) -> None:
        # severity NOT in whitelist AND event_type muted AND in
        # quiet window — should_deliver still returns False,
        # and we can't tell which gate fired. Move outside the
        # window with severity still blocked → still False.
        prefs = {
            "quiet_start": 13,
            "quiet_end": 15,
            "severities": ["critical"],
            "muted_event_types": ["session_pause"],
        }
        # Inside window, info severity → blocked by severity.
        assert should_deliver(prefs, "info", "session_pause", now=NOW) is False
        # Inside window, warning event_type — but warning not in
        # whitelist either, so still False.
        assert should_deliver(prefs, "warning", "session_pause", now=NOW) is False
        # Move outside the window; warning still not allowed → False.
        at_noon = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        assert should_deliver(prefs, "warning", "session_pause", now=at_noon) is False
        # critical + non-muted + outside window → True.
        assert should_deliver(prefs, "critical", "session_start", now=at_noon) is True


# ---------------------------------------------------------------------------
# AC-8: i18n keys exist in both catalogs
# ---------------------------------------------------------------------------


class TestI18nKeys:
    def test_invalid_severity_in_zh(self) -> None:
        assert "media.invalid_severity" in MESSAGES_ZH

    def test_invalid_event_type_in_zh(self) -> None:
        assert "media.invalid_event_type" in MESSAGES_ZH

    def test_invalid_severity_in_en(self) -> None:
        assert "media.invalid_severity" in MESSAGES_EN

    def test_invalid_event_type_in_en(self) -> None:
        assert "media.invalid_event_type" in MESSAGES_EN

    def test_error_response_translates_in_zh(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"severities": ["panic"]},
            headers={"Accept-Language": "zh-CN"},
        )
        assert r.status_code == 400
        # Chinese translation should be present (not the literal
        # key, not English).
        msg = r.json()["error"]["message"]
        assert "panic" not in msg
        assert msg != "media.invalid_severity"

    def test_error_response_translates_in_en(self, client: TestClient) -> None:
        r = client.patch(
            "/api/v1/push/prefs",
            json={"severities": ["panic"]},
            headers={"Accept-Language": "en-US"},
        )
        assert r.status_code == 400
        msg = r.json()["error"]["message"]
        assert "panic" not in msg
        assert msg != "media.invalid_severity"
