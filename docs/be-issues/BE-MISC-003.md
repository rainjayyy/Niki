# BE-MISC-003：公共状态页

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MISC / OPS |
| **来源** | 前端对接文档 §7.3.8 |
| **工作量** | 0.25 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | 无 |
| **后续** | BE-API-009（Prometheus 指标导出） |

## 背景

§7.3.8 要求 `GET /status` 返回服务 + 依赖的实时状态。

当前：家长端只能从 dashboard 反推服务状态（"我的告警怎么没收到？"）。

## 验收标准

- [ ] AC-1：`GET /api/v1/status` 返回整体状态 + 各依赖状态
- [ ] AC-2：依赖项含 Redis / Postgres / 阿里云 OSS 等
- [ ] AC-3：失败依赖标 degraded 而非 500
- [ ] AC-4：测试覆盖 ≥ 3 个
- [ ] AC-5：现有 1376 passed 测试**零回归**

## 设计

复用 `niki/api/health.py` 的依赖检查：
- Redis / Postgres / Chroma / Neo4j / DeepSeek / doubao
- 整体状态 = "healthy" / "degraded" / "unhealthy"
- 单个依赖失败不导致整个 endpoint 500

```python
@router.get("/status")
async def public_status():
    checks = await gather_dependency_checks()
    return {
        "status": "healthy" if all healthy else "degraded",
        "components": checks,
        "version": settings.api_version,
    }
```

## 后续

- **BE-API-009**：Prometheus 指标
- **BE-API-007**：TS SDK
