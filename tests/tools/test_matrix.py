"""Tests for the tool permission matrix."""

import pytest

from niki.tools import ToolAccess, ToolAccessChecker, ToolMatrix, ToolName


@pytest.fixture
def matrix() -> ToolMatrix:
    return ToolMatrix()


class TestToolName:
    def test_all_tools_defined(self) -> None:
        names = {t.value for t in ToolName}
        assert "search_api" in names
        assert "calculator" in names
        assert "time_query" in names
        assert "weather_query" in names
        assert "image_recognition" in names


class TestToolMatrixDefault:
    def test_search_api_only_encyclopedia(self, matrix: ToolMatrix) -> None:
        assert matrix.can_access("encyclopedia_agent", ToolName.SEARCH_API)
        assert not matrix.can_access("tutoring_agent", ToolName.SEARCH_API)
        assert not matrix.can_access("emotional_agent", ToolName.SEARCH_API)

    def test_calculator_globally_removed(self, matrix: ToolMatrix) -> None:
        """PRD: calculator was globally removed (no agent has access)."""
        assert not matrix.can_access("encyclopedia_agent", ToolName.CALCULATOR)
        assert not matrix.can_access("tutoring_agent", ToolName.CALCULATOR)
        assert not matrix.can_access("emotional_agent", ToolName.CALCULATOR)

    def test_time_query_all_agents(self, matrix: ToolMatrix) -> None:
        assert matrix.can_access("encyclopedia_agent", ToolName.TIME_QUERY)
        assert matrix.can_access("tutoring_agent", ToolName.TIME_QUERY)
        assert matrix.can_access("emotional_agent", ToolName.TIME_QUERY)

    def test_weather_query_skips_tutoring(self, matrix: ToolMatrix) -> None:
        assert matrix.can_access("encyclopedia_agent", ToolName.WEATHER_QUERY)
        assert not matrix.can_access("tutoring_agent", ToolName.WEATHER_QUERY)
        assert matrix.can_access("emotional_agent", ToolName.WEATHER_QUERY)

    def test_image_recognition_tutoring_and_encyclopedia(self, matrix: ToolMatrix) -> None:
        assert matrix.can_access("encyclopedia_agent", ToolName.IMAGE_RECOGNITION)
        assert matrix.can_access("tutoring_agent", ToolName.IMAGE_RECOGNITION)
        assert not matrix.can_access("emotional_agent", ToolName.IMAGE_RECOGNITION)

    def test_tutoring_has_no_external_tools(self, matrix: ToolMatrix) -> None:
        """PRD #54: 辅导 Agent 零外部工具 — only internal tools."""
        external_tools = {
            ToolName.SEARCH_API,
            ToolName.CALCULATOR,
            ToolName.WEATHER_QUERY,
        }
        for tool in external_tools:
            if tool != ToolName.CALCULATOR:
                # SEARCH_API + WEATHER_QUERY should be unavailable to tutoring
                assert not matrix.can_access("tutoring_agent", tool)


class TestToolMatrixHelpers:
    def test_get_allowed_tools_encyclopedia(self, matrix: ToolMatrix) -> None:
        tools = matrix.get_allowed_tools("encyclopedia_agent")
        tool_names = {t.value for t in tools}
        assert "search_api" in tool_names
        assert "time_query" in tool_names
        assert "image_recognition" in tool_names

    def test_get_allowed_tools_tutoring(self, matrix: ToolMatrix) -> None:
        tools = matrix.get_allowed_tools("tutoring_agent")
        tool_names = {t.value for t in tools}
        # Tutoring has only internal tools + time_query + image_recognition
        assert "search_api" not in tool_names
        assert "weather_query" not in tool_names
        assert "time_query" in tool_names

    def test_get_agent_for_tool(self, matrix: ToolMatrix) -> None:
        agents = matrix.get_agent_for_tool(ToolName.SEARCH_API)
        assert agents == ["encyclopedia_agent"]


class TestToolMatrixMutation:
    def test_set_permission(self, matrix: ToolMatrix) -> None:
        matrix.set_permission(ToolName.SEARCH_API, {"tutoring_agent"})
        assert matrix.can_access("tutoring_agent", ToolName.SEARCH_API)
        assert not matrix.can_access("encyclopedia_agent", ToolName.SEARCH_API)

    def test_revoke(self, matrix: ToolMatrix) -> None:
        matrix.revoke(ToolName.SEARCH_API)
        assert not matrix.can_access("encyclopedia_agent", ToolName.SEARCH_API)

    def test_to_dict(self, matrix: ToolMatrix) -> None:
        d = matrix.to_dict()
        assert "search_api" in d
        assert "encyclopedia_agent" in d["search_api"]


class TestToolAccess:
    def test_to_dict(self) -> None:
        access = ToolAccess(
            agent="tutoring_agent",
            tool=ToolName.SEARCH_API,
            allowed=False,
            reason="not in allow-list",
        )
        d = access.to_dict()
        assert d["agent"] == "tutoring_agent"
        assert d["tool"] == "search_api"
        assert d["allowed"] is False
        assert d["reason"] == "not in allow-list"


class TestToolAccessChecker:
    @pytest.fixture
    def checker(self) -> ToolAccessChecker:
        return ToolAccessChecker()

    def test_check_allowed(self, checker: ToolAccessChecker) -> None:
        access = checker.check("encyclopedia_agent", ToolName.SEARCH_API)
        assert access.allowed

    def test_check_denied(self, checker: ToolAccessChecker) -> None:
        access = checker.check("tutoring_agent", ToolName.SEARCH_API)
        assert not access.allowed
        assert "NOT in allow-list" in access.reason

    def test_check_or_raise(self, checker: ToolAccessChecker) -> None:
        # Allowed → no raise
        checker.check_or_raise("encyclopedia_agent", ToolName.SEARCH_API)
        # Denied → raise
        with pytest.raises(PermissionError):
            checker.check_or_raise("tutoring_agent", ToolName.SEARCH_API)

    def test_audit_log(self, checker: ToolAccessChecker) -> None:
        checker.check("encyclopedia_agent", ToolName.SEARCH_API)
        checker.check("tutoring_agent", ToolName.SEARCH_API)
        checker.check("encyclopedia_agent", ToolName.TIME_QUERY)
        assert len(checker.checks) == 3

    def test_denied_attempts(self, checker: ToolAccessChecker) -> None:
        checker.check("encyclopedia_agent", ToolName.SEARCH_API)  # Allowed
        checker.check("tutoring_agent", ToolName.SEARCH_API)  # Denied
        checker.check("tutoring_agent", ToolName.WEATHER_QUERY)  # Denied
        denied = checker.get_denied_attempts()
        assert len(denied) == 2

    def test_calculator_checked_always_denied(self, checker: ToolAccessChecker) -> None:
        """PRD: calculator was globally removed."""
        for agent in ("encyclopedia_agent", "tutoring_agent", "emotional_agent"):
            access = checker.check(agent, ToolName.CALCULATOR)
            assert not access.allowed


class TestToolMatrixCustom:
    def test_custom_permissions_override_default(self) -> None:
        # Use set_permission (modifies default matrix)
        matrix = ToolMatrix()
        matrix.set_permission(ToolName.SEARCH_API, {"tutoring_agent"})
        assert matrix.can_access("tutoring_agent", ToolName.SEARCH_API)
        # Default for other tools still applies
        assert matrix.can_access("encyclopedia_agent", ToolName.TIME_QUERY)

    def test_custom_only_specified_tools(self) -> None:
        """Revoking a specific tool doesn't affect others."""
        matrix = ToolMatrix()
        matrix.revoke(ToolName.SEARCH_API)
        # SEARCH_API now denied to all
        assert not matrix.can_access("encyclopedia_agent", ToolName.SEARCH_API)
        # But other tools still have default permissions
        assert matrix.can_access("encyclopedia_agent", ToolName.TIME_QUERY)
