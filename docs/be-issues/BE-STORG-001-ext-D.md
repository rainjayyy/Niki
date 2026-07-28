# BE-STORG-001-ext-D：EmotionBaseline 持久化

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | STOR |
| **来源** | 同 BE-STORG-001-ext-B/C 模板 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-STOR-001-ext-B ✅ / BE-STOR-001-ext-C ✅ |
| **后续** | BE-MEDIA-001-rollout GET / BE-COMP-001 |

## 背景

EmotionBaseline (niki/agents/emotion.py) 持有 7 天滚动窗口的情绪记录：
- `_records: list[EmotionRecord]`
- `add()` 限制 30 天窗口
- `records()` 返列表

被多处使用：
- `niki/api/dashboard.py` — `/dashboard/baseline/add` 端点
- `niki/api/emotion.py` — `EmotionAgent` 默认 baseline
- `niki/agents/orchestrator.py` — `EmotionBaseline()` 实例

worker crash / 多实例部署会丢所有 7 天滚动窗口。本工单按 ext-B/C 模板接 Postgres。

## 验收标准（AC）

- [ ] AC-1：`EmotionBaselineRepository` ABC + `InMemory` + `Postgres`
- [ ] AC-2：ORM model `emotion_baseline_records`（含 date / valence / intensity / source）
- [ ] AC-3：`EmotionBaseline` 接受 repo（默认内存），所有 record 操作走 repo
- [ ] AC-4：`/api/v1/dashboard/baseline/add` 继续工作
- [ ] AC-5：tests/db/test_emotion_baseline_repo.py ≥ 5 个测试
- [ ] AC-6：现有 1077 passed 测试**零回归**

## 设计

### 1. Schema

```python
class EmotionBaselineRecord(Base):
    __tablename__ = "emotion_baseline_records"
    id: Mapped[int]  # PK
    date: Mapped[str]  # ISO YYYY-MM-DD
    valence: Mapped[float]
    intensity: Mapped[int]
    source: Mapped[str]  # "normal" | "treehole"
    created_at: Mapped[datetime]
```

### 2. Repository

```python
class EmotionBaselineRepository(ABC):
    @abstractmethod
    async def add(self, record) -> None: ...
    @abstractmethod
    async def list_all(self) -> list[dict]: ...  # all records, repo-side trim
    @abstractmethod
    async def list_in_window(self, since_days: int = 30) -> list[dict]: ...
```

### 3. 风险

| 风险 | 缓解 |
|------|------|
| 3 个 module-level baseline 单例在不同进程共享 | repo-based singleton |
| 现有 `_records` 30 天窗口过滤由 repo 负责 | 在 list_in_window 中按 created_at 过滤 |
| 现有测试用 sync EmotionBaseline.add | 改为 async |

## 后续工单

- **BE-MEDIA-001-rollout GET**：文件下载端点
- **BE-COMP-001**：PIPL 数据导出
