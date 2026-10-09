"""Tool permission matrix — PRD #54.

Public surface:
    ToolName            — enum of all available tools
    ToolMatrix          — per-agent permission map
    ToolAccess          — result of an access check
    ToolAccessChecker   — main checker with logging
    agent_type_to_matrix_key
                        — translator between agent `agent_type` and
                          matrix keys (BE-AGENT-001)
    matrix_key_to_agent_type
                        — inverse translator
    get_tool_schemas    — OpenAI-format tool schema resolver (BE-AGENT-001)
    dispatch_tool_call  — tool-call executor (BE-AGENT-001)
"""

from niki.tools.dispatcher import dispatch_tool_call
from niki.tools.mapping import (
    agent_type_to_matrix_key,
    matrix_key_to_agent_type,
)
from niki.tools.matrix import (
    ToolAccess,
    ToolAccessChecker,
    ToolMatrix,
    ToolName,
)
from niki.tools.schemas import get_tool_schemas

__all__ = [
    "ToolName",
    "ToolMatrix",
    "ToolAccess",
    "ToolAccessChecker",
    "agent_type_to_matrix_key",
    "matrix_key_to_agent_type",
    "get_tool_schemas",
    "dispatch_tool_call",
]
