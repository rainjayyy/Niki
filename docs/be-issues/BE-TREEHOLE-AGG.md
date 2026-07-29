# BE-TREEHOLE-AGG：Treehole 聚合表

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | TREEHOLE / PERFORMANCE |
| **来源** | TreeholeManager.aggregate_for_baseline 全表解密性能问题 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-STORG-001-ext-B ✅ / BE-STORG-001-ext-D ✅ |
| **后续** | 多窗口聚合（daily / weekly）切换（→ BE-TREEHOLE-AGG-rollout） |

## 背景

`TreeholeManager.aggregate_for_baseline(child_id)` 当前实现：

```python
summaries = await self.list_summaries(child_id=child_id)  # decrypt every one
total = sum(valence_map[s.emotion_valence] * s.emotion_intensity / 5
            for s in summaries)
return {"mean_valence": total / len(summaries), "count": len(summaries)}
```

每次调用都要：
1. `repo.list_for_child(child_id)` 拿所有 engagement
2. AES-256-GCM **解密每条**（base64 + decrypt + JSON parse）
3. 累加

**性能问题**：
- 一个 7 岁孩子一周累计 50 次 treehole → 50 次 AES 解密 + 50 次 JSON 解析
- Dashboard 每次刷新 / 每周报告生成都触发 → P99 高
- 7 天保留期过后 engagement 已被 purge，但聚合结果随 engagement 一起丢 → **基线信号丢失**

PRD #64 「聚合即匿名」要求聚合后无法反推单条 — 现有实现也满足，但**性能不达标**。

本工单引入 `treehole_aggregations` 增量聚合表：`engage()` 时即时增量，`aggregate_for_baseline()` 读 O(1)。

## 验收标准（AC）

- [ ] AC-1：新增 `treehole_aggregations` 表 + `TreeholeAggregation` ORM model
- [ ] AC-2：`TreeholeAggregationRepository` ABC + `InMemory` + `Postgres`
- [ ] AC-3：`increment()` 原子更新（Postgres 走 `RETURNING` + UPSERT）
- [ ] AC-4：`TreeholeManager.engage()` 写完 engagement 后**立即**调 `increment()`
- [ ] AC-5：`TreeholeManager.aggregate_for_baseline()` 读聚合表，O(1) 返回
- [ ] AC-6：聚合表删除子记录时（purge / child delete）级联删除聚合行
- [ ] AC-7：测试覆盖 ≥ 8 个，含：增量正确 / 多 child 独立 / reset / 跨窗口边界 / purge 后聚合保留
- [ ] AC-8：现有 1176 passed 测试**零回归**

## 设计

### 1. ORM model

```python
class TreeholeAggregation(Base):
    __tablename__ = "treehole_aggregations"
    child_id: Mapped[str]   # PK, FK children.id CASCADE
    window_start: Mapped[date]
    count: Mapped[int]                       # 总 engagement 数
    valence_sum: Mapped[float]               # Σ valence × intensity/5
    negative_count: Mapped[int]
    positive_count: Mapped[int]
    neutral_count: Mapped[int]
    intensity_sum: Mapped[float]
    category_counts: Mapped[dict]            # JSON: {family, school, self, other}
    first_engagement_at: Mapped[datetime]
    last_engagement_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

复合主键 `(child_id, window_start)`；每个 child 一个 7 天滚动窗口一行。

### 2. Repository

```python
class TreeholeAggregationRepository(ABC):
    async def increment(
        self, *, child_id: str,
        valence: str, intensity: int, category: str,
        engagement_at: datetime | None = None,
    ) -> dict:  # returns updated aggregate
        """Atomic upsert: INSERT ... ON CONFLICT DO UPDATE."""

    async def get(self, child_id: str) -> dict | None: ...

    async def reset(self, child_id: str, *, window_start: date) -> None: ...

    async def drop_all_for_child(self, child_id: str) -> int: ...
```

`InMemory` 用 dict 锁；`Postgres` 用 `INSERT ... ON CONFLICT (child_id, window_start) DO UPDATE SET ... RETURNING *`。

### 3. TreeholeManager 集成

```python
class TreeholeManager:
    def __init__(self, ..., agg_repo: TreeholeAggregationRepository | None = None):
        self._agg_repo = agg_repo or get_treehole_aggregation_repository()

    async def engage(self, ..., emotion, intensity, category):
        # ... existing encryption + repo.add() ...
        # NEW: after successful repo.add(), bump the aggregate
        await self._agg_repo.increment(
            child_id=child_id,
            valence=emotion,
            intensity=intensity,
            category=category,
            engagement_at=datetime.utcnow(),
        )
        return summary, encrypted

    async def aggregate_for_baseline(self, child_id=None):
        if child_id is None:
            return {"mean_valence": 0.0, "count": 0}
        agg = await self._agg_repo.get(child_id)
        if not agg or agg["count"] == 0:
            return {"mean_valence": 0.0, "count": 0}
        return {
            "mean_valence": agg["valence_sum"] / agg["count"],
            "count": agg["count"],
        }
```

### 4. Window 切换

聚合表 row 由 `(child_id, window_start)` 唯一确定。本期**固定 7 天滚动**：
- 新 engage 进入当前 7 天窗口 → 同 row 更新
- 旧 engagement 过期被 purge → 不影响聚合表（聚合行继续存在）

后续工单支持多窗口（daily / weekly）切换。

### 5. 风险

| 风险 | 缓解 |
|------|------|
| aggregate 写失败影响主链路 | try/except 包住 increment，失败仅 log error |
| 增量漏算（服务重启丢 in-memory 增量） | Postgres 走真正原子 UPSERT；in-memory 用 WAL-style 先写 engagement 再写 aggregate |
| 旧测试依赖 list_summaries+计算 | 保留旧路径，加 `aggregate_for_baseline()` fast path，测试改 mock |
| 删除 child 时聚合行残留 | FK ON DELETE CASCADE |

### 6. 不在本工单

- ❌ 多窗口（daily / weekly）→ 后续
- ❌ 跨 child 聚合（全平台平均）→ 后续
- ❌ 异步落盘（Kafka）→ 后续
- ❌ 历史窗口归档 → DBA 流程

## 后续工单

- **BE-TREEHOLE-AGG-rollout**：多窗口（daily + weekly）切换
- **BE-TREEHOLE-AGG-platform**：跨 child 平台均值
- **BE-TREEHOLE-AGG-siem**：增量事件流到 ELK（实时仪表盘）