"""Tool permission matrix — PRD #54.

Per-agent tool access control. PRD specifies the following matrix:

  | Tool              | Encyclopedia | Tutoring | Emotional |
  |-------------------|--------------|----------|-----------|
  | Search API        | ✅           | ❌       | ❌        |
  | Calculator        | ❌ (globally removed) | ❌ | ❌ |
  | Time query        | ✅           | ✅       | ✅        |
  | Weather query     | ✅           | ❌       | ✅        |
  | Image recognition| ✅           | ✅       | ❌        |

Tutoring agent has NO external tools (PRD #54: 辅导 Agent 零外部工具 —
其唯一"工具"是引导策略库).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ToolName(StrEnum):
    """All tools available in the Niki system."""

    SEARCH_API = "search_api"               # Web search
    CALCULATOR = "calculator"              # Math calculator (globally removed)
    TIME_QUERY = "time_query"              # Current time lookup
    WEATHER_QUERY = "weather_query"        # Weather lookup
    IMAGE_RECOGNITION = "image_recognition"  # Vision API for photo understanding
    EMOTION_BASELINE = "emotion_baseline"  # Internal: emotion baseline read
    KNOWLEDGE_BASE = "knowledge_base"      # Internal: encyclopedia KB
    GUIDED_DISCOVERY = "guided_discovery"  # Internal: tutoring strategy library


# PRD #54 — the matrix (also see PRD #55 — tool permission matrix)
_DEFAULT_PERMISSIONS: dict[ToolName, set[str]] = {
    ToolName.SEARCH_API: {"encyclopedia_agent"},
    ToolName.CALCULATOR: set(),  # PRD: globally removed
    ToolName.TIME_QUERY: {
        "encyclopedia_agent", "tutoring_agent", "emotional_agent",
    },
    ToolName.WEATHER_QUERY: {"encyclopedia_agent", "emotional_agent"},
    ToolName.IMAGE_RECOGNITION: {"encyclopedia_agent", "tutoring_agent"},
    ToolName.EMOTION_BASELINE: {"emotional_agent"},
    ToolName.KNOWLEDGE_BASE: {"encyclopedia_agent"},
    ToolName.GUIDED_DISCOVERY: {"tutoring_agent"},
}


@dataclass(frozen=True)
class ToolAccess:
    """Result of a tool access check."""

    agent: str
    tool: ToolName
    allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "tool": self.tool.value,
            "allowed": self.allowed,
            "reason": self.reason,
        }


class ToolMatrix:
    """Per-agent tool access matrix.

    Used by the chat orchestrator to gate LLM tool calls.
    Even if the LLM 'wants' to call a tool, the orchestrator
    checks the matrix first (PRD: 降级不降安全).
    """

    def __init__(self, permissions: dict[ToolName, set[str]] | None = None) -> None:
        self._permissions = permissions or self._default_dict()

    @staticmethod
    def _default_dict() -> dict[ToolName, set[str]]:
        """Copy of the PRD default matrix."""
        return {k: set(v) for k, v in _DEFAULT_PERMISSIONS.items()}

    def can_access(self, agent: str, tool: ToolName) -> bool:
        """Check whether `agent` is allowed to use `tool`.

        Args:
            agent: Agent name (e.g. "tutoring_agent").
            tool: The tool the agent wants to use.

        Returns:
            True if access is allowed.
        """
        allowed_agents = self._permissions.get(tool, set())
        return agent in allowed_agents

    def get_allowed_tools(self, agent: str) -> list[ToolName]:
        """List all tools the agent can use."""
        return [
            tool
            for tool, agents in self._permissions.items()
            if agent in agents
        ]

    def get_agent_for_tool(self, tool: ToolName) -> list[str]:
        """List all agents that can use a given tool."""
        return list(self._permissions.get(tool, set()))

    def set_permission(
        self,
        tool: ToolName,
        agents: set[str],
    ) -> None:
        """Set the agents that can use a tool (overrides default)."""
        self._permissions[tool] = set(agents)

    def revoke(self, tool: ToolName) -> None:
        """Revoke all access to a tool."""
        self._permissions[tool] = set()

    def to_dict(self) -> dict[str, Any]:
        """Serialise the matrix."""
        return {
            tool.value: sorted(agents)
            for tool, agents in self._permissions.items()
        }


class ToolAccessChecker:
    """Higher-level access checker with logging.

    Used by the chat orchestrator to gate tool calls.
    Logs every check (allowed + denied) for audit.
    """

    def __init__(self, matrix: ToolMatrix | None = None) -> None:
        self._matrix = matrix or ToolMatrix()
        self._checks: list[ToolAccess] = []   # audit log

    @property
    def matrix(self) -> ToolMatrix:
        return self._matrix

    @property
    def checks(self) -> list[ToolAccess]:
        return list(self._checks)

    def check(self, agent: str, tool: ToolName) -> ToolAccess:
        """Check tool access and log the decision.

        Args:
            agent: Agent name.
            tool: The tool being requested.

        Returns:
            ToolAccess with allowed/reason.
        """
        allowed = self._matrix.can_access(agent, tool)

        if allowed:
            reason = f"agent '{agent}' is in allow-list for '{tool.value}'"
        else:
            reason = (
                f"agent '{agent}' is NOT in allow-list for '{tool.value}' "
                f"(PRD #54 matrix denial)"
            )

        access = ToolAccess(
            agent=agent,
            tool=tool,
            allowed=allowed,
            reason=reason,
        )
        self._checks.append(access)

        if allowed:
            logger.debug("tool.access.allowed", agent=agent, tool=tool.value)
        else:
            logger.warning("tool.access.denied", agent=agent, tool=tool.value)

        return access

    def check_or_raise(self, agent: str, tool: ToolName) -> None:
        """Check access; raise PermissionError if denied.

        Useful for hard enforcement.
        """
        access = self.check(agent, tool)
        if not access.allowed:
            raise PermissionError(access.reason)

    def get_denied_attempts(self) -> list[ToolAccess]:
        """Get all denied tool access attempts (for the audit log)."""
        return [c for c in self._checks if not c.allowed]
