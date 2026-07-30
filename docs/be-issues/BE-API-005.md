# BE-API-005：批量操作端点

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API |
| **来源** | 前端家长端 dashboard init 路径实测（5 entities + 4 relations = 9 round trips，200-500ms） |
| **工作量** | 1 天 |
| **状态** | ✅ 完成（2026-07-30） |
| **依赖** | BE-API-004 ✅（list filter / sort / search） |
| **后续** | BE-API-006（OpenAPI 注释）；BE-API-007（TS SDK 自动生成） |

## 背景

前端家长端的"新增孩子"流程要初始化这个孩子的认知图：

- 5 个 entity（孩子的学校、爱好、家庭成员等）
- 4 条 relation（"likes" / "remembers" / "knows_about" / "related_to"）

当前每个 item 都要一次 `POST /memory/graph/entities` 或 `POST /memory/graph/relations`，
共 9 个 round trip。加上移动网络延迟，**实测 200-500ms**。家长点完要等半秒
才看到下一步，体验差。

同样"批量标记告警已读"也是 N 次 `POST /alerts/{id}/delivered`。

本工单新增 3 个 batch 端点，把 round trip 从 N 降到 1，**每个端点的
partial-success 语义**让前端不需要先拉到 ID 列表再循环：失败的项目
进 `skipped`、成功的进 `created` / `dismissed`。

## 验收标准（AC）

- [x] AC-1：`POST /api/v1/memory/graph/entities/batch` 接受 `{"items": [...]}`，返回 `{created, skipped}`
- [x] AC-2：`POST /api/v1/memory/graph/relations/batch` 接受 `{"items": [...]}`，返回 `{created, skipped}`
- [x] AC-3：`POST /api/v1/dashboard/alerts/batch/dismiss` 接受 `{"alert_ids": [...]}`，返回 `{dismissed, skipped}`
- [x] AC-4：每个 item 独立校验，独立成败（partial success）
- [x] AC-5：duplicate entity name → `skipped[].reason = "duplicate"`
- [x] AC-6：missing alert id → `skipped[].reason = "not_found"`
- [x] AC-7：每个端点 **100 items 上限**；101+ → 400 `BATCH_TOO_LARGE`（带 `limit` / `received` / `field` 字段）
- [x] AC-8：单测 + 集成测试 ≥ 6 个（5 happy + 1 越界）覆盖三个端点
- [x] AC-9：现有 1523 passed / 2 skipped → **零回归**
- [x] AC-10：batch 端点**不替换** 单个端点 — 两个都保留

## 设计

### 1. 服务层 helper（不是新 API，是新 batch 入口）

| 在哪 | 签名 | 行为 |
|------|------|------|
| `niki/memory/graph.py` | `add_entities_batch(items) -> {created, skipped}` | 重复 name → `skipped[reason="duplicate"]`；缺字段 → `skipped[reason="invalid"]` |
| `niki/memory/graph.py` | `add_relations_batch(items) -> {created, skipped}` | 缺字段 / 非法 relation → `skipped[reason="invalid"\|"invalid_relation"]`；`add_relation` 本身 idempotent，所以 duplicate edge 走"created"（weight 累加） |
| `niki/dashboard/service.py` | `dismiss_alerts_batch(alert_ids) -> {dismissed, skipped}` | 复用 `mark_alert_delivered`；id 不存在 → `skipped[reason="not_found"]` |

核心：**helper 不知道 endpoint 存在**。它就是个 Python 函数，测试直
接调它就行；端点层只负责 request 校验 + 100 cap。

### 2. 端点路由

```
POST /api/v1/memory/graph/entities/batch
POST /api/v1/memory/graph/relations/batch
POST /api/v1/dashboard/alerts/batch/dismiss
```

3 条路由紧贴原始单条端点放（`memory.py` / `dashboard.py`），保证读代
码时一眼能找到 batch 兄弟。

### 3. 100-item cap

提取到 `niki/api/memory.py::BATCH_MAX_ITEMS = 100` + `_enforce_batch_cap(...)`，
3 个 batch 端点共用。dashboard 端点用 `from niki.api.memory import _enforce_batch_cap, BATCH_MAX_ITEMS` 复用 — 不重复实现。

```python
def _enforce_batch_cap(request, items, field_name="items"):
    payload = items or []
    if len(payload) > BATCH_MAX_ITEMS:
        raise HTTPException(400, detail=localized_error_response(
            request,
            code="BATCH_TOO_LARGE",
            key="api.batch_too_large",
            extra={"limit": BATCH_MAX_ITEMS, "received": len(payload), "field": field_name},
            ...
        ))
```

错误体走 §6.3 shape：`{"error": "BATCH_TOO_LARGE", "message": "...", "limit": 100, "received": 150, "field": "items"}`，前端按 `error.code` 分支。

### 4. 响应 shape

| 端点 | items | created / skipped 的元素 |
|------|-------|--------------------------|
| `entities/batch` | `[{name, entity_type?, aliases?}]` | `created`: entity.to_dict()；`skipped`: `{name, reason}` |
| `relations/batch` | `[{source, target, relation, weight_delta?}]` | `created`: edge.to_dict()；`skipped`: `{source, target, reason}` |
| `alerts/batch/dismiss` | `[alert_id, ...]` | `dismissed`: int；`skipped`: `{alert_id, reason}` |

没有包 `APIResponse[T]` / `PaginatedResponse` — batch 响应**主键就
是 created/skipped**，多一层包装只会让前端多写 `.data.created`。
跟所有现有端点（POST `/graph/entities` / `/graph/relations` 等）的
返回风格保持一致。

### 5. 风险

| 风险 | 缓解 |
|------|------|
| batch 端点被滥用成 DoS | 100 cap + slowapi 走默认 100/minute |
| partial-success 让前端误判成功 | 响应里 `skipped` 字段**永远是数组**（哪怕为空），前端看 `len(skipped)` 即可 |
| 旧的单条端点被新代码路径绕过 | 单条端点**不动**，batch 端点是叠加 |
| 测试 isolate（memory graph + dashboard repo 是 module-level singleton） | 通过 `conftest.py` 的 `_register_common_test_children` / `client` 夹具 + 生成的唯一 child_id 隔离 |

### 6. 不在本工单

- ❌ 真正的部分失败（entity 3 失败要不要回滚 1-2）— 接受 partial-success，不做事务
- ❌ 异步 / 队列化（导入 100 entity 也不到 50ms，单 service worker 足够）
- ❌ GZip / streaming response（cap 100 太小不必要）
- ❌ 其它 batch（不限于当前 3 个端点）— 等后续工单

## 后续

- **BE-API-006**：OpenAPI 描述补充（让前端 SDK 生成器能拿到 schema）
- **BE-API-007**：TS SDK 自动生成
- **BE-API-008**（可选）：其他批量端点（批量 record knowledge query / tutoring event）
