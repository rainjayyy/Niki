# BE-RT-002-rollout：告警 SSE 过滤与回放

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | REALTIME / SECURITY |
| **来源** | 前端对接文档 §2.4 推送策略矩阵 + §4.1 dashboard |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-RT-002 ✅（SSE 基建） |
| **后续** | Redis Streams 多进程缓冲（→ BE-RT-002-rollout-redis） |

## 背景

BE-RT-002 已上线 `GET /api/v1/alerts/stream/{child_id}` 端点。前端对接后报两个体验问题：

1. **没法过滤**：家长 dashboard 想做"只看 critical"或者"只看 lateral_auth_burst"，但 SSE 端点不接 query param，前端只能拿到全部告警后再在浏览器里过滤，浪费流量也浪费客户端算力。
2. **没回放**：家长打开 dashboard 时，只看到订阅之后的告警。订阅前那条 critical 自残信号在他关页面期间触发了，他**永远不会看到**。

后果：BE-RT-002 解决的"实时延迟"只覆盖了**打开页面的家长**。**关页面的家长仍然会错过关键告警**——尤其自残信号可能发生在夜间页面未打开的时段。

本工单加：
- **查询参数过滤**：`?alert_type=&severity=&since=` 三个维度 AND-ed
- **历史回放**：连接时先用 `event: replay` 把环形缓冲里最近 500 条里**匹配过滤器**的告警发一遍，前端据此渲染"历史"而不是"新推送"

## 验收标准（AC）

- [ ] AC-1：`AlertBroker.recent_alerts(parent_id, *, alert_type, severity, since, limit)` 返回最近 N 条匹配的告警（按时间正序）
- [ ] AC-2：`AlertBroker` 维护每家长环形缓冲（默认 500 条），新告警发布时入缓冲；缓冲满时丢弃最旧的
- [ ] AC-3：`GET /api/v1/alerts/stream/{child_id}` 接受可选 query：`alert_type` / `severity` / `since`
- [ ] AC-4：连接时先发 `event: replay` 把缓冲里匹配过滤的告警发一遍（不是 `event: alert`）
- [ ] AC-5：replay 完之后进入正常 live 流，新的告警按 `event: alert` 推
- [ ] AC-6：过滤器同时作用于 replay 与 live（新告警如果不匹配过滤器，不推送给当前订阅者）
- [ ] AC-7：`ready` event 携带 `filters` 字段，前端可以校验服务端实际生效的过滤
- [ ] AC-8：测试覆盖 ≥ 6 个，含：`recent_alerts` 三种过滤 / SSE replay 发出 / live 流衔接 / 缓冲 500 上限
- [ ] AC-9：现有 1523 passed 测试**零回归**

## 设计

### 1. AlertBroker 环形缓冲

`niki/realtime/alert_broker.py` 加内部状态：

```python
self._buffers: dict[str, deque[SecurityAlert]] = {}
# deque(maxlen=REPLAY_BUFFER_CAP)  # 默认 500
```

发布时同步入缓冲：

```python
def publish(self, alert):
    self._push_to_buffer(target, alert)  # 先入缓冲
    for q in subs:                        # 再扇出到订阅者
        q.put_nowait(alert)
```

缓冲在 `publish` 时**同步**写入，所以"在订阅的同时发布"也不会丢——后续 connect 时通过 `recent_alerts` 一定能拿到。

#### `recent_alerts(parent_id, *, alert_type=None, severity=None, since=None, limit=50)`

```python
def recent_alerts(self, parent_id, *, alert_type=None, severity=None,
                  since=None, limit=50) -> list[SecurityAlert]:
    """返回最近 N 条匹配过滤的告警，按时间正序（oldest-first）。

    - alert_type: 精确匹配
    - severity:   精确匹配
    - since:      只取 detected_at >= since
    - limit:      返回上限（默认 50），超出时从最新端截断
    """
```

线程安全：复用现有 `self._lock` 锁（同一进程内 publisher + subscriber 不冲突）。

### 2. SSE 端点 query param 过滤

`niki/api/alerts.py`：

```python
@router.get("/stream/{child_id}")
async def stream_alerts(
    child_id: str = Depends(get_owned_child_id),
    parent: ParentAccount = Depends(get_current_parent),
    alert_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    since: str | None = Query(default=None),
):
    since_dt = _parse_since(since)  # ISO-8601 → aware UTC
    queue = broker.subscribe(parent.id)

    async def event_stream():
        yield _format("ready", {...过滤器回显...})
        # 回放
        for buffered in broker.recent_alerts(
            parent.id,
            alert_type=alert_type, severity=severity, since=since_dt,
        ):
            yield _format("replay", buffered.to_dict())
        # live
        while True:
            alert = await asyncio.wait_for(queue.get(), timeout=30)
            # live 也走同样的过滤
            if alert_type is not None and alert.alert_type != alert_type:
                continue
            if severity is not None and alert.severity != severity:
                continue
            if since_dt is not None and alert.detected_at < since_dt:
                continue
            yield _format("alert", alert.to_dict())
```

`since` 解析：接受 `2026-07-30T12:34:56Z` / `2026-07-30T12:34:56+00:00` / 无时区（按 UTC 解读）。解析失败 → 422。

### 3. 客户端契约

```javascript
const es = new EventSource('/api/v1/alerts/stream/c-1?severity=critical');
es.addEventListener('ready', e => { ... });
es.addEventListener('replay', e => { renderHistory(JSON.parse(e.data)); });
es.addEventListener('alert', e => { renderNewPush(JSON.parse(e.data)); });
es.addEventListener('heartbeat', e => { ... });
```

前端可以靠 `event` 名区分"历史"和"新推送"，决定是否触发手机震动 / 声音。

### 4. 风险

| 风险 | 缓解 |
|------|------|
| 多 worker 部署下缓冲只在一个进程内 | 单进程有效；多 worker 需 Redis Streams（→ BE-RT-002-rollout-redis） |
| 缓冲无限增长 | deque(maxlen=500) 硬上限；超出静默丢弃 |
| 客户端断网期间错过 alert | 缓冲最多覆盖最近 500 条 critical；超出范围需补 audit log 拉取 |
| `since` 解析失败 | 422 立刻拒；前端可提示 |
| `recent_alerts` 在缓冲很大时扫一遍 | 缓冲上限 500，O(N) 扫一次即可 |
| 回放期间新告警进入 `publish` | 先入缓冲再扇出，所以即便回放中来了新告警，要么在 replay 列表之后（live 流里推）要么在前（前端去重）；不会丢 |

### 5. 不在本工单

- ❌ Redis Streams 跨进程缓冲（→ BE-RT-002-rollout-redis）
- ❌ 拉取更老的历史（缓冲 500 上限是设计选择；超出的走 `GET /dashboard/alerts/{child_id}` 历史接口）
- ❌ 客户端按事件去重（前端自己处理）
- ❌ 告警风暴限流（同 actor 告警冷却 → backlog）

## 后续工单

- **BE-RT-002-rollout-redis**：把 `AlertBroker` 迁到 Redis Streams，多 worker 可共享缓冲与发布通道
- **BE-PUSH-003**：Web Push 通道，对应 PRD §2.4 高危告警的次级通道（与 SSE 并行）
- **BE-ADMIN-001**：Admin 实时告警看板（管理侧 dashboard）