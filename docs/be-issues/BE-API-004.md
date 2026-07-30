# BE-API-004：列表端点 filter / sort / search

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API |
| **来源** | 前端对接文档 §7.2.7 |
| **工作量** | 0.5 天 |
| **状态** | ✅ 完成（2026-07-30） |
| **依赖** | BE-API-003 ✅（分页基础） |
| **后续** | BE-API-005（批量） |

## 背景

BE-API-003 给两个 list 端点加了分页。但前端还需要 **filter / sort / search**：

- 家长端："查看 c-1 这个孩子过去 30 天的告警，按严重度倒序"
- 家长端："搜索包含'今天不开心'的对话"
- 运营 dashboard："列出所有高危告警"

当前：拉全量 → 前端 slice + filter → 性能差 + 流量大。

本工单为 3 个 list 端点补齐 `?since=&until=&sort=&order=&keyword=`，服务端做
时间窗 + 关键字 + 排序，前端不必再拉全表。

## 验收标准（AC）

- [x] AC-1：3 个 list 端点（`/auth/audit/recent`、`/treehole/summaries/{child_id}`、`/memory/graph/entities`）接 `?page=&page_size=&since=&until=&sort=&order=&keyword=`
- [x] AC-2：`order` 仅 `asc` / `desc`，否则 400 INVALID_ORDER
- [x] AC-3：`sort` 端点级白名单，非法值 400 INVALID_SORT
- [x] AC-4：`keyword` 大小写不敏感 substring 匹配，前后空格 trim
- [x] AC-5：`since > until` → 400 INVALID_RANGE
- [x] AC-6：默认行为不变（无 query → 全量、created_at/timestamp 降序）
- [x] AC-7：响应形状零变更（只删行 / 排序，不动 key 集合）
- [x] AC-8：测试覆盖 ≥ 5 个，含 since/until、sort/order asc 反转、keyword 大小写不敏感、INVALID_ORDER、分页 + 过滤组合
- [x] AC-9：现有 1381 passed / 2 skipped → 零回归

## 设计

### 1. 通用 helper — `niki/api/_pagination.py`

```python
@dataclass(frozen=True)
class ListQueryParams:
    page: int = 1
    page_size: int = 20
    since: datetime | None = None
    until: datetime | None = None
    sort: str = "created_at"
    order: str = "desc"
    keyword: str | None = None

    @property
    def is_ascending(self) -> bool: ...

    def normalized_keyword(self) -> str | None:
        """strip + lowercase; returns None on whitespace-only."""

def list_query_params(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    sort: str = Query("created_at"),
    order: str = Query("desc"),
    keyword: str | None = Query(None, max_length=128),
) -> ListQueryParams:
    """FastAPI dependency: validate + package into a dataclass.

    Raises:
      - 400 INVALID_ORDER for any order not in ('asc', 'desc')
      - 400 INVALID_RANGE for since > until
    """

def sort_rows(rows, *, sort, order, time_field="created_at") -> list:
    """Sort a list of dict | dataclass rows by `sort` + `order`.

    Unknown `sort` falls back to `time_field` so a bad input
    never 500s the endpoint.
    """
```

### 2. 端点改动

| 端点 | 端点-level sort 白名单 | keyword 字段 |
|------|---------------------|--------------|
| `/auth/audit/recent`      | `created_at`, `severity` | actor/target parent/child id + endpoint |
| `/treehole/summaries/{child_id}` | `timestamp`, `emotion_intensity` | serialised summary JSON（category / valence / intensity） |
| `/memory/graph/entities`  | `first_seen`, `last_seen`, `name` | entity `name` + `aliases` |

3 个端点的 `?since=&until=&keyword=&sort=&order=&page=&page_size=` 在 endpoint
层统一通过 `Depends(list_query_params)` 接收。**响应 shape 零变更**：

- `/auth/audit/recent`：保留 `{actor_parent_id, event_type_filter, limit, count, events}`，额外追加 `sort`, `order`, `keyword` 三个回显字段（向后兼容 + 调试便利）
- `/treehole/summaries/{child_id}`：保留 `{child_id, count, total, page, page_size, has_more, summaries}`
- `/memory/graph/entities`：保留 `{total, count, page, page_size, has_more, entities}`

### 3. 服务层过滤

- `niki/treehole/manager.py::list_summaries`
  新增 `since / until / sort / order / keyword` 关键字参数。
  `since`/`until` 通过新增的 `_summary_in_window()` 模块级辅助函数比对；
  `keyword` 走 `json.dumps(summary.to_dict()).lower()` substring。
- `niki/memory/graph.py::list_entities`
  新增 `sort / order / keyword` 参数，`keyword` 在 `node.name + aliases`
  上做大小写不敏感 substring；`sort` 未识别则回落到 `first_seen`。
- `niki/api/auth_audit.py::list_recent_audit`
  端点层（在 repo 拉到的 `_MAX_LIMIT` 窗口内）做 `since / until / keyword`
  过滤 + 排序，再裁剪到 `limit`。`actor_parent_id` 强制 = caller（保持
  现有安全默认）。

### 4. 不在本工单

- ❌ 物理 DB 层下推 keyword（in-memory 已 O(n) 足够；Postgres 后续 BE-STOR 接续）
- ❌ 时间分桶（hour buckets / day buckets）— 后续按需
- ❌ 全文检索（Postgres tsvector / Meilisearch）— `?keyword=` 仅 substring

## 后续

- **BE-API-005**：批量操作端点
- **BE-API-006**：OpenAPI 描述补充
- **BE-API-007**：TS SDK 自动生成
