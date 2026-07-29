# BE-SESSION-redis：会话状态 Redis 化（多 worker 前置）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | SESSION / INFRA |
| **来源** | 前端对接文档 §7.1.6 + 多 worker 部署前置 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-API-002-redis 限流（已上） |
| **后续** | session 跨设备同步 / 异地登录检测 → BE-SESSION-redis-rollout |

## 背景

`SessionLimiter`（`niki/session/limiter.py`）当前是**纯内存**实现：

```python
self._session_start: dict[str, datetime] = {}
self._session_last_activity: dict[str, datetime] = {}
self._soft_warned: set[str] = set()
self._ended: dict[str, ExitReason] = {}
```

后果（多 worker / 多 pod 部署）：
- Worker-1 给孩子发"5 分钟提醒" → Worker-2 不知道 → 孩子从 worker-2 继续玩，没有时限
- `is_exhausted` 走 worker-2 → 孩子继续玩 → 超过每日时长上限
- 家长按"暂停" → worker-1 收到 → worker-2 不知道 → 孩子照常玩

**业务影响**：60 分钟每日上限在 4-worker 部署下可被绕过到 240 分钟。

本工单把会话状态抽出 `SessionStore` ABC，多 worker 共享同一份（默认 in-memory 兼容；production 切 Redis）。

## 验收标准（AC）

- [ ] AC-1：`SessionStore` ABC 暴露 8 个方法（get/set/clear × 4 字段）
- [ ] AC-2：`InMemorySessionStore` 默认实现（dict + 锁）
- [ ] AC-3：`RedisSessionStore` 用 `redis.asyncio` + JSON 编码 + TTL（24h）
- [ ] AC-4：SessionLimiter 接受 `store: SessionStore` 注入，**默认行为不变**
- [ ] AC-5：Redis 不可达时**降级**到 in-memory（不 500）
- [ ] AC-6：Settings `session_storage_uri: str = "memory://"` 路由
- [ ] AC-7：测试覆盖 ≥ 8 个，含：roundtrip / TTL / 跨进程 simulate / 降级 / ABC 兼容
- [ ] AC-8：现有 1252 passed 测试**零回归**

## 设计

### 1. SessionStore ABC

```python
class SessionStore(ABC):
    # Session start
    def get_started_at(self, child_id: str) -> datetime | None
    def set_started_at(self, child_id: str, ts: datetime) -> None
    def clear_started_at(self, child_id: str) -> None

    # Last activity (idle timeout guard)
    def get_last_activity(self, child_id: str) -> datetime | None
    def set_last_activity(self, child_id: str, ts: datetime) -> None

    # Soft warning flag
    def is_soft_warned(self, child_id: str) -> bool
    def mark_soft_warned(self, child_id: str) -> None
    def clear_soft_warned(self, child_id: str) -> None

    # End reason
    def get_ended(self, child_id: str) -> str | None
    def set_ended(self, child_id: str, reason: str) -> None
    def clear_ended(self, child_id: str) -> None
```

### 2. RedisSchema

| Key | Value | TTL |
|-----|-------|-----|
| `niki:session:{child_id}:started_at` | ISO ts string | 24h |
| `niki:session:{child_id}:last_activity` | ISO ts string | 24h |
| `niki:session:{child_id}:soft_warned` | "1" | 24h |
| `niki:session:{child_id}:ended` | reason string | 24h |

TTL = 24h（每日窗口 + 缓冲）。过夜自动清理。

### 3. SessionLimiter 改造

```python
class SessionLimiter:
    def __init__(self, *, store: SessionStore | None = None, ...):
        self._store = store or get_session_store()
        # 移除所有 _session_start / _session_last_activity / _soft_warned / _ended 字段
```

公开 API 不变（保持向后兼容）。所有内部 `dict.get/set/del` 换成 `self._store.*`。

### 4. 降级

```python
class FallbackSessionStore(SessionStore):
    """Wrap a primary store (Redis); on failure, use fallback."""
    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback
        self._in_fallback = False
    
    def _try_primary(self, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            if not self._in_fallback:
                logger.warning("session_store.fallback", error=str(exc))
                self._in_fallback = True
            return self._fallback.__class__._method(self._fallback, *args)
```

类似 slowapi 的 `in_memory_fallback_enabled`，但不丢数据——降级期间新数据写 memory，恢复后下次操作重新尝试 Redis。

### 5. 风险

| 风险 | 缓解 |
|------|------|
| Redis 抖动 → 计数失准 | 降级到 memory（同进程内一致）；多 worker 仍共享 Redis |
| TTL 误删 → 当日累积时长丢失 | TTL = 24h，远超每日上限 60min |
| Key 命名冲突 | `niki:session:` 前缀 + 子键明确 |
| 时区错乱 | 一律 UTC + `aware` datetime |

### 6. 不在本工单

- ❌ 跨设备同步（同 parent_id 下多个 child_id）
- ❌ 异地登录检测
- ❌ session 历史持久化（只存当日活跃）