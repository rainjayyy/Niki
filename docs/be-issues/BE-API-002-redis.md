# BE-API-002-redis：多实例部署限流（Redis storage）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | INFRASTRUCTURE / API |
| **来源** | BE-API-002 后续 + 多 worker 部署前置 |
| **工作量** | 半天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-API-002 ✅ |
| **后续** | 同 IP 多 actor 检测（→ BE-AUTH-001-rollout-alerts-rollout） |

## 背景

BE-API-002 用 slowapi 给 8 个端点加了 `@limiter.limit("N/minute")`，**默认是 in-memory storage**。

后果（多 worker 部署下）：
- 4 个 uvicorn worker × 20/min 上传限制 → 实际允许 80 次/分钟（绕过限流 4 倍）
- 家长 A 在 worker-1 用完配额 → 切到 worker-2 又能刷 20 次
- 攻击者无成本：每个 worker 单独计 10 次 → 实际 40 次/分钟
- rate tracker（BE-AUTH-001-rollout-alerts）也是 in-memory → 多 worker 阈值失真

本工单让限流**走 Redis**（生产已部署 `redis://localhost:6379/0`），多个 worker 共享一份计数器。

## 验收标准（AC）

- [ ] AC-1：新增 `rate_limit_storage_uri` 配置项（默认 `memory://`）
- [ ] AC-2：当配置为 `redis://...` 时，slowapi Limiter 自动用 Redis backend
- [ ] AC-3：`in_memory_fallback_enabled=True` —— Redis 不可达时降级到 in-memory（不阻断请求）
- [ ] AC-4：env var `NIKI_RATE_LIMIT_STORAGE_URI` 可覆盖
- [ ] AC-5：现有 1166 passed 测试**零回归**
- [ ] AC-6：新增测试覆盖：URI 解析 / 默认值 / Redis 故障降级（用 fakeredis 或 mock）
- [ ] AC-7：Health endpoint 报告当前限流 backend 类型

## 设计

### 1. Settings

```python
class Settings(BaseSettings):
    # BE-API-002-redis: shared rate-limit counter backend.
    # Default keeps current in-memory behaviour for tests / dev.
    # Production: set NIKI_RATE_LIMIT_STORAGE_URI=redis://...
    rate_limit_storage_uri: str = "memory://"
```

### 2. Limiter 构造

```python
def _build_limiter() -> Limiter:
    settings = get_settings()
    return Limiter(
        key_func=get_remote_address,
        headers_enabled=False,
        strategy="fixed-window",
        storage_uri=settings.rate_limit_storage_uri,
        # BE-API-002-redis: fall back to in-memory if Redis is
        # unreachable, instead of failing every request.
        in_memory_fallback_enabled=True,
    )
```

slowapi 0.1.10 已原生支持这两个参数。

### 3. 健康降级语义

```
限流请求进来
  ↓
slowapi 检查 storage
  ↓ (Redis 可达)
记数 + 检查 → 命中 / 通过
  ↓ (Redis 不可达)
log warning, switch to in-memory for rest of process
  ↓
记数 + 检查 → 命中 / 通过
```

**不会**因为 Redis 抖动就 500 整个服务。

### 4. 健康检查 endpoint

现有 `/health` 已包含 redis check。本工单加 `rate_limit_backend` 字段：

```json
{
  "redis": {"status": "healthy", "latency_ms": 0.5},
  "rate_limit_backend": "redis",
  "rate_limit_in_fallback": false,
  ...
}
```

### 5. 测试

由于真实 Redis 在测试环境不可用，用两种方案：
1. `fakeredis` 提供 in-process Redis 兼容
2. 单测构造 Limiter 时验证参数传递正确；集成测手动验 fakeredis

### 6. 风险

| 风险 | 缓解 |
|------|------|
| Redis 抖动 → 全员 500 | `in_memory_fallback_enabled=True` |
| Redis 慢 → P99 飙升 | slowapi 是同步调用，慢请求会塞 event loop |
| 切换 Redis 后旧 in-memory 数据丢失 | 接受 —— 计数器本来就是临时的 |
| Redis Key 命名冲突 | slowapi 默认 key 前缀 `LIMITER/`，足够区分 |

### 7. 不在本工单

- ❌ fakeredis 引入（开发依赖） —— 直接 mock slowapi 的 storage 接口
- ❌ 限流 dashboard（→ BE-ADMIN-001）
- ❌ 按用户（而非 IP）限流 —— 当前 key_func=get_remote_address 保持

## 后续工单

- **BE-AUTH-001-rollout-alerts-rollout**：Redis 共享 rate tracker
- **BE-API-002-redis-rollout**：生产 Redis 切换 + 监控 dashboard
- **BE-ADMIN-001**：限流实时看板