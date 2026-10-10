"""Tests for the BE-MISC-002 feature flag service + config endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.api.config import _resolve_feature_flags
from niki.feature_flags import (
    FeatureFlagService,
    get_feature_flag_service,
    reset_feature_flag_service_for_tests,
    set_feature_flag_service,
)
from niki.main import create_app


# ---------------------------------------------------------------------------
# Unit tests on the FeatureFlagService directly
# ---------------------------------------------------------------------------


class TestFeatureFlagServiceUnit:
    def test_default_construction_is_empty(self) -> None:
        svc = FeatureFlagService()
        assert svc.all_flags() == {}
        assert svc.is_enabled("p1", "any_flag") is False

    def test_defaults_dict_loaded(self) -> None:
        svc = FeatureFlagService(defaults={"new_dashboard": True})
        assert svc.is_enabled("p1", "new_dashboard") is True
        assert svc.is_enabled("p2", "new_dashboard") is True

    def test_defaults_json_parsed(self) -> None:
        svc = FeatureFlagService(
            defaults_json='{"new_dashboard": true, "ocr_enabled": false}',
        )
        assert svc.is_enabled("p1", "new_dashboard") is True
        assert svc.is_enabled("p1", "ocr_enabled") is False

    def test_defaults_and_defaults_json_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError):
            FeatureFlagService(
                defaults={"x": True},
                defaults_json='{"x": true}',
            )

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError):
            FeatureFlagService(defaults_json="{not json}")

    def test_non_object_json_raises(self) -> None:
        with pytest.raises(ValueError):
            FeatureFlagService(defaults_json="[1, 2, 3]")

    def test_unknown_flag_defaults_to_false(self) -> None:
        svc = FeatureFlagService(defaults={"new_dashboard": True})
        # Unknown flag → conservative False (flag-off is the
        # safe answer for "is this experimental feature on?").
        assert svc.is_enabled("p1", "totally_unknown") is False

    def test_set_override_flips_runtime_value(self) -> None:
        svc = FeatureFlagService(defaults={"new_dashboard": True})
        assert svc.is_enabled("p1", "new_dashboard") is True
        svc.set_override("new_dashboard", False)
        assert svc.is_enabled("p1", "new_dashboard") is False
        svc.set_override("new_dashboard", True)
        assert svc.is_enabled("p1", "new_dashboard") is True

    def test_reset_returns_to_initial_defaults(self) -> None:
        svc = FeatureFlagService(defaults={"new_dashboard": True})
        svc.set_override("new_dashboard", False)
        svc.set_override("silence_alerts", True)
        svc.reset()
        assert svc.is_enabled("p1", "new_dashboard") is True
        assert svc.is_enabled("p1", "silence_alerts") is False

    def test_reset_with_new_defaults(self) -> None:
        svc = FeatureFlagService(defaults={"new_dashboard": True})
        svc.reset(defaults={"alpha": True, "beta": False})
        assert svc.is_enabled("p1", "alpha") is True
        assert svc.is_enabled("p1", "beta") is False
        # The old `new_dashboard` is gone after a reset with
        # new defaults.
        assert svc.is_enabled("p1", "new_dashboard") is False

    def test_parent_id_arg_accepted_but_unused_for_v1(self) -> None:
        """v1 ignores the parent_id. The seam is in place so
        the call sites don't change when A/B targeting ships."""
        svc = FeatureFlagService(defaults={"new_dashboard": True})
        assert svc.is_enabled("p_one", "new_dashboard") is True
        assert svc.is_enabled("p_two", "new_dashboard") is True
        assert svc.is_enabled(None, "new_dashboard") is True

    def test_non_bool_values_are_coerced_to_bool(self) -> None:
        svc = FeatureFlagService(defaults={"flag_a": 1, "flag_b": 0})
        assert svc.is_enabled("p1", "flag_a") is True
        assert svc.is_enabled("p1", "flag_b") is False


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


class TestFeatureFlagServiceSingleton:
    def teardown_method(self) -> None:
        # Always drop the cached singleton between tests so
        # nothing leaks across cases.
        reset_feature_flag_service_for_tests()

    def test_get_returns_a_service(self) -> None:
        svc = get_feature_flag_service()
        assert isinstance(svc, FeatureFlagService)

    def test_set_replaces_singleton(self) -> None:
        custom = FeatureFlagService(defaults={"injected": True})
        set_feature_flag_service(custom)
        assert get_feature_flag_service() is custom

    def test_reset_drops_singleton(self) -> None:
        get_feature_flag_service()
        reset_feature_flag_service_for_tests()
        # After reset, the next `get_*` rebuilds — we can verify
        # by setting a sentinel then resetting and rebuilding.
        set_feature_flag_service(FeatureFlagService(defaults={"x": True}))
        reset_feature_flag_service_for_tests()
        # Don't call get_* — that would build from settings.
        # Instead, just confirm the global is None.
        from niki import feature_flags as ff_module

        assert ff_module._service is None


# ---------------------------------------------------------------------------
# Resolution helper used by the config endpoint
# ---------------------------------------------------------------------------


class TestResolveFeatureFlags:
    def test_returns_sorted_dict_of_bools(self) -> None:
        svc = FeatureFlagService(
            defaults={"alpha": True, "beta": False, "gamma": True},
        )
        out = _resolve_feature_flags(svc)
        assert out == {"alpha": True, "beta": False, "gamma": True}
        # All values must be `bool` — never string / int — so
        # the JSON serialiser gives the frontend what it expects.
        for v in out.values():
            assert isinstance(v, bool)


# ---------------------------------------------------------------------------
# Config endpoint integration
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """Build an app with a known feature-flag fixture."""
    set_feature_flag_service(
        FeatureFlagService(
            defaults={
                "new_dashboard": True,
                "ocr_enabled": False,
                "sse_alerts": True,
            },
        )
    )
    return TestClient(create_app())


class TestConfigEndpointFeatureFlags:
    def test_config_returns_feature_flags_field(self, client: TestClient) -> None:
        r = client.get("/api/v1/config/client")
        assert r.status_code == 200
        body = r.json()
        assert "feature_flags" in body
        assert isinstance(body["feature_flags"], dict)

    def test_config_feature_flags_match_seeded_service(
        self,
        client: TestClient,
    ) -> None:
        r = client.get("/api/v1/config/client")
        body = r.json()
        flags = body["feature_flags"]
        assert flags["new_dashboard"] is True
        assert flags["ocr_enabled"] is False
        assert flags["sse_alerts"] is True

    def test_override_takes_effect_after_set(self, client: TestClient) -> None:
        """A runtime override on the service should be reflected
        by subsequent /config/client calls — important for ops
        tools that flip flags live."""
        # Flip one flag via the underlying service.
        service = get_feature_flag_service()
        service.set_override("new_dashboard", False)
        r = client.get("/api/v1/config/client")
        body = r.json()
        assert body["feature_flags"]["new_dashboard"] is False

    def test_empty_service_yields_empty_flags(self) -> None:
        set_feature_flag_service(FeatureFlagService())
        client = TestClient(create_app())
        r = client.get("/api/v1/config/client")
        body = r.json()
        assert body["feature_flags"] == {}
