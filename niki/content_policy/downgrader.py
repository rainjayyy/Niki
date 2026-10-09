"""Content downgrader — applies age + blacklist rules.

Combines:
- FamilyBlacklist (per-family preferences, PRD #32)
- AgeGrader (age-appropriateness, PRD #34)

Produces a DowngradeDecision with:
- action: how to modify the response
- rewritten_text: optional alternative text
- notes: explanation for debugging
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from niki.content_policy.blacklist import (
    BlacklistAction,
    BlacklistMatch,
    FamilyBlacklist,
)
from niki.content_policy.grader import AgeGrader, ContentGrade

logger = structlog.get_logger(__name__)


class DowngradeAction(StrEnum):
    """How the response should be modified."""

    PASS = "pass"  # No change needed
    SOFTEN = "soften"  # Replace complex words with simpler ones
    REDIRECT = "redirect"  # Pivot to related safe content
    DEFLECT = "deflect"  # Politely decline + offer alternative
    SIMPLIFY = "simplify"  # Replace advanced concept with simpler one


@dataclass
class DowngradeDecision:
    """A single decision on how to handle a piece of content."""

    action: DowngradeAction
    original_text: str
    rewritten_text: str | None = None
    notes: list[str] = field(default_factory=list)
    blacklist_matches: list[BlacklistMatch] = field(default_factory=list)
    content_grades: list[ContentGrade] = field(default_factory=list)
    blocked_concept: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "original_text": self.original_text,
            "rewritten_text": self.rewritten_text,
            "notes": self.notes,
            "blocked_concept": self.blocked_concept,
            "blacklist_match_count": len(self.blacklist_matches),
            "content_grade_count": len(self.content_grades),
        }


# Default deflection responses
DEFLECT_RESPONSES: list[str] = [
    "嗯，Niki对这方面了解不多呢~ 我们换个话题聊聊吧！",
    "这个嘛 Niki还不太清楚~ 我们聊点别的怎么样？",
    "哦，Niki对这个不太了解呢~ 一起聊点别的好不好？",
]

# Simplification: complex term → simpler term
SIMPLIFICATIONS: dict[str, str] = {
    "量子力学": "光和物质",
    "基因编辑": "植物和动物的小秘密",
    "微积分": "数字游戏",
    "代数": "数字谜语",
    "分数": "切蛋糕",
    "细胞": "生命的小积木",
}


class ContentDowngrader:
    """Applies age + blacklist rules to produce a downgrade decision."""

    def __init__(
        self,
        blacklist: FamilyBlacklist,
        age_grader: AgeGrader,
    ) -> None:
        self._blacklist = blacklist
        self._age_grader = age_grader

    @property
    def blacklist(self) -> FamilyBlacklist:
        return self._blacklist

    @property
    def age_grader(self) -> AgeGrader:
        return self._age_grader

    def evaluate(self, text: str) -> DowngradeDecision:
        """Evaluate text for downgrades.

        Order of checks (most severe first):
        1. Blacklist match → action based on entry's BlacklistAction
        2. Age-inappropriate concept → SIMPLIFY or SOFTEN
        3. Otherwise → PASS
        """
        if not text:
            return DowngradeDecision(
                action=DowngradeAction.PASS,
                original_text=text,
                rewritten_text=None,
            )

        notes: list[str] = []

        # 1. Check blacklist
        bl_matches = self._blacklist.match(text)
        if bl_matches:
            return self._handle_blacklist(text, bl_matches, notes)

        # 2. Check age-appropriateness
        grades = self._age_grader.grade_text(text)
        too_hard = [g for g in grades if not g.appropriate]
        if too_hard:
            return self._handle_age(text, too_hard, notes)

        # 3. PASS
        return DowngradeDecision(
            action=DowngradeAction.PASS,
            original_text=text,
            rewritten_text=None,
            notes=notes,
            blacklist_matches=[],
            content_grades=grades,
        )

    def _handle_blacklist(
        self,
        text: str,
        matches: list[BlacklistMatch],
        notes: list[str],
    ) -> DowngradeDecision:
        """Handle a blacklist match.

        PRD #32: 降级处理而非生硬拒绝.
        """
        match = matches[0]  # First match wins
        notes.append(f"blacklist match: '{match.topic}' (action: {match.action.value})")
        blocked = match.topic

        if match.action == BlacklistAction.DOWNGRADE:
            action = DowngradeAction.DEFLECT
            rewritten = self._pick_deflection()
        elif match.action == BlacklistAction.REDIRECT:
            action = DowngradeAction.REDIRECT
            rewritten = self._redirect_for(blocked)
        else:  # APOLOGIZE
            action = DowngradeAction.DEFLECT
            rewritten = "不好意思，Niki不太了解这个话题呢~"

        return DowngradeDecision(
            action=action,
            original_text=text,
            rewritten_text=rewritten,
            notes=notes,
            blacklist_matches=matches,
            blocked_concept=blocked,
        )

    def _handle_age(
        self,
        text: str,
        too_hard: list[ContentGrade],
        notes: list[str],
    ) -> DowngradeDecision:
        """Handle age-inappropriate content.

        If we know a simpler alternative (SIMPLIFICATIONS), use it.
        Otherwise SOFTEN the response.
        """
        notes.append(
            f"{len(too_hard)} concept(s) too advanced: " + ", ".join(g.concept for g in too_hard)
        )

        # Try to find a simplification for the first too-hard concept
        for grade in too_hard:
            simpler = self._age_grader.suggest_simpler(grade.concept)
            if simpler:
                rewritten = text.replace(grade.concept, simpler)
                return DowngradeDecision(
                    action=DowngradeAction.SIMPLIFY,
                    original_text=text,
                    rewritten_text=rewritten,
                    notes=notes + [f"simplified '{grade.concept}' → '{simpler}'"],
                    content_grades=too_hard,
                )

            # Fall back to SIMPLIFICATIONS table
            if grade.concept in SIMPLIFICATIONS:
                rewritten = text.replace(grade.concept, SIMPLIFICATIONS[grade.concept])
                return DowngradeDecision(
                    action=DowngradeAction.SIMPLIFY,
                    original_text=text,
                    rewritten_text=rewritten,
                    notes=notes,
                    content_grades=too_hard,
                )

        # No known simplification — just SOFTEN
        return DowngradeDecision(
            action=DowngradeAction.SOFTEN,
            original_text=text,
            rewritten_text=None,
            notes=notes + ["content softened; no replacement available"],
            content_grades=too_hard,
        )

    def _pick_deflection(self) -> str:
        """Pick a random deflection message (deterministic for tests)."""
        import hashlib

        idx = int(hashlib.md5(b"deflect").hexdigest(), 16) % len(DEFLECT_RESPONSES)
        return DEFLECT_RESPONSES[idx]

    def _redirect_for(self, topic: str) -> str:
        """Suggest a redirect away from a blacklisted topic.

        The topic is NOT mentioned in the message (would reveal the
        blacklist to the child). The message just pivots to something else.
        """
        return "嗯~ Niki对这方面了解不多呢~ 我们聊点别的好不好？比如你今天想做什么？"
