# BE-AUTH-001-rollout-ext: lateral authorization guard

| Field | Value |
|------|------|
| **Priority** | red P0 (security hole, more urgent than P1) |
| **Domain** | AUTH |
| **Source** | §7 horizontal privilege escalation issue |
| **Workload** | 1 day |
| **Status** | in progress (2026-07-28) |
| **Depends on** | BE-AUTH-001 done, BE-AUTH-001-rollout done |
| **Follow-up** | none |

## Background

After BE-AUTH-001-rollout every protected endpoint has JWT auth, but
**no child-ownership check**:

- Parent A with a valid JWT calls `GET /api/v1/dashboard/alerts/child-B`
- dep `get_current_parent()` returns `parent-A`
- The endpoint uses `child_id="child-B"` to query the repo, with **no
  check** that `child-B` actually belongs to `parent-A`
- Net effect: **parent A can read any child's data** -> severe horizontal
  privilege escalation.

This is a more dangerous hole than missing auth entirely.

## Acceptance criteria

- AC-1: `niki/services/child_registry.py` - simple `ChildRegistry` storing
  `child_id -> parent_id` mapping.
- AC-2: `niki/auth/dependencies.py` adds `get_owned_child_id` - path
  `child_id` + current parent -> verify ownership -> 403
  `PARENT_CHILD_MISMATCH`.
- AC-3: All child_id endpoints (dashboard / memory / treehole /
  emotion / media) use the new dep.
- AC-4: Unit + integration tests >= 8.
- AC-5: Existing 1089 passed tests zero regression.

## Design

### 1. ChildRegistry

```python
class ChildRegistry(ABC):
    @abstractmethod
    def get_parent_of(self, child_id: str) -> str | None: ...
    @abstractmethod
    def register(self, child_id: str, parent_id: str) -> None: ...
```

### 2. get_owned_child_id dep

```python
async def get_owned_child_id(
    child_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> str:
    registered_parent = get_child_registry().get_parent_of(child_id)
    if registered_parent is None:
        raise HTTPException(404, "CHILD_NOT_FOUND")
    if registered_parent != parent.id:
        raise HTTPException(403, "PARENT_CHILD_MISMATCH")
    return child_id
```

### 3. Endpoint migration

From:
```python
@router.get("/alerts/{child_id}")
async def get_alerts(child_id: str, parent=Depends(get_current_parent)): ...
```

To:
```python
@router.get("/alerts/{child_id}")
async def get_alerts(
    child_id: Annotated[str, Depends(get_owned_child_id)],
    parent=Depends(get_current_parent),
): ...
```

### 4. Risks

| Risk | Mitigation |
|------|-----------|
| ChildRegistry empty -> 404 everywhere | Tests inject test children via `register_child` |
| PoC's `child_id` from path has no owner concept | Tests auto-register common test child_ids |
| Existing test clients have no child | Add fixture `child_registry` to conftest |

### 5. Not in this ticket

- Real `Child` Postgres persistence (separate ticket)
- Audit log for failed ownership checks

## Follow-up tickets

- BE-STOR-001-ext-E: Child ORM persistence + full ChildRepository
- BE-AUTH-001-rollout-audit: ownership-violation audit log
