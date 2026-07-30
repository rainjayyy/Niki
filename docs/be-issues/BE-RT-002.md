# BE-RT-002：高危告警 SSE 实时推送

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | REALTIME / SECURITY |
| **来源** | 前端对接文档 §2.4 推送策略矩阵 + §4.1 dashboard |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-RT-001 ✅（SSE 基建）/ BE-AUTH-001-rollout-alerts ✅（越权告警） |
| **后续** | Web Push 通道 → BE-PUSH-003 / 飞书机器人 → BE-PUSH-002 |

## 背景

PRD §2.4 推送策略矩阵要求 **高危告警走 SSE，延迟 < 5s**（PRD #29 自残/自伤信号）。但当前：
- BE-AUTH-001-rollout-alerts 检测到越权 → 触发 SecurityAlert → 走 webhook / email
- **没有**任何路径把告警实时推到家长 dashboard
- 家长只能**轮询** `GET /dashboard/alerts/{child_id}` 才能看到新告警

后果：自残信号触发后 30s+ 家长才看到 → 错失最佳干预窗口。

本工单加 **SSE 端点** `GET /api/v1/alerts/stream/{child_id}`，把 SecurityAlert **实时**推给订阅的家长客户端。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/alerts/stream/{child_id}` 是 SSE 端点
- [ ] AC-2：家长只能订阅自己孩子的告警（lateral-auth 检查）
- [ ] AC-3：订阅后，新 SecurityAlert 自动通过 SSE 推送给该家长
- [ ] AC-4：心跳（每 30s 一条 `: heartbeat`）让浏览器知道连接还活着
- [ ] AC-5：多个家长独立订阅（一个家长不会收到其他家长的告警）
- [ ] AC-6：测试覆盖 ≥ 5 个，含：lateral-auth 拒绝 / 单家长订阅 / 多家长隔离 / 心跳 / 异常断开
- [ ] AC-7：现有 1331 passed 测试**零回归**

## 设计

### 1. AlertBroker

新增 `niki/realtime/alert_broker.py`：

```python
class AlertBroker:
    """Process-wide singleton: subscribes to SecurityAlert
    events, fans out to per-parent SSE connections."""
    
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}  # parent_id → queues
    
    def subscribe(self, parent_id: str) -> asyncio.Queue:
        q = asyncio.Queue()
        self._subscribers.setdefault(parent_id, set()).add(q)
        return q
    
    def unsubscribe(self, parent_id: str, q):
        self._subscribers[parent_id].discard(q)
    
    def publish(self, alert: SecurityAlert):
        for q in self._subscribers.get(alert.actor_parent_id, []):
            q.put_nowait(alert)
```

**关键设计**：订阅按 `parent_id` 隔离。SecurityAlert 必须带 `actor_parent_id`（已有，BE-AUTH-001-rollout-alerts 触发）。这样家长 A 只收到 A 自己的告警。

### 2. 接入点

`AuthAuditLogger._dispatch()` 触发 SecurityAlert 时，调 `AlertBroker.publish(alert)`。这样：
- 所有触发路径（webhook / email / broker）都过同一个 `_dispatch`
- 不需要改 BE-AUTH-001-rollout-alerts 的代码

### 3. SSE 端点

```python
@router.get("/alerts/stream/{child_id}")
async def stream_alerts(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
):
    queue = broker.subscribe(parent.id)
    try:
        # 1) Send a "ready" event
        yield {"event": "ready", "data": "..."}
        # 2) Heartbeat + queue drain loop
        while True:
            try:
                alert = await asyncio.wait_for(queue.get(), timeout=30)
                yield {"event": "alert", "data": alert.to_dict()}
            except asyncio.TimeoutError:
                yield {"event": "heartbeat", "data": "{}"}
    finally:
        broker.unsubscribe(parent.id, queue)
```

### 4. 风险

| 风险 | 缓解 |
|------|------|
| 客户端断网 → broker 队列堆积 | 队列无 maxsize，ASGI server 会卡住 → 限 100 + 溢出丢弃 |
| 多 worker 部署 → broker 进程隔离 | 单进程内 broker OK；多 worker 需 Redis pub/sub → 后续工单 |
| 客户端 poll 频率过高 | heartbeat 30s 防止 proxy 切断空闲连接 |

### 5. 不在本工单

- ❌ 多 worker broker（→ 后续 Redis pub/sub）
- ❌ WebSocket（与 SSE 选其一，PRD 已选 SSE）
- ❌ 历史告警回放（首次连接时 replay 最近 N 条）