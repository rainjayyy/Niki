"""Learning whitepaper — PRD #26.

Shows the parent what their child has been learning:
- New knowledge explored (encyclopedia queries)
- Hard problems attempted (tutoring safety-valve triggers)
- New vocabulary learned

Each item comes with an action suggestion (PRD '行动建议伴行').
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class NewVocabulary:
    """A new word/phrase the child learned this week."""

    word: str
    context: str                       # sample sentence / topic
    first_seen: str                    # ISO date

    def to_dict(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "context": self.context,
            "first_seen": self.first_seen,
        }


@dataclass
class LearningWhitepaper:
    """A weekly learning summary for the parent."""

    child_id: str
    new_knowledge_explored: list[str]     # encyclopedia topics
    hard_problems_attempted: list[str]   # tutoring safety-valve events
    new_vocabulary: list[NewVocabulary]
    action_suggestions: list[str]         # PRD: 行动建议伴行
    total_questions_asked: int = 0
    total_problems_solved: int = 0
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "new_knowledge_explored": list(self.new_knowledge_explored),
            "hard_problems_attempted": list(self.hard_problems_attempted),
            "new_vocabulary": [v.to_dict() for v in self.new_vocabulary],
            "action_suggestions": list(self.action_suggestions),
            "total_questions_asked": self.total_questions_asked,
            "total_problems_solved": self.total_problems_solved,
            "generated_at": self.generated_at,
        }
