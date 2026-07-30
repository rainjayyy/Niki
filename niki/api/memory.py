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
from fastapi import HTTPException
from fastapi import Request

from niki.api._i18n import localized_error_response
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

router = APIRouter(prefix="/memory", tags=[
    {"name": "memory", "description": "Memory system API endpoints — PRD #45-#51."}
])

# Module-level singletons
_short_term = ShortTermMemory()
_long_term = LongTermMemory()
_graph = CognitiveGraph()
_pipeline = NightlyPipeline(_long_term, _graph)
_hook = DaytimeMemoryHook(_long_term, _graph)

# BE-API-005: cap on items per batch request (memory + dashboard).
BATCH_MAX_ITEMS = 100


def _enforce_batch_cap(
    request: Request, items: list[Any] | None, *, field_name: str = "items"
) -> list[Any]:
    """Return `items` if within cap; raise 400 BATCH_TOO_LARGE otherwise.

    Shared by the 3 batch endpoints (entities / relations / alerts).
    A `None` payload is treated as an empty list so callers can
    distinguish "no items" (allowed) from "missing field" (caller
    is responsible for the req schema).
    """
    payload = items or []
    if len(payload) > BATCH_MAX_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=localized_error_response(
                request,
                code="BATCH_TOO_LARGE",
                key="api.batch_too_large",
                extra={
                    "limit": BATCH_MAX_ITEMS,
                    "received": len(payload),
                    "field": field_name,
                },
                fallback_message=(
                    f"batch {field_name} accepts at most "
                    f"{BATCH_MAX_ITEMS} items"
                ),
            ),
        )
    return payload


def _turn_to_dict(turn: ConversationTurn) -> dict[str, Any]:
    return {
        "role": turn.role,
        "text": turn.text,
        "timestamp": turn.timestamp.isoformat(),
        "intent": turn.intent,
        "metadata": turn.metadata,
    }


@router.get(
    "/short-term/{session_id}",
    summary="Get recent conversation turns for a session",
    description="Get recent conversation turns for a session.",
)
async def get_recent_turns(session_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get recent conversation turns for a session."""
    turns = await _short_term.get_recent(session_id)
    return {
        "session_id": session_id,
        "count": len(turns),
        "turns": [_turn_to_dict(t) for t in turns],
    }


@router.post(
    "/short-term/{session_id}",
    summary="Append a turn to the session's recent memory",
    description="Append a turn to the session's recent memory.",
)
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


@router.delete(
    "/short-term/{session_id}",
    summary="Clear a session's recent memory",
    description="Clear a session's recent memory.",
)
async def clear_session(session_id: str,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Clear a session's recent memory."""
    await _short_term.clear(session_id)
    return {"session_id": session_id, "cleared": True}


@router.post(
    "/long-term/add",
    summary="Add a memory record to long-term storage",
    description="Add a memory record to long-term storage.",
)
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


@router.post(
    "/long-term/search",
    summary="Semantic search over long-term memory",
    description="Semantic search over long-term memory.",
)
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


@router.get(
    "/graph/entities",
    summary="List entities in the cognitive graph (BE-API-003: paginated)",
    description="""
            List entities in the cognitive graph (BE-API-003: paginated).
            
            BE-API-004: ?keyword= case-insensitive substring over name +
            aliases; ?sort= picks `first_seen` (default), `last_seen`,
            or `name`; ?order= `asc` | `desc`. Since this endpoint does
            not store a created_at on entities, `since`/`until` are not
            applied — the helper still parses them so callers can use
            the same URL convention across endpoints, but they are
            silently ignored here (with a documented note).
            """,
)
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


@router.post(
    "/graph/entities",
    summary="Add an entity (with automatic alignment/dedup)",
    description="Add an entity (with automatic alignment/dedup).",
)
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


@router.post(
    "/graph/relations",
    summary="Add a relation between two entities",
    description="Add a relation between two entities.",
)
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


@router.post(
    "/graph/entities/batch",
    summary="Add many entities in a single round trip (BE-API-005)",
    description="""
            Add many entities in a single round trip (BE-API-005).
            
            Replaces the 5+ sequential POSTs the parent dashboard used to
            do to init a new child's cognitive graph (~200-500ms over
            mobile). Each item is independent; well-formed items go into
            `created`, malformed or duplicate names go into `skipped`
            with a `reason`. The 100-item cap is enforced up front so a
            pathological request can't tie up the server.
            
            Body:
                {"items": [{"name": "A", "entity_type": "concept",
                            "aliases": []}, ...]}
            Response:
                {"created":  [entity.to_dict(), ...],
                 "skipped":  [{"name": "...", "reason": "..."}, ...]}
            """,
)
async def add_entities_batch(request: Request, payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Add many entities in a single round trip (BE-API-005).

    Replaces the 5+ sequential POSTs the parent dashboard used to
    do to init a new child's cognitive graph (~200-500ms over
    mobile). Each item is independent; well-formed items go into
    `created`, malformed or duplicate names go into `skipped`
    with a `reason`. The 100-item cap is enforced up front so a
    pathological request can't tie up the server.

    Body:
        {"items": [{"name": "A", "entity_type": "concept",
                    "aliases": []}, ...]}
    Response:
        {"created":  [entity.to_dict(), ...],
         "skipped":  [{"name": "...", "reason": "..."}, ...]}
    """
    items = _enforce_batch_cap(
        request, payload.get("items"), field_name="items",
    )
    return _graph.add_entities_batch(items)


@router.post(
    "/graph/relations/batch",
    summary="Add many relations in a single round trip (BE-API-005)",
    description="""
            Add many relations in a single round trip (BE-API-005).
            
            Body:
                {"items": [{"source": "A", "target": "B",
                            "relation": "related_to"}, ...]}
            Response:
                {"created":  [edge.to_dict(), ...],
                 "skipped":  [{"source": "...", "reason": "..."}, ...]}
            """,
)
async def add_relations_batch(request: Request, payload: dict,
    parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Add many relations in a single round trip (BE-API-005).

    Body:
        {"items": [{"source": "A", "target": "B",
                    "relation": "related_to"}, ...]}
    Response:
        {"created":  [edge.to_dict(), ...],
         "skipped":  [{"source": "...", "reason": "..."}, ...]}
    """
    items = _enforce_batch_cap(
        request, payload.get("items"), field_name="items",
    )
    return _graph.add_relations_batch(items)


@router.post(
    "/graph/apply-decay",
    summary="Apply PRD #50 time decay to all relations",
    description="Apply PRD #50 time decay to all relations.",
)
async def apply_time_decay(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Apply PRD #50 time decay to all relations."""
    count = _graph.apply_decay()
    return {"decayed_edges": count}


@router.post(
    "/hook/process",
    summary="Run the daytime memory hook on a child message",
    description="Run the daytime memory hook on a child message.",
)
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


@router.get(
    "/pipeline/run",
    summary="Trigger a pipeline run (for testing/admin)",
    description="Trigger a pipeline run (for testing/admin).",
)
async def run_pipeline(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Trigger a pipeline run (for testing/admin)."""
    run = await _pipeline.run()
    return run.to_dict()


@router.get(
    "/pipeline/health",
    summary="Get pipeline health status (PRD #47)",
    description="Get pipeline health status (PRD #47).",
)
async def pipeline_health(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get pipeline health status (PRD #47)."""
    return _pipeline.health_check()


@router.get(
    "/pipeline/dead-letter",
    summary="Get the dead-letter queue contents (PRD #47)",
    description="Get the dead-letter queue contents (PRD #47).",
)
async def pipeline_dead_letter(parent: ParentAccount = Depends(get_current_parent),) -> dict:
    """Get the dead-letter queue contents (PRD #47)."""
    return {"items": _pipeline.get_dead_letter()}
