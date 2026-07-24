# BE-STOR-002：连接保活（Background Heartbeat + Unified Health）

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0 |
| **域** | STOR |
| **来源** | `docs/frontend-integration.md` §7.1.6 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-STOR-001 ✅ |
| **后续** | BE-API-001 统一响应 |

## 背景

当前 4 个外部依赖（Redis / Postgres / Chroma / Neo4j）只在 lifespan 启动时连一次，之后**无任何后台心跳**。后果：

1. **长跑后断连** —— Neo4j session 默认 1 小时空闲断；容器内 Redis 偶尔 idle close
2. **`/health` 端点浪费** —— 每次 health check 都新连，对 Prometheus/scraper 不友好
3. **故障感知延迟** —— 上游抖动后，业务请求第一次撞墙才发现

各客户端当前 keep-alive 状态：

| 客户端 | 现有保活 | 缺口 |
|--------|---------|------|
| Postgres | `pool_pre_ping=True` | ✓ 已自动 |
| Redis | `socket_keepalive=True` + `health_check_interval=30` | ✓ 已自动 |
| Chroma | 无 | ⚠️ HTTP 客户端无自动心跳 |
| Neo4j | 无 `keep_alive=True` | ⚠️ 长跑 session 会失效 |

## 验收标准（AC）

- [ ] AC-1：`HeartbeatService` 类，支持 `register(name, ping_fn, interval)` / `start()` / `stop()`
- [ ] AC-2：每个后台任务独立 `try/except`，单点失败不传播
- [ ] AC-3：保留最近一次 ping 结果 `(status, latency_ms, timestamp)`，可查询 `aggregate_health()`
- [ ] AC-4：`main.py` lifespan 启动时 spawn 心跳任务，shutdown 时 cancel
- [ ] AC-5：Neo4j driver 加 `keep_alive=True`（小修补）
- [ ] AC-6：`pytest tests/infrastructure/test_heartbeat.py -v` 全绿（至少 6 个测试）
- [ ] AC-7：现有 854 passed 测试**零回归**

## 设计

### 1. 模块布局

```
niki/infrastructure/
├── heartbeat.py       # NEW — HeartbeatService + HealthSnapshot
├── redis.py            # existing
├── postgres.py         # existing
├── chroma.py           # existing
└── neo4j.py            # existing — add keep_alive=True
```

### 2. HeartbeatService API

```python
service = HeartbeatService()
service.register("postgres", PostgresClient.health_check, interval_seconds=30)
service.register("redis",    RedisClient.health_check,    interval_seconds=30)
service.register("chroma",   ChromaClient.health_check,   interval_seconds=60)
service.register("neo4j",    Neo4jClient.health_check,    interval_seconds=30)

await service.start()
# ... app running ...
await service.stop()

snapshot = service.aggregate_health()
# {"postgres": {"status": "healthy", "latency_ms": 1.2, ...}, ...}
```

### 3. 风险

| 风险 | 缓解 |
|------|------|
| 后台任务 leak | `stop()` 时 cancel all + 等待 done |
| 频繁 ping 把外部打挂 | interval 默认 30s，可配 |
| 单点失败影响其他 | 独立 try/except + 结构化日志 |
| 测试 race condition | 用 `asyncio.Event` 同步，注入 fast/slow ping |

## 不在本工单

- ❌ 改 `/health` 端点的返回（保持向后兼容）
- ❌ Prometheus 指标（→ BE-API-009 P2）
- ❌ 自动重连业务逻辑（Postgres 已经 lazy init）

## 后续工单

- **BE-API-001**：把 5 种返回模式统一为 APIResponse（基线 BE-STOR-002 上的小重构）
- **BE-API-009**：Prometheus `/metrics` 端点（消费 HeartbeatService 数据）
