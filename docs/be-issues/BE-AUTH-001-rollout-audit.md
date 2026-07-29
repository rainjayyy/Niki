# BE-AUTH-001-rollout-audit：横向越权 audit log

| 字段 | 值 |
|------|-----|
| **优先级** | 🟠 P0（安全审计） |
| **域** | AUTH / SECURITY |
| **来源** | PRD §6.4 审计要求 + BE-AUTH-001-rollout-ext 后续 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-AUTH-001 ✅ / BE-AUTH-001-rollout ✅ / BE-AUTH-001-rollout-ext ✅ / BE-STOR-001-ext-E ✅ |
| **后续** | 告警阈值 / SIEM 导出（→ BE-AUTH-001-rollout-alerts） |

## 背景

BE-AUTH-001-rollout 修了「未鉴权」漏洞（任何人都能调），BE-AUTH-001-rollout-ext 修了「水平越权」漏洞（家长 A 改家长 B 的孩子），BE-STOR-001-ext-E 修了「多 worker 不一致」漏洞。

**但**：lateral auth 现在是 throw-and-forget — 越权请求被拒绝就完事，**没有留下任何证据**。

后果：
- 家长 A 试探性访问 B 的孩子 → 后端 403 → 没了 → 安全团队看不到攻击信号
- 同一攻击者重复尝试 → 没有速率 / 趋势 / IP 画像
- 事后追责无法举证"是谁 / 什么时候 / 试了什么"
- 满足 PIPL / 等保 2.0 三级 §8.1.4 审计日志要求（至少 6 个月留存）

本工单补齐 audit trail，让所有 lateral auth 违规**留下结构化证据**，并提供查询端点供安全团队事后审查。

## 验收标准（AC）

- [ ] AC-1：新增 `auth_audit_logs` 表 + `AuthAuditLog` ORM model
- [ ] AC-2：`AuthAuditLogRepository` ABC + `InMemoryAuthAuditLogRepository`（默认）+ `PostgresAuthAuditLogRepository`
- [ ] AC-3：`AuthAuditLogger` service 同时写 DB + structlog（生产可走 ELK / Loki）
- [ ] AC-4：`get_owned_child_id` 在 `PARENT_CHILD_MISMATCH` 和 `CHILD_NOT_FOUND` 时记录 audit event
- [ ] AC-5：`register_child` / `list_children` 在 `PARENT_MISMATCH` 时记录 audit event
- [ ] AC-6：`GET /api/v1/auth/audit/recent` 端点，默认只返回当前家长的 event（防越权查询）
- [ ] AC-7：可选 `?event_type=` 过滤，`?limit=` 上限 200
- [ ] AC-8：测试覆盖 ≥ 8 个，含：单条记录 / 多条累积 / 端点过滤 / event_type 过滤 / 默认只看自己 / 上限 200
- [ ] AC-9：现有 1109 passed 测试**零回归**

## 设计

### 1. ORM model

```python
class AuthAuditLog(Base):
    __tablename__ = "auth_audit_logs"
    id: Mapped[str]              # UUID
    event_type: Mapped[str]      # parent_child_mismatch | parent_mismatch |
                                 # child_not_found | token_revoked | ...
    severity: Mapped[str]        # info | warning | critical
    actor_parent_id: Mapped[str | None]   # 来自 JWT 的 parent.id
    target_parent_id: Mapped[str | None]  # 路径里的 parent_id
    target_child_id: Mapped[str | None]   # 路径里的 child_id
    endpoint: Mapped[str]        # "POST /api/v1/children/register/{parent_id}"
    method: Mapped[str]          # POST | GET | ...
    reason: Mapped[str]          # 人话描述
    client_ip: Mapped[str | None]
    created_at: Mapped[datetime]
```

索引：`(actor_parent_id, created_at)` 复合索引便于按家长查询

### 2. Repository

```python
class AuthAuditEvent:
    event_type: str; severity: str
    actor_parent_id: str | None
    target_parent_id: str | None
    target_child_id: str | None
    endpoint: str; method: str
    reason: str
    client_ip: str | None

class AuthAuditLogRepository(ABC):
    async def add(self, event: AuthAuditEvent) -> str
    async def list_recent(
        self, *, limit: int = 100,
        event_type: str | None = None,
        actor_parent_id: str | None = None,
    ) -> list[dict]
    async def count(self, *, event_type: str | None = None, since: datetime | None = None) -> int
```

`InMemoryAuthAuditLogRepository` 默认；`PostgresAuthAuditLogRepository` 复用现有 SQLAlchemy async 模式（参考 dashboard / treehole / emotion_baseline repo）。

### 3. Service

```python
class AuthAuditLogger:
    async def log_violation(self, event_type: str, *, ...) -> str:
        # severity="warning" + structlog.warning + repo.add
    async def log_event(self, event_type: str, *, severity="info", ...) -> str:
        # severity=info + structlog.info + repo.add

# process-wide singleton
def get_auth_audit_logger() -> AuthAuditLogger: ...
def set_auth_audit_logger(logger: AuthAuditLogger) -> None: ...
```

### 4. Lateral check hook 点

| 位置 | 触发条件 | event_type | severity |
|------|---------|-----------|----------|
| `get_owned_child_id` | `CHILD_NOT_FOUND` | `child_not_found` | warning |
| `get_owned_child_id` | `PARENT_CHILD_MISMATCH` | `parent_child_mismatch` | warning |
| `register_child` | `PARENT_MISMATCH` | `parent_mismatch` | warning |
| `list_children` | `PARENT_MISMATCH` | `parent_mismatch` | warning |

每次违规同时记录：`actor_parent_id`（JWT）、`target_*`（路径）、`endpoint`、`method`、`reason`、`client_ip`。

### 5. 查询端点

```
GET /api/v1/auth/audit/recent?limit=50&event_type=parent_child_mismatch
Authorization: Bearer <token>

→ 200 {
    "events": [
      { "id": "...", "event_type": "...", "severity": "warning",
        "actor_parent_id": "...", "target_parent_id": "...",
        "target_child_id": "...", "endpoint": "...",
        "method": "POST", "reason": "...",
        "client_ip": "10.0.0.1",
        "created_at": "2026-07-29T12:34:56Z" },
      ...
    ],
    "count": 12
  }

默认 actor_parent_id 锁定为 token 的 parent.id（防通过查询端点反查他人）
limit 上限 200
```

### 6. 风险

| 风险 | 缓解 |
|------|------|
| Audit 写库失败导致 lateral check 误判 | 用 try/except 包住 audit 调用，写库失败仅 structlog.error，不阻塞 403 |
| 查询端点泄露其他家长的审计 | 默认按 `actor_parent_id == caller.id` 过滤 |
| 高频试探导致 audit 表爆 | 上限 200 / 查询；后台归档 → 后续工单 |
| 写入延迟影响 P99 | `add()` 是 insert + flush（无 commit），微秒级 |

### 7. 不在本工单

- ❌ Admin 角色 / 跨家长查询（→ 后续 admin 工单）
- ❌ 告警阈值 + 飞书 / 邮件通知（→ BE-AUTH-001-rollout-alerts）
- ❌ 异步落盘 / Kafka / SIEM 导出（→ BE-AUTH-001-rollout-siem）
- ❌ 自动归档 / 6 个月留存策略（→ DBA 流程）

## 后续工单

- **BE-AUTH-001-rollout-alerts**：5 分钟内 ≥3 次同 actor 越权 → 飞书 / 邮件
- **BE-AUTH-001-rollout-siem**：异步 ELK 导出 + 6 个月冷存
- **BE-ADMIN-001**：admin 角色 + 跨家长 audit 查询端点