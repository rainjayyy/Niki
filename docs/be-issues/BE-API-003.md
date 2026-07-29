# BE-API-003：列表端点分页

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0 |
| **域** | API |
| **来源** | 前端对接文档 §7.1.7 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | `PaginatedResponse` schema（已存在） |
| **后续** | BE-API-004（filter / sort / search） |

## 背景

当前多个 list 端点返回**全量数据**：

- `GET /api/v1/memory/graph/entities` → 全部 entity
- `GET /api/v1/treehole/summaries/{child_id}` → 全部 summary

后果：
- 列表大到几千条时浏览器卡死
- 首屏 N+1 序列化阻塞事件循环
- 家长端没法用 page / page_size 拼接 UI

本工单给上述 2 个端点加 `?page=&page_size=`，统一返回 `PaginatedResponse` 形状。

## 验收标准（AC）

- [ ] AC-1：2 个端点（`/memory/graph/entities`、`/treehole/summaries/{child_id}`）接 `?page=&page_size=`
- [ ] AC-2：`page_size` 默认 20，最大 100，越界 422
- [ ] AC-3：`page` 默认 1，最小 1
- [ ] AC-4：响应包含 `total` / `page` / `page_size` / `has_more` / `items`（或 `entities` / `summaries`）
- [ ] AC-5：现有调用方（无 query 参数）继续返回全量（向后兼容）或默认第 1 页 20 条
- [ ] AC-6：测试覆盖 ≥ 6 个，含：默认参数 / 越界 / 多页 / has_more 计算
- [ ] AC-7：现有 1215 passed 测试**零回归**

## 设计

### 1. Query 参数解析

```python
def parse_pagination(
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, int]:
    if page < 1: raise 422
    if not 1 <= page_size <= 100: raise 422
    return page, page_size
```

### 2. 切片 + 响应

```python
def paginate(items: list, page: int, page_size: int) -> dict:
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "items": items[start:end],
    }
```

### 3. 端点改动

| 端点 | 旧响应 | 新响应 |
|------|--------|--------|
| `/memory/graph/entities` | `{count, entities: [...]}` | `{total, page, page_size, has_more, entities: [...]}` |
| `/treehole/summaries/{child_id}` | `{child_id, count, summaries: [...]}` | `{child_id, total, page, page_size, has_more, summaries: [...]}` |

**向后兼容**：`count` 字段保留在 `/memory/graph/entities` 响应（值 = `total`），让老前端不破。

### 4. 不在本工单

- ❌ `?since=&until=&keyword=&sort=&order=` → `BE-API-004`
- ❌ 游标分页（cursor-based）— 列表规模暂不需要
- ❌ 其它 list 端点分页（如 `audit/recent` 已有 limit）