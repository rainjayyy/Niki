# BE-STORG-001-ext-C：DashboardService 持久化

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | STOR |
| **来源** | `docs/frontend-integration.md` §7.4 P1 |
| **工作量** | 1-2 天（照 ext-B 模板） |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-STOR-001 ✅ / BE-STORG-001-ext-B ✅ |
| **后续** | BE-MEDIA-001-rollout GET / BE-COMP-001 |

## 背景

§7.4 P1 列表里：

> BE-STORG-001-ext-C | DashboardService 同样问题，周报会丢 | 1-2 天

DashboardService 当前 5 个 in-memory 列表：
- `_safety_valve_alerts: list[SafetyValveAlert]`
- `_high_risk_alerts: list[HighRiskAlert]`
- `_tutoring_events: list[dict]`
- `_knowledge_queries: list[dict]`
- `_baseline_records: list[EmotionRecord]` (delegated to `EmotionBaseline`)

多实例部署 / worker crash 都会丢所有告警 + 周报输入。本工单按 ext-B 模板，把这 4 个（EmotionBaseline 留作单独工单）持久化。

## 验收标准（AC）

- [ ] AC-1：`DashboardRepository` ABC + `InMemoryDashboardRepository` 默认 + `PostgresDashboardRepository` 生产
- [ ] AC-2：3 个新 ORM model：`DashboardAlert`、`TutoringEvent`、`KnowledgeQuery`（`weekly_reports` 已有）
- [ ] AC-3：`DashboardService` 接受 repo（默认内存），所有 record/list 方法走 repo
- [ ] AC-4：`/api/v1/dashboard/*` 全部端点继续工作
- [ ] AC-5：`pytest tests/db/test_dashboard_repo.py` ≥ 6 个测试
- [ ] AC-6：现有 1070 passed 测试**零回归**
- [ ] AC-7：EmotionBaseline 持久化 → BE-STORG-001-ext-D（独立工单）

## 设计

### 1. Schema（新增 3 个 model）

```python
class DashboardAlert(Base):  # safety_valve + high_risk 合一表
    __tablename__ = "dashboard_alerts"
    id: Mapped[str]
    alert_type: Mapped[str]  # "safety_valve" | "high_risk"
    child_id: Mapped[str]
    parent_id: Mapped[str]
    payload: Mapped[dict]            # JSONB
    created_at: Mapped[datetime]
    delivered_at: Mapped[datetime | None]


class TutoringEvent(Base):
    __tablename__ = "tutoring_events"
    id: Mapped[str]
    child_id: Mapped[str]
    payload: Mapped[dict]            # JSONB
    created_at: Mapped[datetime]


class KnowledgeQuery(Base):
    __tablename__ = "knowledge_queries"
    id: Mapped[str]
    child_id: Mapped[str]
    payload: Mapped[dict]
    created_at: Mapped[datetime]
```

### 2. Repository 抽象

```python
class DashboardRepository(ABC):
    @abstractmethod
    async def add_alert(alert_type, child_id, parent_id, payload) -> str: ...
    @abstractmethod
    async def list_alerts(alert_type=None, child_id=None) -> list[dict]: ...
    @abstractmethod
    async def mark_alert_delivered(alert_id) -> bool: ...
    @abstractmethod
    async def add_tutoring_event(child_id, payload) -> str: ...
    @abstractmethod
    async def list_tutoring_events(child_id) -> list[dict]: ...
    @abstractmethod
    async def add_knowledge_query(child_id, payload) -> str: ...
    @abstractmethod
    async def list_knowledge_queries(child_id) -> list[dict]: ...
```

### 3. 风险

| 风险 | 缓解 |
|------|------|
| 4 个新表 + weekly_reports 已有 = 12 张表 | 测试 metadata 更新 EXPECTED_TABLES |
| alerts 复合类型（safety_valve + high_risk）shape 不同 | 通用 payload 字段 + alert_type discriminator |
| 跨实例 alert_id 唯一性 | UUID 足够 |
| 测试 fixtures 共享状态 | 显式 `set_dashboard_repository(repo)` per test |

## 后续工单

- **BE-STORG-001-ext-D**：`EmotionBaseline` 持久化
- **BE-MEDIA-001-rollout**：GET /media/{id}/raw
- **BE-COMP-001**：数据导出 PIPL 合规