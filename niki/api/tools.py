"""Tool permission API endpoints — PRD #54.

Demonstration endpoints for #14 vertical slice:
  GET  /tools/matrix                   — view the tool permission matrix
  GET  /tools/allowed/{agent}         — list tools an agent can use
  POST /tools/check                   — check access for an agent+tool pair
  POST /tools/revoke                  — revoke a tool (admin)
  POST /tools/grant                   — grant a tool to an agent (admin)
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from fastapi import Depends

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

from niki.tools import ToolAccessChecker, ToolMatrix, ToolName

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])

# Module-level singleton
_matrix = ToolMatrix()
_checker = ToolAccessChecker(_matrix)


@router.get("/matrix")
async def get_matrix(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Return the full tool permission matrix."""
    return {
        "matrix": _matrix.to_dict(),
        "tools": [t.value for t in ToolName],
    }


@router.get("/allowed/{agent}")
async def get_allowed_tools(agent: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """List all tools an agent can use."""
    tools = _matrix.get_allowed_tools(agent)
    return {
        "agent": agent,
        "tools": [t.value for t in tools],
    }


@router.post("/check")
async def check_access(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Check whether an agent can use a tool.

    Request body: {"agent": "tutoring_agent", "tool": "search_api"}
    """
    agent = payload.get("agent", "")
    tool_str = payload.get("tool", "")
    try:
        tool = ToolName(tool_str)
    except ValueError:
        return {
            "agent": agent,
            "tool": tool_str,
            "allowed": False,
            "reason": f"unknown tool: {tool_str}",
        }

    access = _checker.check(agent, tool)
    return access.to_dict()


@router.post("/revoke")
async def revoke_tool(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Revoke all access to a tool (admin)."""
    tool_str = payload.get("tool", "")
    try:
        tool = ToolName(tool_str)
    except ValueError:
        return {"error": f"unknown tool: {tool_str}"}

    _matrix.revoke(tool)
    logger.info("tool.revoked", tool=tool.value)
    return {"tool": tool.value, "revoked": True}


@router.post("/grant")
async def grant_tool(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Grant a tool to one or more agents (admin)."""
    tool_str = payload.get("tool", "")
    agents = payload.get("agents", [])
    if not isinstance(agents, list):
        return {"error": "agents must be a list"}

    try:
        tool = ToolName(tool_str)
    except ValueError:
        return {"error": f"unknown tool: {tool_str}"}

    _matrix.set_permission(tool, set(agents))
    logger.info("tool.granted", tool=tool.value, agents=agents)
    return {"tool": tool.value, "granted_to": agents}


@router.get("/audit")
async def get_audit(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get the access-check audit log (PRD: 降级不降安全)."""
    return {
        "total_checks": len(_checker.checks),
        "denied_count": len(_checker.get_denied_attempts()),
        "recent": [c.to_dict() for c in _checker.checks[-20:]],
    }
