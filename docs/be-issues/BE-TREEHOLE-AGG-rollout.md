# BE-TREEHOLE-AGG-rollout：多窗口聚合（daily + weekly）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | TREEHOLE |
| **来源** | 前端对接文档 §7.1.8（dashboard §2.4 推送矩阵） |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-TREEHOLE-AGG ✅（单窗口基础） |
| **后续** | monthly / quarterly 滚动 → BE-TREEHOLE-AGG-rollout-rollout |

## 背景

BE-TREEHOLE-AGG 把"基线聚合"从全表扫描降为 O(1)。但**只有 7 天 weekly 窗口**一种粒度。

实际场景需要：
- **daily**（1 天）—— 短期"今天聊得怎样"卡片
- **weekly**（7 天）—— 当前 weekly report
- **monthly**（30 天）—— 月度趋势（后续工单）

现在前端调 `aggregate_for_baseline()` 只能拿 weekly。本工单加上 **daily + weekly 并存**，每次 `engage()` 同时更新两个窗口（写入成本是 2 个 UPSERT，可忽略）。

## 验收标准（AC）

- [ ] AC-1：`TreeholeAggregation` 模型加 `window_kind` 字段（"daily" | "weekly"）
- [ ] AC-2：`increment()` 同时写 daily + weekly 两条
- [ ] AC-3：`get(child_id, window_kind="weekly")` 按窗口读
- [ ] AC-4：`aggregate_for_baseline(child_id, window_kind="daily")` 工作
- [ ] AC-5：现有 weekly 行为不变（默认 `window_kind="weekly"`）
- [ ] AC-6：测试覆盖 ≥ 5 个，含：dual-window increment / 跨窗口独立 / 跨窗口聚合
- [ ] AC-7：现有 1303 passed 测试**零回归**

## 设计

### 1. ORM 变更

```python
class TreeholeAggregation(Base):
    __tablename__ = "treehole_aggregations"
    child_id: Mapped[str]            # PK
    window_kind: Mapped[str]        # "daily" | "weekly"
    window_start: Mapped[date]       # PK
    count, valence_sum, ...        # 现有字段
    __table_args__ = (
        UniqueConstraint(
            "child_id", "window_kind", "window_start",
            name="uq_treehole_agg_scope",
        ),
    )
```

升级路径：
- 旧 `(child_id, window_start)` PK → 新 `(child_id, window_kind, window_start)`
- Alembic migration：增加 `window_kind` 列，default `'weekly'`，PK 重建

### 2. Window 边界计算

```python
WINDOW_KIND_DAILY = "daily"
WINDOW_KIND_WEEKLY = "weekly"

def _window_start(kind: str, ts: datetime) -> date:
    if kind == "daily":
        return ts.date()
    if kind == "weekly":
        # ISO weekday: Monday=1.  Back to Monday.
        return ts.date() - timedelta(days=ts.weekday() - 1)
    raise ValueError(f"unknown window_kind: {kind}")
```

### 3. 仓储

```python
async def increment(...) -> dict:
    # Two upserts in one call (cheap).
    daily = await self._upsert_one(
        kind=WINDOW_KIND_DAILY, child_id, ...
    )
    weekly = await self._upsert_one(
        kind=WINDOW_KIND_WEEKLY, child_id, ...
    )
    return weekly  # or a {daily, weekly} dict
```

`get(child_id, window_kind="weekly")` 增加 window_kind 参数。

### 4. TreeholeManager 集成

```python
async def engage(...) -> ...:
    ...
    await self._agg_repo.increment(
        child_id=child_id, valence=emotion,
        intensity=intensity, category=category,
        engagement_at=datetime.utcnow(),
    )
    # ↑ 默认就写两窗口（增量内部 fan-out）
```

`aggregate_for_baseline(child_id, window_kind="weekly")` 加参数。

### 5. API

```
GET /treehole/aggregate?child_id=c-1&window=weekly  → 现有
GET /treehole/aggregate?child_id=c-1&window=daily   → 新增
```

### 6. 风险

| 风险 | 缓解 |
|------|------|
| 双窗口写入放大事务 | 单条 UPSERT 原子，2 条独立 UPSERT ≈ 2× 时间，可忽略 |
| 老 PK 数据迁移 | `window_kind` default 'weekly'，旧数据自动归 weekly |
| 前端忘了传 `window=` | 默认 weekly（向后兼容） |

### 7. 不在本工单

- ❌ monthly / quarterly —— 后续
- ❌ 时区切分（中国家长 vs 海外）—— 后续
- ❌ 周报模板里直接用 daily 数据 —— 后续工单（前端拉两份）