"""Tool permission matrix — PRD #54.

Public surface:
    ToolName            — enum of all available tools
    ToolMatrix          — per-agent permission map
    ToolAccess          — result of an access check
    ToolAccessChecker   — main checker with logging
"""

from niki.tools.matrix import (
    ToolAccess,
    ToolAccessChecker,
    ToolMatrix,
    ToolName,
)

__all__ = [
    "ToolName",
    "ToolMatrix",
    "ToolAccess",
    "ToolAccessChecker",
]
