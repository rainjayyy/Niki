# BE-STORG-001-ext-B：TreeholeManager 持久化

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | STOR |
| **来源** | `docs/frontend-integration.md` §7.4 P1 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-STOR-001 ✅ |
| **后续** | BE-STORG-001-ext-C（DashboardService 持久化，同模板） |

## 背景

§7.4 关键路径 P1：

> BE-STORG-001-ext-B | TreeholeManager 仍用内存 dict，跨进程丢失情绪数据 | 1 天

实际痛点：
- 多实例部署 → 不同 worker 的 `TreeholeManager` 内存 dict 完全独立，告警聚合时**看到的是子集**
- Worker crash → 所有情绪摘要丢失，PRD #64 规定的聚合匿名化窗口没了
- 测试环境跨进程 → 状态丢失

本工单按 BE-STOR-001-ext-A 的模板，把 `TreeholeManager` 接到 Postgres。

## 验收标准（AC）

- [ ] AC-1：`TreeholeRepository` ABC + `PostgresTreeholeRepository` + `InMemoryTreeholeRepository` 默认实现
- [ ] AC-2：`TreeholeManager` 接受 repo（默认内存），所有方法走 repo
- [ ] AC-3：`TreeholePurger` 同样接入 repo
- [ ] AC-4：新增 ORM model `TreeholeEngagement`（独立于 `treehole_summaries` 聚合表）
- [ ] AC-5：SQLite-async 单测覆盖 7+ 个场景：add / list / aggregate / purge_expired / purge_all / decrypt round-trip / cross-process persistence
- [ ] AC-6：`/api/v1/treehole/*` 全部端点接入持久化（list、aggregate、purge）
- [ ] AC-7：现有 1060 passed 测试**零回归**（默认 InMemory repo 不破坏既有逻辑）
- [ ] AC-8：Alembic migration 自动创建新表（通过 Base.metadata.create_all）

## 设计

### 1. Schema（新增 ORM model）

```python
class TreeholeEngagement(Base):
    __tablename__ = "treehole_engagements"

    id: Mapped[str]              # uuid
    child_id: Mapped[str]
    parent_id: Mapped[str]
    encrypted_payload: Mapped[bytes]  # base64-decoded nonce||ct
    associated_data: Mapped[bytes | None]
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime]
```

### 2. Repository

```python
class TreeholeRepository(ABC):
    @abstractmethod
    async def add(child_id, parent_id, encrypted_payload, associated_data, expires_at) -> str: ...
    @abstractmethod
    async def list_for_child(child_id) -> list[dict]: ...   # returns encrypted + meta
    @abstractmethod
    async def delete_expired(now) -> int: ...
    @abstractmethod
    async def delete_all_for_child(child_id) -> int: ...
    @abstractmethod
    async def delete_all() -> int: ...
```

### 3. 触发流程

```
[engage()] 
  → encryption.encrypt(plaintext_summary) → encrypted_b64
  → repo.add(child_id, parent_id, decode(encrypted_b64), expires_at)
[list_summaries()]
  → repo.list_for_child(child_id)
  → 对每条 decrypt → TreeholeSummary
[purge_expired()]
  → repo.delete_expired(now)
```

### 4. 风险

| 风险 | 缓解 |
|------|------|
| Postgres 不可用 | 默认 InMemory repo（不依赖 DB） |
| 加密 blob 大小不一致 | LargeBinary 不限长度 |
| Worker A 写入 + Worker B 读取的 race | 单事务 commit（sqlalchemy 默认） |
| 重复 ID 冲突 | 用 uuid4，无碰撞 |
| 旧 TreeholeStore 类向后兼容 | 保留类，但 TreeholeManager 不再用 |

### 5. 不在本工单

- ❌ `/treehole/summaries` 聚合表（→ 后续 PR，独立 schema）
- ❌ 跨 child 的全局搜索（→ PRD #64 范围外）
- ❌ 把 encryption_key 从 env var 改为 KMS（→ 安全工单）

## 后续工单

- **BE-STORG-001-ext-C**：DashboardService 持久化（同模板，半天）
- **BE-TREEHOLE-AGG**：聚合表实现（PRD #64）