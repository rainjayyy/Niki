"""Memory system — PRD #45-#51, #61-#64.

Public surface:
    ShortTermMemory       — Redis-backed recent conversation (10 rounds)
    LongTermMemory        — ChromaDB-backed vector store
    CognitiveGraph        — Neo4j entity-relation graph with time decay
    EntityAligner         — semantic deduplication (>0.85 cosine)
    DaytimeMemoryHook     — real-time high-value extraction
    NightlyPipeline       — scheduled deep extraction with retries + DLQ
    PipelineHealth        — health check endpoint data
"""

from niki.memory.aligner import EntityAligner
from niki.memory.graph import (
    CognitiveGraph,
    EntityNode,
    RelationEdge,
    RelationType,
)
from niki.memory.hook import (
    DaytimeMemoryHook,
    ExtractionCandidate,
    ExtractionKind,
)
from niki.memory.long_term import (
    LongTermMemory,
    MemoryRecord,
    MemorySource,
)
from niki.memory.pipeline import (
    NightlyPipeline,
    PipelineStatus,
    PipelineStepResult,
    StepStatus,
)
from niki.memory.short_term import ConversationTurn, ShortTermMemory

__all__ = [
    "ShortTermMemory",
    "ConversationTurn",
    "LongTermMemory",
    "MemoryRecord",
    "MemorySource",
    "CognitiveGraph",
    "EntityNode",
    "RelationEdge",
    "RelationType",
    "EntityAligner",
    "DaytimeMemoryHook",
    "ExtractionCandidate",
    "ExtractionKind",
    "NightlyPipeline",
    "PipelineStatus",
    "PipelineStepResult",
    "StepStatus",
]
