"""Treehole mode activation detection — PRD #13.

The child asks for a private conversation; we route to empathy mode
and the original text is NEVER written to long-term memory.
"""

from __future__ import annotations

import re

# Triggers for treehole mode (PRD #13 + general secret-keeping phrases)
TREEHOLE_TRIGGERS: set[str] = {
    "秘密", "小秘密", "不要告诉", "别告诉", "只想跟你说", "只告诉你",
    "不想让", "保密", "treehole", "secret", "private",
}

# Regex patterns for more flexible matching
_TREEHOLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"这是.{0,4}秘密"),
    re.compile(r"不要告诉.{0,8}人"),
    re.compile(r"between\s+us", re.IGNORECASE),
    re.compile(r"don't\s+tell", re.IGNORECASE),
]


def should_activate_treehole(text: str) -> bool:
    """Detect if the child is asking for a private conversation.

    Args:
        text: The child's message.

    Returns:
        True if treehole mode should be activated.
    """
    if not text:
        return False

    text_lower = text.lower()

    # 1. Keyword match
    for trigger in TREEHOLE_TRIGGERS:
        if trigger.lower() in text_lower:
            return True

    # 2. Pattern match (Chinese / English)
    return any(pattern.search(text_lower) for pattern in _TREEHOLE_PATTERNS)
