"""Memory system API endpoints — PRD #45-#51.

Demonstration endpoints for the #9 vertical slice:
  GET  /memory/short-term/{session_id}         — recent turns
  POST /memory/short-term/{session_id}         — append a turn
  DELETE /memory/short-term/{session_id}       — clear session
  POST /memory/long-term/add                  — add a memory record
  POST /memory/long-term/search               — semantic search
  GET  /memory/graph/entities                 — list entities
  POST /memory/graph/entities                 — add/align an entity
  POST /memory/graph/relations                — add a relation
  GET  /memory/pipeline/run                   — trigger pipeline (test)
  GET  /memory/pipeline/health                — pipeline health check
  GET  /memory/pipeline/dead-letter           — DLQ contents
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi import Depends

from niki.api._pagination import (
    ListQueryParams,
    list_query_params,
    paginate,
    parse_pagination,
)
from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

from niki.memory import (
    CognitiveGraph,
    ConversationTurn,
    DaytimeMemoryHook,
    EntityNode,
    LongTermMemory,
    MemoryRecord,
    MemorySource,
    NightlyPipeline,
    RelationType,
    ShortTermMemory,
)

router = APIRouter(prefix="/memory", tags=["memory"])

# Module-level singletons
_short_term = ShortTermMemory()
_long_term = LongTermMemory()
_graph = CognitiveGraph()
_pipeline = NightlyPipeline(_long_term, _graph)
_hook = DaytimeMemoryHook(_long_term, _graph)


def _turn_to_dict(turn: ConversationTurn) -> dict[str, Any]:
    return {
        "role": turn.role,
        "text": turn.text,
        "timestamp": turn.timestamp.isoformat(),
        "intent": turn.intent,
        "metadata": turn.metadata,
    }


@router.get("/short-term/{session_id}")
async def get_recent_turns(session_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get recent conversation turns for a session."""
    turns = await _short_term.get_recent(session_id)
    return {
        "session_id": session_id,
        "count": len(turns),
        "turns": [_turn_to_dict(t) for t in turns],
    }


@router.post("/short-term/{session_id}")
async def append_turn(session_id: str, payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Append a turn to the session's recent memory."""
    turn = ConversationTurn(
        role=payload.get("role", "child"),
        text=payload.get("text", ""),
        intent=payload.get("intent"),
        metadata=payload.get("metadata", {}),
    )
    turns = await _short_term.append(session_id, turn)
    return {
        "session_id": session_id,
        "count": len(turns),
        "turns": [_turn_to_dict(t) for t in turns],
    }


@router.delete("/short-term/{session_id}")
async def clear_session(session_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Clear a session's recent memory."""
    await _short_term.clear(session_id)
    return {"session_id": session_id, "cleared": True}


@router.post("/long-term/add")
async def add_long_term_record(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Add a memory record to long-term storage."""
    record = MemoryRecord(
        id=payload.get("id", ""),
        content=payload["content"],
        source=MemorySource(payload.get("source", "daytime_hook")),
        child_id=payload["child_id"],
        emotion=payload.get("emotion"),
        intensity=payload.get("intensity"),
        topic=payload.get("topic"),
        metadata=payload.get("metadata", {}),
    )
    if not record.id:
        record.id = LongTermMemory.make_id(
            record.child_id, record.content, record.source.value
        )
    record_id = await _long_term.add(record)
    return {"id": record_id, "stored": True}


@router.post("/long-term/search")
async def search_long_term(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Semantic search over long-term memory."""
    records = await _long_term.search(
        query=payload["query"],
        child_id=payload.get("child_id"),
        n_results=int(payload.get("n_results", 5)),
    )
    return {
        "query": payload["query"],
        "count": len(records),
        "records": [r.to_dict() for r in records],
    }


@router.get("/graph/entities")
async def list_entities(
    parent: ParentAccount = Depends(get_current_parent),
    pagination: tuple[int, int] = Depends(parse_pagination),
    params: ListQueryParams = Depends(list_query_params),
) -> dict:
    """List entities in the cognitive graph (BE-API-003: paginated).

    BE-API-004: ?keyword= case-insensitive substring over name +
    aliases; ?sort= picks `first_seen` (default), `last_seen`,
    or `name`; ?order= `asc` | `desc`. Since this endpoint does
    not store a created_at on entities, `since`/`until` are not
    applied — the helper still parses them so callers can use
    the same URL convention across endpoints, but they are
    silently ignored here (with a documented note).
    """
    page, page_size = pagination
    entities = _graph.list_entities(
        sort=params.sort,
        order=params.order,
        keyword=params.keyword,
    )
    payload = paginate(
        [e.to_dict() for e in entities],
        page,
        page_size,
        items_key="entities",
    )
    # Back-compat: legacy `count` field still present.
    payload["count"] = payload["total"]
    return payload


@router.post("/graph/entities")
async def add_entity(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Add an entity (with automatic alignment/dedup)."""
    entity = _hook._aligner.align(
        EntityNode(
            name=payload["name"],
            entity_type=payload.get("entity_type", "concept"),
            aliases=payload.get("aliases", []),
        )
    )
    return {"entity": entity.to_dict(), "aligned": entity.name == payload["name"]}


@router.post("/graph/relations")
async def add_relation(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Add a relation between two entities."""
    edge = _graph.add_relation(
        source=payload["source"],
        target=payload["target"],
        relation=RelationType(payload["relation"]),
        weight_delta=float(payload.get("weight_delta", 0.1)),
    )
    return {"edge": edge.to_dict()}


@router.post("/graph/apply-decay")
async def apply_time_decay(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Apply PRD #50 time decay to all relations."""
    count = _graph.apply_decay()
    return {"decayed_edges": count}


@router.post("/hook/process")
async def hook_process(payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Run the daytime memory hook on a child message."""
    candidates = await _hook.process(
        child_id=payload["child_id"],
        text=payload["text"],
        context=payload.get("context"),
    )
    return {
        "extracted_count": len(candidates),
        "candidates": [
            {
                "kind": c.kind.value,
                "text": c.text,
                "confidence": c.confidence,
            }
            for c in candidates
        ],
    }


@router.get("/pipeline/run")
async def run_pipeline(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Trigger a pipeline run (for testing/admin)."""
    run = await _pipeline.run()
    return run.to_dict()


@router.get("/pipeline/health")
async def pipeline_health(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get pipeline health status (PRD #47)."""
    return _pipeline.health_check()


@router.get("/pipeline/dead-letter")
async def pipeline_dead_letter(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get the dead-letter queue contents (PRD #47)."""
    return {"items": _pipeline.get_dead_letter()}
