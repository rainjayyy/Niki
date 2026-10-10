"""Mapping between agent `agent_type` strings and `ToolMatrix` keys — BE-AGENT-001.

Background:
    `BaseAgent.agent_type` (and its subclasses) are short forms —
    ``"encyclopedia"``, ``"tutoring"``, ``"emotion"`` — but the
    `ToolMatrix` permission dict in
    ``niki/tools/matrix.py:42-55`` uses the longer ``"_agent"``
    suffix, with an irregular form for emotion
    (``"emotional_agent"``, not ``"emotion_agent"``).

    Passing the short form straight to
    ``ToolAccessChecker.check(agent, tool)`` results in a silent
    denial for *every* tool call — because the matrix looks up the
    short form and finds no matching entry.

    This module centralises the translation so the orchestrator /
    EncyclopediaAgent can use the consistent short ``agent_type``
    everywhere and only this single helper knows about the matrix
    naming convention.

Forward direction (agent → matrix key) is the hot path; the inverse
(matrix key → agent type) is used by the audit log reader to
classify historical entries.
"""

from __future__ import annotations

# Short form (`agent_type`) → matrix key (`_DEFAULT_PERMISSIONS` dict).
# Note the irregular "emotion" → "emotional_agent" mapping — keep this
# in sync with `_DEFAULT_PERMISSIONS` in `niki/tools/matrix.py:42-55`.
_AGENT_TYPE_TO_MATRIX_KEY: dict[str, str] = {
    "encyclopedia": "encyclopedia_agent",
    "tutoring": "tutoring_agent",
    "emotion": "emotional_agent",
}

# Inverse lookup, built once at import — small dict, no caching needed.
_MATRIX_KEY_TO_AGENT_TYPE: dict[str, str] = {v: k for k, v in _AGENT_TYPE_TO_MATRIX_KEY.items()}


def agent_type_to_matrix_key(agent_type: str) -> str:
    """Translate ``agent_type`` to the matrix key used in
    ``ToolMatrix._DEFAULT_PERMISSIONS``.

    Args:
        agent_type: The short form, e.g. ``"encyclopedia"``.

    Returns:
        The matching matrix key, e.g. ``"encyclopedia_agent"``.

    Raises:
        ValueError: If ``agent_type`` is not one of the known agents.
            The agent layer should treat this as a programming error,
            not a runtime fallback.
    """
    try:
        return _AGENT_TYPE_TO_MATRIX_KEY[agent_type]
    except KeyError as exc:
        raise ValueError(
            f"Unknown agent_type {agent_type!r}; known types: {sorted(_AGENT_TYPE_TO_MATRIX_KEY)}"
        ) from exc


def matrix_key_to_agent_type(matrix_key: str) -> str:
    """Inverse of :func:`agent_type_to_matrix_key`.

    Args:
        matrix_key: The matrix key, e.g. ``"encyclopedia_agent"``.

    Returns:
        The short form, e.g. ``"encyclopedia"``.

    Raises:
        ValueError: If ``matrix_key`` is not a known key.
    """
    try:
        return _MATRIX_KEY_TO_AGENT_TYPE[matrix_key]
    except KeyError as exc:
        raise ValueError(
            f"Unknown matrix key {matrix_key!r}; known keys: {sorted(_MATRIX_KEY_TO_AGENT_TYPE)}"
        ) from exc


__all__ = [
    "agent_type_to_matrix_key",
    "matrix_key_to_agent_type",
]
