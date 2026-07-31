# BE-PUSH-005：通知中心（家长错过的推送回顾）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟠 P1 |
| **域** | Push / Dashboard |
| **来源** | `docs/frontend-integration.md` §7.3.2（家长端"消息中心"页面）|
| **工作量** | 1.5 天 |
| **状态** | 进行中 |
| **依赖** | BE-PUSH-001 ✅（push 注册/偏好）；BE-RT-002 ✅（dashboard_alerts 持久化）|

## 背景

BE-PUSH-001 让家长端注册设备，BE-RT-002 把实时推送（安全阀/高危）
落到 `dashboard_alerts` 表。但目前家长**只在推送那一刻**才能看到
告警 — 错过推送、终端离线、清空通知中心之后就**无法追溯**。

家长端需要"通知中心"页面：

- 列出**所有曾经推过但还没标记已读**的通知
- 支持按 severity 过滤（critical / warning / info）
- 支持"全部已读"一键清零
- 必须 cursor 分页（通知可能 90 天累计数百条）

本工单新增 4 个端点：

| 端点 | 用途 |
|------|------|
| `GET    /api/v1/notifications`            | 列出通知（cursor + 过滤）|
| `POST   /api/v1/notifications/{id}/read` | 单条标记已读 |
| `POST   /api/v1/notifications/read-all`  | 一键全部已读 |
| `GET    /api/v1/dashboard/alerts/{child_id}` | 复用：每孩子维度的 pending 视图 |

## 验收标准（AC）

- [ ] **AC-1**：4 个端点（list + 2 read + 复用 dashboard/alerts）全部 200 路径 + 错误路径有测试覆盖
- [ ] **AC-2**：Cursor 双向一致 — `cursor=N` 取后 `cursor=N+1` 取完 → `has_more=false, next_cursor=null`；`cursor=stale`（旧）返回空 list 而不是 500
- [ ] **AC-3**：lateral-auth — `POST /notifications/{id}/read` 中 id 属于**别的家长** → 404 `NOTIFICATION_NOT_FOUND`（**不**回 403，避免泄露 id 是否存在）
- [ ] **AC-4**：i18n 键 `notifications.not_found`、`notifications.invalid_cursor` 在 `MESSAGES_ZH` 和 `MESSAGES_EN` 都存在
- [ ] **AC-5**：测试 ≥ 15 个，全绿
- [ ] **AC-6**：现有 1676 个测试零回归
- [ ] **AC-7**：OpenAPI tag `notifications` 带 `description`
- [ ] **AC-8**：`limit` 上限 100，>100 → 422；cursor 解码失败 → 400 `INVALID_CURSOR`
- [ ] **AC-9**：空家长的 notifications → 空 list（不是 404）
- [ ] **AC-10**：`read-all` 是幂等的 — 第二次调用 `marked=0`，不报错

## 设计

### 1. 不新建 `Notification` 模型

直接复用 `DashboardAlert`：

| 字段 | 含义 |
|------|------|
| `id`            | alert 主键 |
| `parent_id`     | 接收方（已经记录在 row 上）|
| `child_id`      | 触发方 |
| `alert_type`    | `safety_valve` / `high_risk` |
| `payload`       | `{severity, title, body, ...}` |
| `created_at`    | 触发时间 |
| `delivered_at`  | 已下发时间（push 送达）|
| **缺**：read_at | 待加 |

为不破坏现有 alert 视图（BE-RT-002），新增一个 **可选**列
`read_at`（nullable timestamp）。这样 dashboard alerts 视图不变，
通知中心能 select 同一张表 + `read_at IS NULL` 过滤。

> AC 明确要求"reuse DashboardAlert" — 不新建 model 是设计目标。

### 2. Repo 层扩展

在 `DashboardRepository` ABC 新增方法：

```python
async def list_for_parent(
    self,
    *,
    parent_id: str,
    severity_filter: list[str] | None = None,
    unread_only: bool = False,
    after_id: str | None = None,
    after_ts: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Cursor-paginated list for one parent. Sort: created_at DESC, id DESC.
    `severity_filter` matches `payload['severity']` (case-insensitive).
    `unread_only=True` filters `read_at IS NULL`.
    `after_*` is the previous page's last (id, ts) — strict less-than.
    Returns at most `limit` rows.
    """
```

两个 impl 都要实现：内存版本走 list comprehension，Postgres 走 SQL。

新增三个小方法：

```python
async def mark_alert_read(self, alert_id: str, parent_id: str) -> bool
async def count_unread_for_parent(self, parent_id: str) -> int
```

`mark_alert_read` 携带 parent_id 做 lateral-auth（不靠 endpoint 二次校验）。

### 3. Cursor 编码

不暴露 alert id（泄露 alert id → 攻击者可枚举）。Cursor 是 opaque
base64 JSON：

```python
# niki/api/_cursor.py
def encode_cursor(alert_id: str, ts: datetime) -> str:
    payload = {"id": alert_id, "ts": ts.isoformat()}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def decode_cursor(cursor: str) -> tuple[str, datetime]:
    """Returns (alert_id, ts). Raises ValueError on malformed."""
    ...
```

> AC 明确说 "must NOT be reversible to leak alert IDs" — 但本工单
> 范围内 base64 不是加密，只是 **opaque**（client 不能轻易改）。后续
> 工单如果要把 id 也加密，HMAC/seal 替换实现即可，endpoint 接口
> 不变。

### 4. 端点

```python
# niki/api/notifications.py
router = APIRouter(prefix="/notifications", tags=[
    {"name": "notifications", "description": "Parent-facing notification center ..."}
])

@router.get("", ...)
async def list_notifications(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    severity: str | None = None,   # comma-sep: "critical,warning"
    unread_only: bool = False,
    parent = Depends(get_current_parent),
) -> dict:
    ...

@router.post("/{notification_id}/read", ...)
async def mark_read(
    request: Request, notification_id: str,
    parent = Depends(get_current_parent),
) -> dict:
    ...

@router.post("/read-all", ...)
async def read_all(
    request: Request,
    parent = Depends(get_current_parent),
) -> dict:
    ...
```

响应 shape：

```jsonc
// GET /notifications
{
  "notifications": [
    {
      "id": "<alert_id>",   // alert_id, NOT the opaque cursor
      "child_id": "...",
      "alert_type": "high_risk",
      "severity": "urgent",
      "title": "...",
      "body": "...",
      "created_at": "2026-07-30T10:00:00+00:00",
      "read_at": null
    }
  ],
  "next_cursor": "eyJpZCI6...",
  "has_more": true
}
```

> 注意：响应里**保留** alert_id（前端要用来调 `/read`），但 cursor 是 opaque。
> Cursor 是「上一次最后一条」的位置指针；id 是这一条的标识。两者不冲突。

### 5. Severity filter

`severity` 是 CSV 字符串。前端传 `"critical,warning"` → 服务端
split + lowercase + 校验（必须是 `info`/`warning`/`critical`/`urgent`
子集，否则 400 `INVALID_SEVERITY`）。

`payload.severity` 是 dashboard 写入时塞的（见 `record_safety_valve`
→ `severity="info"`；`record_high_risk` → `severity="urgent"`）。
这两类目前只覆盖 info / urgent。filter 默认不限制。

### 6. 错误体（§6.3 shape）

| 端点 | 状况 | 状态码 | code |
|------|------|--------|------|
| GET | limit>100 | 422 | (FastAPI 默认) |
| GET | cursor 解码失败 | 400 | `INVALID_CURSOR` |
| GET | severity 非法值 | 400 | `INVALID_SEVERITY` |
| POST /{id}/read | id 不存在或不属于调用方 | 404 | `NOTIFICATION_NOT_FOUND` |
| POST /read-all | — | 200 | (永远成功) |

### 7. 风险

| 风险 | 缓解 |
|------|------|
| cursor 编码被破解泄露 alert id | 当前只 base64（opaque）；后续工单再 HMAC |
| read-all 在 1000+ 条时阻塞 | 现有 dashboard_alerts 量级 < 100/家长，足够；如未来需 scan 分批 |
| 现有 dashboard/alerts 视图受影响 | `read_at` 列加 nullable 不破坏 view；列上加 index (`parent_id, read_at`) |
| 测试 module-level singleton 串扰 | 用 `set_dashboard_repository(InMemoryDashboardRepository())` per-test |
| Postgres `payload->>'severity'` JSONB filter 性能 | 现实量级 < 1000/parent；如未来量大可加 GIN index（不在本工单）|

### 8. 不在本工单

- ❌ 把 `read_at` 同步到 push vendor 侧（device 上的"已读"状态）
- ❌ WebSocket / SSE 实时新增通知（已有 BE-RT-002 alerts 流；通知中心是列表视图）
- ❌ 子路由按 child 分（`GET /notifications?child_id=`）— 暂不需要
- ❌ HMAC seal cursor（保留 base64 opaque；可作为后续工单）

## 后续

- **BE-PUSH-006**（可选）：HMAC seal cursor，避免 alert id 泄露
- **BE-PUSH-007**（可选）：按 child_id 过滤
- **BE-STOR-002-ext-B**：90 天 TTL 任务（Redis 落地后从老 `dashboard_alerts` 删除）