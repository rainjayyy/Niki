"""Age grader — PRD #34.

Classifies concepts by age-appropriateness. The agent checks
the child's age against the concept's recommended age band.

PRD #34: 家长输入年龄，Niki自动适配内容难度和语言复杂度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ContentAge(IntEnum):
    """Age band for content (PRD: 3-5, 6-8, 9-12)."""

    TODDLER = 3
    YOUNG = 6
    MID = 9
    TEEN = 12


# Default mapping: child age → expected age band
AGE_BANDS: list[tuple[int, int, ContentAge]] = [
    (3, 5, ContentAge.TODDLER),
    (6, 8, ContentAge.YOUNG),
    (9, 12, ContentAge.MID),
    (13, 18, ContentAge.TEEN),
]


def age_to_band(age: int) -> ContentAge:
    """Map a child's age to the appropriate content age band."""
    for lo, hi, band in AGE_BANDS:
        if lo <= age <= hi:
            return band
    return ContentAge.MID  # Default


@dataclass
class ContentGrade:
    """Grade a concept's age-appropriateness.

    A concept is "appropriate" if its required age band matches the
    child's age band, or if it falls into a higher (more complex) band.
    """

    concept: str
    required_age: ContentAge
    child_age: ContentAge
    appropriate: bool
    complexity_delta: int  # positive = too complex, negative = too simple

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "required_age": int(self.required_age),
            "child_age": int(self.child_age),
            "appropriate": self.appropriate,
            "complexity_delta": self.complexity_delta,
        }


# Concept → recommended minimum age
# In production: this would be a much larger curated dictionary
CONCEPT_MIN_AGE: dict[str, ContentAge] = {
    # Toddler (3-5)
    "颜色": ContentAge.TODDLER,
    "动物": ContentAge.TODDLER,
    "数字": ContentAge.TODDLER,
    "恐龙": ContentAge.TODDLER,
    "月亮": ContentAge.TODDLER,
    "太阳": ContentAge.TODDLER,
    # Young (6-8)
    "乘法": ContentAge.YOUNG,
    "除法": ContentAge.YOUNG,
    "历史": ContentAge.YOUNG,
    "地理": ContentAge.YOUNG,
    "英语": ContentAge.YOUNG,
    # Mid (9-12)
    "分数": ContentAge.MID,
    "代数": ContentAge.MID,
    "物理": ContentAge.MID,
    "化学": ContentAge.MID,
    "细胞": ContentAge.MID,
    # Teen (13-18)
    "量子力学": ContentAge.TEEN,
    "基因编辑": ContentAge.TEEN,
    "微积分": ContentAge.TEEN,
    "哲学": ContentAge.TEEN,
    "政治": ContentAge.TEEN,
}


class AgeGrader:
    """Grades concepts by age-appropriateness for a given child age.

    PRD #34: language and content should adapt to the child's age.
    """

    def __init__(self, child_age: int) -> None:
        self.child_age = child_age
        self._child_band = age_to_band(child_age)

    @property
    def child_band(self) -> ContentAge:
        return self._child_band

    def grade(self, concept: str) -> ContentGrade:
        """Grade a single concept for this child.

        Args:
            concept: The concept or topic name.

        Returns:
            ContentGrade indicating appropriateness.
        """
        required = CONCEPT_MIN_AGE.get(concept, ContentAge.MID)
        # If concept not in dictionary, assume MID (default complexity)
        delta = int(required) - int(self._child_band)
        # Appropriate if not more than 1 age-band above
        appropriate = delta <= 1
        return ContentGrade(
            concept=concept,
            required_age=required,
            child_age=self._child_band,
            appropriate=appropriate,
            complexity_delta=delta,
        )

    def grade_text(self, text: str) -> list[ContentGrade]:
        """Grade multiple concepts mentioned in text.

        Checks the full text AND individual tokens against CONCEPT_MIN_AGE
        so multi-character concepts (e.g. "恐龙") are matched.
        """
        if not text:
            return []
        grades: list[ContentGrade] = []
        seen: set[str] = set()

        # 1. Check the full text against concept names
        #    (allows matching multi-character concepts)
        for concept in CONCEPT_MIN_AGE:
            if concept in text and concept not in seen:
                seen.add(concept)
                grades.append(self.grade(concept))

        # 2. Also check individual tokens (for any 1-char concepts in dict)
        tokens = re.findall(r"[一-鿿]|[a-zA-Z]+", text)
        for token in tokens:
            if token in CONCEPT_MIN_AGE and token not in seen:
                seen.add(token)
                grades.append(self.grade(token))

        return grades

    def is_appropriate(self, concept: str) -> bool:
        """Quick check: is this concept appropriate for the child?"""
        return self.grade(concept).appropriate

    def suggest_simpler(self, concept: str) -> str | None:
        """If concept is too advanced, suggest a simpler alternative.

        Returns None if no simpler alternative is known.
        """
        grade = self.grade(concept)
        if grade.appropriate or grade.complexity_delta <= 0:
            return None

        # Naive simplification: try to find a simpler concept in the dictionary
        # with the same first character
        first_char = concept[0] if concept else ""
        candidates = [
            (c, int(ca))
            for c, ca in CONCEPT_MIN_AGE.items()
            if c.startswith(first_char) and int(ca) < int(grade.required_age)
        ]
        if not candidates:
            return None
        # Pick the highest-band candidate (least simplified)
        candidates.sort(key=lambda x: -x[1])
        return candidates[0][0]
