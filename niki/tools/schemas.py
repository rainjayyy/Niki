"""OpenAI-format tool schemas for the agent layer — BE-AGENT-001.

This module produces the ``tools=[...]`` payload that the agent
sends to the LLM. We only expose tools that:

1. Have a real implementation in :mod:`niki.tools.dispatcher`.
2. Are in the agent's allow-list per
   :class:`niki.tools.matrix.ToolMatrix`.

Tools in the matrix with **no implementation** (e.g. ``SEARCH_API``,
``WEATHER_QUERY``) are deliberately *not* exposed — surfacing them
would invite the LLM to hallucinate a call that the dispatcher
returns an ``"unknown tool"`` error for, burning tokens and degrading
the eval score.

Naming convention: ``<ToolName.value>.<verb>`` — e.g.
``knowledge_base.search``, ``time_query.now``. This keeps the JSON
schema self-describing and matches the dispatcher switch.
"""

from __future__ import annotations

from typing import Any

import structlog

from niki.tools.mapping import agent_type_to_matrix_key
from niki.tools.matrix import ToolAccessChecker, ToolMatrix, ToolName

logger = structlog.get_logger(__name__)


# Hardcoded schema registry. Keep names in lockstep with the
# dispatcher's switch in `niki/tools/dispatcher.py`.
_SCHEMAS: dict[str, dict[str, Any]] = {
    "knowledge_base.search": {
        "type": "function",
        "function": {
            "name": "knowledge_base.search",
            "description": (
                "Search the curated children's encyclopedia for facts "
                "matching the query. Returns a list of relevant snippets "
                "with relevance scores. Use this BEFORE answering any "
                "factual question to ground the response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The question or topic to search for, in the "
                            "child's own words when possible."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "Maximum number of hits to return (1-5). "
                            "Default 3."
                        ),
                        "minimum": 1,
                        "maximum": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    "time_query.now": {
        "type": "function",
        "function": {
            "name": "time_query.now",
            "description": (
                "Return the current server time as an ISO 8601 string. "
                "Use this when the child asks '现在几点了' / '今天星期几'."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    "image_recognition.caption": {
        "type": "function",
        "function": {
            "name": "image_recognition.caption",
            "description": (
                "Return the pre-computed caption for an attached image "
                "(set by the vision model upstream). The image has "
                "already been described — call this to retrieve the "
                "description before answering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_url": {
                        "type": "string",
                        "description": "The image URL the child attached.",
                    },
                },
                "required": ["image_url"],
            },
        },
    },
}


def get_tool_schemas(
    agent_type: str,
    *,
    checker: ToolAccessChecker | None = None,
) -> list[dict[str, Any]]:
    """Return the OpenAI-format tool schemas that ``agent_type`` is
    allowed to use.

    The returned list is filtered by the PRD #54 permission matrix —
    if a tool isn't in the agent's allow-list it will not appear here,
    even if its schema is registered. This keeps the LLM from
    requesting tools the agent can't execute.

    Args:
        agent_type: The short form, e.g. ``"encyclopedia"``.
        checker: Optional pre-built checker. If omitted a default
            :class:`ToolAccessChecker` is constructed. Pass the
            shared one when you have it, so the audit log is shared
            across the same request.

    Returns:
        A list of OpenAI tool schema dicts (each
        ``{"type": "function", "function": {...}}``). Empty list if
        the agent has no tool permissions.
    """
    try:
        matrix_key = agent_type_to_matrix_key(agent_type)
    except ValueError:
        logger.warning(
            "tools.schemas.unknown_agent_type",
            agent_type=agent_type,
        )
        return []

    own_checker = checker or ToolAccessChecker(ToolMatrix())
    allowed_tool_names = set(own_checker.matrix.get_allowed_tools(matrix_key))

    # Map each ToolName back to its canonical schema name.
    _TOOL_NAME_TO_SCHEMA = {
        ToolName.KNOWLEDGE_BASE: "knowledge_base.search",
        ToolName.TIME_QUERY: "time_query.now",
        ToolName.IMAGE_RECOGNITION: "image_recognition.caption",
    }

    schemas: list[dict[str, Any]] = []
    for tool_name in allowed_tool_names:
        schema_name = _TOOL_NAME_TO_SCHEMA.get(tool_name)
        if schema_name is None:
            # Tool is allowed by the matrix but has no registered
            # schema — log and skip. This is the path for SEARCH_API
            # / WEATHER_QUERY until they get implementations.
            logger.debug(
                "tools.schemas.no_implementation",
                agent=matrix_key,
                tool=tool_name.value,
            )
            continue
        schema = _SCHEMAS.get(schema_name)
        if schema is None:
            logger.warning(
                "tools.schemas.schema_missing",
                schema_name=schema_name,
            )
            continue
        schemas.append(schema)

    logger.debug(
        "tools.schemas.resolved",
        agent=matrix_key,
        count=len(schemas),
    )
    return schemas


__all__ = ["get_tool_schemas"]
