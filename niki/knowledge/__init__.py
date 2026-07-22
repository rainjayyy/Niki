"""Knowledge base layer — three-level RAG + fact-checking middleware.

Public surface:
    KnowledgeBase   — three-level KB (L1 core, L2 expanded, L3 live search)
    KnowledgeHit    — single retrieval result
    KnowledgeSource — L1 | L2 | L3 enum
    FactChecker     — verifies claims against evidence (target ≥95% accuracy)
    FactCheckResult — confidence + reason
"""

from niki.knowledge.fact_checker import FactChecker, FactCheckResult
from niki.knowledge.knowledge_base import (
    KnowledgeBase,
    KnowledgeHit,
    KnowledgeSource,
)

__all__ = [
    "KnowledgeBase",
    "KnowledgeHit",
    "KnowledgeSource",
    "FactChecker",
    "FactCheckResult",
]
