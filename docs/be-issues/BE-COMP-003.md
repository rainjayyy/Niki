# BE-COMP-003：最低客户端版本强制

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | COMPLIANCE / API |
| **来源** | 前端对接文档 §11.2 版本管理 + §7.2.8 |
| **工作量** | 0.25 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-MISC-001 ✅（min_supported_version 字段已上） |
| **后续** | 强制升级引导 UI（→ 前端工单） |

## 背景

客户端版本过旧会有：
- 已知安全 bug 未打补丁
- 老协议不再支持
- 新功能用不了

§11.2 要求：**低于 `min_supported_version` 的客户端返回 426 Upgrade Required**。

当前 `X-Client-Version` 完全被忽略。前端可以永远不升级。

## 验收标准

- [ ] AC-1：`X-Client-Version` < `min_supported_version` → 426
- [ ] AC-2：响应含 `Retry-After` + `Upgrade` 头
- [ ] AC-3：版本号语义化比较（`1.2.3` < `1.10.0`）
- [ ] AC-4：缺 header 时**不**拒绝（旧客户端可能没发，向后兼容）
- [ ] AC-5：测试覆盖 ≥ 4 个
- [ ] AC-6：现有 1367 passed 测试**零回归**

## 设计

`/api/v1/config/client` 已经返回 `min_supported_version`。增加 middleware 读 `X-Client-Version`：

```python
class MinClientVersionMiddleware:
    async def dispatch(self, request, call_next):
        ver = request.headers.get("X-Client-Version")
        if ver and _lt(ver, settings.min_supported_version):
            return Response(
                426, body={"error": "UPGRADE_REQUIRED", ...},
                headers={"Upgrade": settings.min_supported_version, ...}
            )
        return call_next(request)
```

半路换 semantic_version 库（`packaging.version`）。
