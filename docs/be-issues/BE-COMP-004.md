# BE-COMP-004：统一审计日志表

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | COMPLIANCE / STORAGE |
| **来源** | 合规工单 §7.2.8 + PIPL Art. 14 / 47 |
| **工作量** | 0.75 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-AUTH-001-rollout-audit ✅（`auth_audit_logs` 表已上，本工单沿用其模式） |
| **后续** | BE-ADMIN-001 管理员角色（跨 parent 查询）、审计留存归档任务 |

## 背景

BE-COMP-004 之前是 ⚠️ **部分完成**：横向越权事件走 `auth_audit_logs` 表，
其余合规相关事件（安全阻断 / 同意签署 / 删除请求）**只进 structlog**。

后果是家长（或客服）问「我账号上都发生了什么」时无处可查：

| 事件 | 落地位置（本工单前） | 可查询？ |
|------|--------------------|---------|
| 横向越权 | `auth_audit_logs` 表 | ✅ |
| 同意签署 | structlog | ❌ |
| 删除请求 | structlog | ❌ |
| 安全阻断 / 高危信号 | structlog | ❌ |
| 登录成功 / 短信下发 | 无 | ❌ |

structlog 记录进的是 ELK / Loki，**不是**产品能查的接口 —— 家长端拿不到，
客服要开 Kibana 翻日志，而 PIPL Art. 14（同意可追溯）和 Art. 47（删除留痕）
都要求这些事件能按人取回。

本工单新增 `audit_logs` 统一表，让上述所有域走同一个入口、落同一张表、
从同一个接口读回。

## 验收标准（AC）

- [ ] AC-1：`niki/db/models/audit_log.py` 新增 `AuditLog` 模型，含
      `id` / `event_type` / `severity` / `actor_parent_id` /
      `target_parent_id` / `target_child_id` / `endpoint` / `method` /
      `reason` / `metadata`(JSON) / `client_ip` / `created_at` 全部列
- [ ] AC-2：复合索引 `(actor_parent_id, created_at)` +
      `(event_type, created_at)`
- [ ] AC-3：`audit_log_repo.py` 提供 ABC + InMemory + Postgres 三件套
- [ ] AC-4：`AuditLogService.log_event(...)` 为**唯一**写入口，各域统一调用
- [ ] AC-5：`GET /api/v1/admin/audit?event_type=&since=&actor_parent_id=&limit=50`
      鉴权 + 按 caller 自身 scope
- [ ] AC-6：接入 `auth.py`（`consent_signed` / `sms_sent` / `login_success`）
      与 `dashboard.py`（`safety_block` / `high_risk_recorded`）
- [ ] AC-7：`auth_audit.py` 双写 —— `auth_audit_logs` **保留**，同时镜像到
      `audit_logs`
- [ ] AC-8：统一表**不含 PII**（无孩子原话、无家长手机号）
- [ ] AC-9：测试 ≥ 5 个（`tests/compliance/test_audit_log.py`）
- [ ] AC-10：现有 **1576 passed, 2 skipped** 零回归

## 设计

### 1. 为什么是「双写」而不是「迁移」

最直觉的做法是把 `auth_audit_logs` 合并进 `audit_logs` 然后删掉旧表。
**没有这么做**，原因是两张表的读写模式相反：

| | `auth_audit_logs` | `audit_logs` |
|---|---|---|
| 写入频率 | 低（只有越权时） | 高（每次登录 / 短信 / 阻断） |
| 读取者 | 告警逻辑（`ViolationRateTracker`，热路径） | 家长 / 客服（冷路径） |
| 查询形状 | 单 actor 最近 N 条，窄索引扫描 | 多维过滤 |
| 列宽 | 窄（无 JSON 列） | 宽（带 `metadata` JSONB） |

告警路径每次越权都要数「这个 actor 在窗口内犯了几次」。让它去扫一张
被登录事件灌满、还带 JSONB 列的宽表，是拿热路径延迟换一张表的整洁 ——
不值得。所以：

- `auth_audit_logs`：**保留不动**，仍是横向越权告警的权威来源，
  schema 一列不改（测试里明确断言它没长出 `metadata` 列）。
- `audit_logs`：新增，跨域只读视图。`AuthAuditLogger._safe_add` 在写完
  旧表后镜像一份过来，`metadata.source = "auth_audit_logs"` 标出血缘。

镜像失败是 `warning` 不是 `error`：旧表此时已落地，跨域视图少一行的代价
远小于让审计写失败影响用户已经拿到的响应。

### 2. 分层

```
各域调用点 (auth.py / dashboard.py / auth_audit.py)
        ↓  log_event(event_type, severity=..., metadata={...})
AuditLogService          ← 唯一写入口；脱敏 + 永不抛异常
        ↓  AuditEvent (frozen dataclass, 构造时校验 severity)
AuditLogRepository (ABC) ← InMemory (默认/测试) | Postgres (生产)
        ↓
audit_logs 表
        ↑
GET /api/v1/admin/audit  ← actor-scoped 读回
```

`AuditLogService` 承担两条不变量，各调用点因此不需要自己操心：

1. **永不抛异常。** 审计写失败不能改变用户已经挣到的响应。非法
   `severity` / DB 挂掉都只记 log 然后返回 `None`，与
   `AuthAuditLogger._safe_add` 的既有约定一致。
2. **永不落 PII。** `_scrub_metadata()` 对 `original_text` / `phone` /
   `typed_signature` 等 denylist key 替换为 `"[redacted]"`（保留 key 而非
   删除 —— 排查调用点时能看出「这里本来有东西」），并把超过 200 字符的
   字符串截断（长字符串本质上就是自由文本）。

### 3. 事件类型

`event_type` 是自由 String（新域接入不用改 migration），但常量集中在
`niki/db/models/audit_log.py`，避免两个域把同一事件拼成两种写法：

| 常量 | 值 | severity | 来源 |
|------|-----|----------|------|
| `EVENT_CONSENT_SIGNED` | `consent_signed` | info | `auth.py` |
| `EVENT_LOGIN_SUCCESS` | `login_success` | info | `auth.py` |
| `EVENT_SMS_SENT` | `sms_sent` | info | `auth.py` |
| `EVENT_SAFETY_BLOCK` | `safety_block` | warning | `dashboard.py` |
| `EVENT_HIGH_RISK_RECORDED` | `high_risk_recorded` | critical | `dashboard.py` |
| `EVENT_LATERAL_AUTH_VIOLATION` | `lateral_auth_violation` | warning | `auth_audit.py`（镜像） |
| `EVENT_DELETION_REQUESTED` | `deletion_requested` | warning | 预留 |
| `EVENT_DELETION_COMPLETED` | `deletion_completed` | warning | 预留 |

### 4. 端点

```
GET /api/v1/admin/audit
Query: ?event_type=safety_block     # 精确匹配
       &since=2026-07-01            # ISO 8601 或 YYYY-MM-DD，闭区间下界
       &actor_parent_id=<id>        # v1 必须等于自己，否则 403
       &limit=50                    # 1..200
Auth: Bearer <access_token>

→ 200 {
    "actor_parent_id": "p_x", "event_type_filter": null,
    "since": null, "limit": 50, "count": 3,
    "events": [ { "id", "event_type", "severity", "actor_parent_id",
                  "target_parent_id", "target_child_id", "endpoint",
                  "method", "reason", "metadata", "client_ip",
                  "created_at" }, ... ]
  }
→ 403 { "error": "FORBIDDEN", ... }   # actor_parent_id != token sub
```

**为什么路径叫 `/admin` 而 v1 只能查自己？** 因为形状已经是管理端形状：
单端点、actor-scoped、可过滤。BE-ADMIN-001 上管理员角色后只需放开下面
那个 scope 检查，前端不用改 URL。

在那之前规则是绝对的：`actor_parent_id` 强制为 JWT 里的 parent id。
传别人的 id 是 **403 而非静默改写** —— 探测别人历史的调用方应该知道自己
被拒了，而且这次尝试本身也该进审计。

### 5. 不含 PII 的取舍

统一表被客服工具和（经 `/admin/audit`）家长本人读取，比各域表广得多，
所以按更严的标准约束：

- `safety_block` **不带** `problem_summary`（题目描述贴着孩子），只带
  `round_reached` + `alert_id`；原文仍在 `dashboard_alerts`。
- `high_risk_recorded` **不带** `original_text`。孩子说的话留在
  `dashboard_alerts`，审计行只有 `alert_id` 指针。
- `sms_sent` **不带**手机号，只带「发了」+ captcha 姿态。这是 pre-auth
  事件，没有 actor —— 按 IP 统计这类行的量才是识别短信轰炸的信号。
- `consent_signed` **不带** `typed_signature`，只带文档版本 / 是否代签
  儿童同意。

测试里对后两条有直接断言（`assert "我不想活了" not in str(row)`），
不靠约定靠断言。

### 6. Migration

`alembic/versions/2026_07_30_0002_add_audit_logs.py`，`down_revision`
指向 `2026_07_30_0001`。`metadata` 列用
`sa.JSON().with_variant(postgresql.JSONB(), "postgresql")` —— 与
`treehole_aggregations.category_counts` 同一写法，SQLite 测试库也能建。

注意模型上 Python 属性叫 `event_metadata`（`metadata` 是 SQLAlchemy
declarative 保留名），DB 列名仍是工单要求的 `metadata`。仓储层的
`to_dict()` / `to_row()` 负责两种命名之间的转换，API 出口永远是
`metadata`。

### 7. 风险

| 风险 | 缓解 |
|------|------|
| 审计写失败影响主流程 | `log_event` 吞掉所有异常，返回 `None` |
| 高频事件把表写爆 | 索引按 `(actor, time)` / `(event, time)`；留存归档任务 → 后续工单 |
| 调用点误传 PII 进 metadata | `_scrub_metadata()` denylist + 长度截断 |
| 双写导致告警路径变慢 | 镜像在旧表写入**之后**、告警判定**之前**，且失败不阻塞 |
| 测试间审计状态泄漏 | `conftest.py` 新增 `_reset_unified_audit` autouse fixture |

### 8. 不在本工单

- ❌ 删除 `auth_audit_logs` —— 见 §1，故意保留
- ❌ 管理员角色 / 跨 parent 查询 —— → BE-ADMIN-001
- ❌ 6 个月留存归档到冷存储 —— → 后续运维工单
- ❌ `deletion_requested` / `deletion_completed` 实际接入 ——
      常量已预留，`deletion.py` 目前还没有持久化的 DeletionRequest 仓储

## 后续工单

- **BE-ADMIN-001**：管理员角色，放开 `/admin/audit` 的 actor scope
- **BE-COMP-004-ext-A**：审计留存策略 + 冷存储归档任务
- **BE-COMP-004-ext-B**：`deletion_requested` / `deletion_completed`
  接入（依赖 DeletionRequest 仓储落地）
