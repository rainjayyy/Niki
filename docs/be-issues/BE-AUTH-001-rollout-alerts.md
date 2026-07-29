# BE-AUTH-001-rollout-alerts：横向越权告警

| 字段 | 值 |
|------|-----|
| **优先级** | 🟠 P0（安全响应） |
| **域** | AUTH / SECURITY |
| **来源** | PRD §6.4 + BE-AUTH-001-rollout-audit 后续 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-AUTH-001-rollout-audit ✅ |
| **后续** | 飞书 / 邮件双通道（→ BE-AUTH-001-rollout-alerts-rollout） |

## 背景

BE-AUTH-001-rollout-audit 把 lateral-auth 违规写进 `auth_audit_logs` 表。但**光记不发，安全团队半夜仍然收不到信号**。

真实威胁场景：
- 凌晨 3 点，攻击者用 1 个家长账号试探性访问其他家长数据 → 每次都 403 → 没人知道
- 同一攻击者用 50 个 IP 试同一孩子的 treehole → 安全团队错过早期信号
- 真实家长账号被盗窃，10 分钟内连续访问 5 个不属于自己的 child → 应该立即告警

本工单给 audit log 加**实时触发告警**：阈值触发 → 多通道分发（默认控制台 + structlog critical，生产可挂 webhook）。

## 验收标准（AC）

- [ ] AC-1：定义 `SecurityAlert` dataclass + `AlertDispatcher` ABC
- [ ] AC-2：`ConsoleAlertDispatcher` 默认实现（structlog.critical）
- [ ] AC-3：`WebhookAlertDispatcher` httpx 异步 POST 到配置 URL
- [ ] AC-4：`CompositeAlertDispatcher` 扇出到多个 sink
- [ ] AC-5：`ViolationRateTracker` 滑动窗口追踪 actor 越权频次
- [ ] AC-6：阈值 `3 violations / 5 min` 触发 `lateral_auth_burst` CRITICAL
- [ ] AC-7：`AuthAuditLogger` 写 audit 后自动调用 rate tracker + 触发告警
- [ ] AC-8：阈值配置走 `Settings`（`auth_alert_burst_count`、`auth_alert_window_seconds`、`auth_alert_webhook_url`）
- [ ] AC-9：测试覆盖 ≥ 8 个，含：阈值触发 / 阈值未触发 / 滑出窗口 / 多通道 / 告警失败不阻塞 / webhook URL 未配置
- [ ] AC-10：现有 1137 passed 测试**零回归**

## 设计

### 1. 告警对象

```python
@dataclass(frozen=True)
class SecurityAlert:
    alert_type: str               # lateral_auth_burst | multi_actor_same_ip | ...
    severity: str                 # warning | critical
    actor_parent_id: str | None
    target_parent_id: str | None
    target_child_id: str | None
    client_ip: str | None
    event_count: int
    window_seconds: int
    detected_at: datetime
    title: str                    # 短句，UI / IM 标题
    detail: str                   # 详情，多行
```

### 2. AlertDispatcher ABC

```python
class AlertDispatcher(ABC):
    @abstractmethod
    async def dispatch(self, alert: SecurityAlert) -> None: ...

class ConsoleAlertDispatcher(AlertDispatcher):
    """默认实现 — structlog.critical，把告警推到 stdout / ELK / Loki。"""

class WebhookAlertDispatcher(AlertDispatcher):
    """生产实现 — httpx POST 到配置的 webhook URL。
    失败仅 structlog.error，不抛（告警通道挂了不影响主流程）。"""

class CompositeAlertDispatcher(AlertDispatcher):
    """扇出到多个 sink；任意一个抛错不影响其它。"""
```

### 3. ViolationRateTracker

```python
class ViolationRateTracker:
    """滑动窗口：记录每个 actor_parent_id 最近 N 次违规时间戳。
    单 actor 5 分钟内 ≥ 3 次 → 触发告警。"""

    def __init__(self, *, window_seconds: int, burst_count: int): ...
    def record(self, actor_parent_id: str, *, now: datetime | None = None) -> int:
        """记录一次违规；返回当前窗口内的累计次数（剪枝过期）。"""
    def should_alert(self, actor_parent_id: str) -> bool:
        """当前窗口内次数是否 >= burst_count。"""
```

线程安全：用 `threading.Lock` 保护内部 dict + deque。

### 4. 阈值规则（本期）

| 规则 | 触发条件 | alert_type | severity |
|------|---------|-----------|----------|
| 单 actor 频次 | 5 分钟内 ≥ 3 次违规 | `lateral_auth_burst` | critical |

后续工单加：
- 单 actor 1 小时内 ≥ 10 次
- 同 client_ip 关联多 actor
- 同 target_child_id 关联多 actor

### 5. 与 AuthAuditLogger 集成

```python
class AuthAuditLogger:
    async def _safe_add(self, event):
        # 1. structlog
        log_fn(...)
        # 2. repo.add
        await self.repo.add(event)
        # 3. NEW: 触发检查（仅对 violation 事件）
        if event.severity == SEVERITY_WARNING:
            await self._maybe_alert(event)
    
    async def _maybe_alert(self, event):
        if event.actor_parent_id is None:
            return
        count = self._rate_tracker.record(event.actor_parent_id)
        if count >= self._rate_tracker.burst_count:
            await self._dispatcher.dispatch(SecurityAlert(
                alert_type="lateral_auth_burst",
                severity="critical",
                actor_parent_id=event.actor_parent_id,
                event_count=count,
                window_seconds=self._rate_tracker.window_seconds,
                ...
            ))
```

### 6. Settings

```python
class Settings(BaseSettings):
    # BE-AUTH-001-rollout-alerts
    auth_alert_burst_count: int = 3              # 5 分钟内 ≥ 3 次
    auth_alert_window_seconds: int = 300         # 滑动窗口
    auth_alert_webhook_url: str = ""             # 空 = 不发 webhook
    auth_alert_webhook_timeout_s: int = 5
```

### 7. 风险

| 风险 | 缓解 |
|------|------|
| 告警风暴（同一攻击者一次触达 1000 次） | dispatcher 用 token bucket + 同 actor 告警冷却 1 分钟（→ 后续） |
| Webhook 慢导致请求 P99 飙升 | `httpx.AsyncClient(timeout=5)` + 失败不阻塞主流程 |
| 误报：合法家长多次输错 child_id | 5 分钟 3 次是合理阈值；可调 |
| 多实例下阈值不准确 | 单进程 InMemory；多 worker 需 Redis（→ BE-API-002-redis 后续） |
| Audit 写失败已经把异常吞掉 | 告警独立于 audit 写入路径，但同样 try/except |

### 8. 不在本工单

- ❌ Redis 共享阈值（→ BE-API-002-redis）
- ❌ 同 IP 多 actor 检测（→ BE-AUTH-001-rollout-alerts-rollout）
- ❌ 飞书 / 邮件双通道（→ BE-AUTH-001-rollout-alerts-rollout）
- ❌ 同 actor 告警冷却（防风暴） — 列入 backlog
- ❌ Admin 看板（→ BE-ADMIN-001）

## 后续工单

- **BE-AUTH-001-rollout-alerts-rollout**：飞书 + 邮件双通道 + 同 IP 多 actor
- **BE-API-002-redis**：多实例共享阈值
- **BE-ADMIN-001**：实时告警看板