"""Interest graph — PRD #25.

Shows the parent what topics the child has been curious about.
Designed to be '积极优先': counts, recent interest, suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class InterestTopic:
    """A single interest topic with metadata."""

    name: str
    count: int                       # how many times discussed
    last_seen: str                   # ISO date
    subtopics: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)  # sample questions

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "last_seen": self.last_seen,
            "subtopics": list(self.subtopics),
            "examples": list(self.examples),
        }


@dataclass
class InterestGraph:
    """A snapshot of the child's current interest graph."""

    child_id: str
    top_topics: list[InterestTopic]
    new_interests: list[InterestTopic]    # appeared this week
    growing_topics: list[str]             # topic names trending up
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "top_topics": [t.to_dict() for t in self.top_topics],
            "new_interests": [t.to_dict() for t in self.new_interests],
            "growing_topics": list(self.growing_topics),
            "generated_at": self.generated_at,
        }
