# BE-API-009：Prometheus 指标

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API / Observability |
| **来源** | `docs/frontend-integration.md` §7.3.5 |
| **工作量** | 0.5 天 |
| **状态** | ✅ 完成（2026-07-31） |
| **依赖** | 现有 FastAPI 应用 + `prometheus-client>=0.20.0` |

## 背景

§7.3.5 列出：

> ❌ BE-API-009 — Prometheus 指标 — 缺失，`/metrics` 不存在 —
> `prometheus-fastapi-instrumentator`；自定义 Counter：`chat_total`、
> `safety_block_total` — 0.5 天

痛点：
- 线上 `chat_stream` / `safety` / `auth` / `push` / `webhook` 各自刷自己的 structlog，
  但运维只能凭日志手撕 grep 算 QPS / 错误率
- 没有 latency 直方图，`p95` / `p99` 全靠前端埋点估算
- 没有「拦截率」可见性 —— safety 触发频次只能从审计表 `SELECT COUNT`，
  无法做实时熔断 / 阈值告警

本工单接入 `prometheus-client`（非 `prometheus-fastapi-instrumentator` —— 后者
强制装 `prometheus_client` 但带来额外的 Starlette middleware + 全局默认 registry，
跟我们"测试隔离 + 单实例复用"的诉求冲突），提供 `GET /metrics` + 11 个自定义
Counter / Histogram / Gauge。

## 验收标准（AC）

- [x] AC-1：`GET /api/v1/metrics` 返回 Prometheus 文本格式（`text/plain; version=0.0.4`）
- [x] AC-2：定义 11+ 个自定义指标（12 个：11 Counter + 1 Histogram + 1 Gauge）
- [x] AC-3：至少 4 条 hot path 真的调用了 Counter（chat_stream / safety /
      auth_audit / auth login success+failure / rate_limit 429）
- [x] AC-4：HTTP latency histogram 覆盖全部 endpoint
- [x] AC-5：测试 ≥ 12 个，全部通过
- [x] AC-6：零回归（其余测试通过）
- [x] AC-7：`prometheus-client>=0.20.0` 已加入 `pyproject.toml`

## 设计

### 1. 模块边界

```
niki/metrics.py           # 单一 source of truth —— 所有 Counter/Histogram/Gauge
                          #   注册到一个 process 级 CollectorRegistry
                          #   （不用 prometheus_client.REGISTRY 默认 registry，
                          #    避免测试间污染）

niki/api/metrics.py       # FastAPI router —— 暴露 GET /metrics
                          #   走 generate_latest(registry) + Response(text/plain)

niki/middleware/metrics.py# BaseHTTPMiddleware —— 每次请求观测
                          #   niki_http_request_duration_seconds{method,endpoint}

niki/middleware/rate_limit.py  # 复用已有 429 处理器：在那里 +1 RATE_LIMITED_TOTAL
```

### 2. 指标清单

| 名称 | 类型 | Labels | 说明 |
|------|------|--------|------|
| `niki_chat_total` | Counter | route, provider, mode | 每次 `/chat/stream` 入口 +1 |
| `niki_safety_block_total` | Counter | reason, severity | safety 拦截触发 +1 |
| `niki_high_risk_total` | Counter | alert_type | high-risk alert 发出 +1 |
| `niki_sms_sent_total` | Counter | provider, status | SMS 发送成功/失败 +1 |
| `niki_login_success_total` | Counter | method | 登录成功 +1（sms / password / ...） |
| `niki_login_failure_total` | Counter | method, reason | 登录失败 +1 |
| `niki_push_delivery_total` | Counter | channel, status | push 投递成功/失败 +1 |
| `niki_ocr_requests_total` | Counter | status | OCR 请求 +1 |
| `niki_rate_limited_total` | Counter | endpoint | 429 响应 +1 |
| `niki_webhook_deliveries_total` | Counter | event_type, status | webhook 投递 +1 |
| `niki_http_request_duration_seconds` | Histogram | method, endpoint | 每个 HTTP 请求观测 |
| `niki_auth_audit_total` | Counter | event_type, severity | auth 审计事件 +1 |
| `niki_active_parents` | Gauge | — | 最近 5 分钟活跃家长数（v1 留 0） |

### 3. Registry 隔离策略

```python
# niki/metrics.py
_REGISTRY: CollectorRegistry | None = None

def get_registry() -> CollectorRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CollectorRegistry()
        _register_all(_REGISTRY)
    return _REGISTRY

def reset_for_tests() -> None:
    """Drop the singleton so the next get_registry() builds a fresh one."""
    global _REGISTRY
    _REGISTRY = None
```

关键点：
- **不用** `prometheus_client.REGISTRY` —— 默认 registry 是 process 级全局，
  pytest 多 worker + 同进程多测试串行会互相污染计数。
- 测试之间调 `reset_for_tests()`，下一次 `get_registry()` 重新建一个空
  `CollectorRegistry` + 重新注册同名 metric —— 但因为 `Counter('niki_chat_total', ...)`
  在新 registry 里是**新对象**，所以从 0 开始。
- 生产端走同一个 `get_registry()`，单实例重复调用是幂等的（带 cached）。

### 4. Hot path 接入

| 文件 | 调用点 | Metric |
|------|--------|--------|
| `niki/api/chat_stream.py` | `chat_stream()` 入口 | `CHAT_TOTAL.labels(route="chat_stream", provider="mock", mode="stream")` |
| `niki/safety/input_gateway.py` | `check()` 返回 BLOCK verdict | `SAFETY_BLOCK_TOTAL.labels(reason=matched_category, severity="block")` |
| `niki/safety/input_gateway.py` | `check()` 返回 ESCALATE verdict | `SAFETY_BLOCK_TOTAL.labels(reason="self_harm", severity="escalate")` |
| `niki/services/auth_audit.py` | `_safe_add()` 落库后 | `AUTH_AUDIT_TOTAL.labels(event_type=..., severity=...)` |
| `niki/api/auth.py` | `sms_login()` 成功 | `LOGIN_SUCCESS_TOTAL.labels(method="sms")` |
| `niki/api/auth.py` | `sms_login()` SMSCodeInvalid | `LOGIN_FAILURE_TOTAL.labels(method="sms", reason="invalid_code")` |
| `niki/middleware/rate_limit.py` | `_on_rate_limit()` | `RATE_LIMITED_TOTAL.labels(endpoint=...)` |

5 个 hot path（chat + safety-block + safety-escalate + auth_audit + auth-login × 2），
满足 AC-3。

### 5. HTTP middleware

```python
class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        endpoint = request.scope.get("route").path if request.scope.get("route") else request.url.path
        HTTP_REQUEST_DURATION.labels(method=request.method, endpoint=endpoint).observe(elapsed)
        return response
```

`endpoint` label 用路由模板（如 `/api/v1/auth/sms/login`）而不是 raw path，
避免把用户输入（手机号 / UUID）当 label 值造成 cardinality 爆炸。

## 测试

`tests/api/test_metrics.py` —— 14 用例：

1. `/metrics` 返回 200
2. content-type 是 `text/plain; version=0.0.4`
3. body 含 `niki_chat_total`、`niki_safety_block_total`、`niki_login_success_total`
4. 调用 `/chat/stream` → `niki_chat_total` +1
5. 触发 safety block → `niki_safety_block_total{reason="profanity",severity="block"}` +1
6. `reset_for_tests()` 后计数归零
7. label cardinality 有界 —— 1000 个不同 `route` label value 不会 OOM（仅看 metric
   数量增加、不会爆 Prometheus）
8. `niki_active_parents` Gauge 初始为 0
9. `HTTP_REQUEST_DURATION` 观测到 latency（`_sum` > 0）
10. 不同 label 产生不同 series —— `LOGIN_SUCCESS_TOTAL{method="sms"}` 与
    `LOGIN_SUCCESS_TOTAL{method="password"}` 各自独立
11. registry 隔离 —— 测试 A 加的 counter 在测试 B 的 `get_registry()` 不可见
12. auth audit counter —— 调用 `auth_audit.log_event()` → `niki_auth_audit_total` +1
13. rate-limited counter —— 触发 429 → `niki_rate_limited_total` +1
14. SMS login failure → `LOGIN_FAILURE_TOTAL{method="sms",reason="invalid_code"}` +1

## 风险与回滚

- 风险：`prometheus-client` 新增依赖 → pyproject 锁定 `>=0.20.0`，CI 装包即可
- 回滚：`git revert` 即可，无 schema 迁移
- 监控：上线后第一周盯 `rate_limit_exceeded` / `prometheus_exposition_latency`

## 文件清单

新增：
- `niki/metrics.py`
- `niki/api/metrics.py`
- `niki/middleware/metrics.py`
- `tests/api/test_metrics.py`

修改：
- `pyproject.toml`（+1 dep）
- `niki/main.py`（注册 router + middleware）
- `niki/api/router.py`（接入 `metrics_router`）
- `niki/api/chat_stream.py`（+1 行 CHAT_TOTAL）
- `niki/safety/input_gateway.py`（+2 行 SAFETY_BLOCK_TOTAL）
- `niki/services/auth_audit.py`（+1 行 AUTH_AUDIT_TOTAL）
- `niki/api/auth.py`（+2 行 LOGIN_SUCCESS / LOGIN_FAILURE）
- `niki/middleware/rate_limit.py`（+1 行 RATE_LIMITED_TOTAL）