# BE-STOR-001-ext-E: Child 持久化 (Postgres)

| Field | Value |
|------|------|
| Priority | P1 |
| Domain | STOR |
| Source | BE-AUTH-001-rollout-ext follow-up |
| Workload | 1 day |
| Status | in progress (2026-07-28) |
| Depends on | BE-STOR-001-ext-D, BE-AUTH-001-rollout-ext |
| Follow-up | none |

## Background

BE-AUTH-001-rollout-ext (lateral authorization guard) added a
`ChildRegistry` (in-memory dict) to verify parent-child ownership
at request time. This works for single-process PoC but does not
scale to multi-worker deployment: each worker has its own dict,
so worker A's "child X belongs to parent A" lookup is invisible
to worker B.

This ticket swaps the dict-backed registry for a Postgres-backed
repository that uses the existing `Child` ORM model.

## Acceptance criteria

- AC-1: `ChildRepository` ABC + `InMemoryChildRepository` (default)
  + `PostgresChildRepository` (production, uses `Child` ORM).
- AC-2: `ChildRegistry` is an adapter that delegates all calls to
  the repo. Same public API as before (`get_parent_of`, `register`,
  `unregister`, `__len__`).
- AC-3: When the process-wide repo is Postgres-backed, the lateral
  auth check works across worker processes.
- AC-4: New endpoint `POST /api/v1/parents/{parent_id}/children`
  registers a child. Lateral check: `parent_id` must match token.
- AC-5: Unit + integration tests >= 6.
- AC-6: Existing 1089 passed tests zero regression.

## Design

### 1. ChildRepository contract

```python
class ChildRepository(ABC):
    @abstractmethod
    def get_parent_of(self, child_id: str) -> Optional[str]: ...
    @abstractmethod
    def register(self, child_id: str, parent_id: str) -> None: ...
    @abstractmethod
    def list_children_of(self, parent_id: str) -> list[str]: ...
    @abstractmethod
    def delete(self, child_id: str) -> bool: ...
```

### 2. Implementation

- `InMemoryChildRepository` reuses the same dict semantics as
  `InMemoryChildRegistry` (now thin wrapper).
- `PostgresChildRepository` wraps the existing `Child` ORM model.
  Each child_id becomes a `Child` row keyed by id; get_parent_of
  reads `parent_id`.

### 3. Risks

| Risk | Mitigation |
|------|-----------|
| Existing tests use InMemoryChildRegistry signature | ChildRegistry keeps the same public API; underlying impl swaps |
| Child ORM has required fields (name) | New `register` accepts optional name; defaults to placeholder |
| Pre-existing Child rows in prod lack child_id collisions | UUID prevents collision |

### 4. Not in this ticket

- POST /children/{child_id} management endpoints (full CRUD)
- Child deletion cascades (PRD #35 account deletion)
- Child metadata (name, birth_date) UI flows
