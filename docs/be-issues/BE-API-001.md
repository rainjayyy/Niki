# BE-API-001：统一响应包装（§6.3 compliance）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API |
| **来源** | `docs/frontend-integration.md` §7.1.7 + §6.3 |
| **工作量** | 2 天（合并到 BE-API-002 共 3 天） |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | 无 |
| **后续** | BE-API-002 限流（共享这次 middleware 改造） |

## 背景

§7.1.7 已经写明：

> 5 种返回模式并存：APIResponse / AuthError dict / 原始 dict / dataclass.to_dict() / Pydantic model
> 前端 axios 拦截器必须按路径分流；否则出错

实际表现（前端代码被迫每个端点单独判断）：

```ts
// 前端 axios interceptor 目前的丑代码：
if (url.endsWith('/auth/signup/start')) {
  // success: response.data.account  (Pydantic to_dict)
  // error:   response.data.detail.error  (HTTPException)
} else if (url.endsWith('/dashboard/...')) {
  // success: response.data.report
  // error:   response.data.error (string)
} ...
```

§6.3 已经写好契约，但只有 `APIResponse[T]` schema 在 `niki/schemas/common.py` 里，没有强制所有端点用。

## 验收标准（AC）

- [ ] AC-1：`APIResponse[T].error` 字段从 `str` 升级为 `str | dict[str, Any] | None`
- [ ] AC-2：dict shape 形如 `{"code": "INVALID_PHONE_FORMAT", "message": "..."}`，符合 §6.3
- [ ] AC-3：global exception handlers (NikiException / ValidationError / SafetyViolationError / LLMRateLimitError / LLMTimeoutError / LLMAuthenticationError / InfrastructureError / ConfigurationError / UnhandledException / HTTPException) 都返回新 shape
- [ ] AC-4：AuthError 通过 HTTPException 抛出后，前端拿到的 `error.code` 跟 §11.1 命名规范一致
- [ ] AC-5：`request_id` 出现在每个响应（成功 + 失败）
- [ ] AC-6：`Authorization` header 出现在 401 响应（保持 BE-AUTH-001 兼容）
- [ ] AC-7：测试覆盖每个 exception class，至少 9 个 handler-level 测试
- [ ] AC-8：现有 939 passed 测试**零回归**（向后兼容 `error: str` 也得保留可读）

## 设计

### 1. 新响应契约（§6.3 升级版）

```json
// Success
{
  "success": true,
  "data": { ... },
  "request_id": "uuid-v4"
}

// Error (新增)
{
  "success": false,
  "error": {
    "code": "INVALID_PHONE_FORMAT",   // 机器可读
    "message": "phone format invalid", // 人类可读
    "detail": { ... }                 // 可选上下文
  },
  "request_id": "uuid-v4"
}

// Back-compat 字段：保留旧 `error` 字段平铺形式给老前端
{
  "success": false,
  "error": "phone format invalid",     // 旧字段保留
  "error_code": "INVALID_PHONE_FORMAT",
  "error_detail": { ... },
  "request_id": "uuid-v4"
}
```

最终方案：**两种 shape 共存**，前端挑喜欢的：
- 新前端用 `error.code` / `error.message`（嵌套 dict）
- 老前端继续读 `error` 字符串 + 新增 `error_code`

### 2. 异常 handler 层级

```
Exception in endpoint
      ↓
FastAPI 默认 hierarchy
      ↓
我们的 handlers:
  - HTTPException（含 AuthError 抛出）→ structured error
  - RequestValidationError → VALIDATION_ERROR
  - NikiException  → 各自的 .code
  - 其他 Exception → INTERNAL_ERROR
      ↓
最终响应：
  {
    "success": false,
    "error": {"code": "...", "message": "..."},
    "error_code": "...",   // 平铺背兼容
    "error_detail": {...},
    "request_id": "uuid-v4"
  }
```

### 3. 模块改动

- **`niki/schemas/common.py`**：`APIResponse` 加 `error_code`/`error_detail` 字段
- **`niki/middleware/error_handler.py`**：所有 handlers 改返回新 shape
- **`niki/api/auth.py`**：`AuthError` 路径同步（仍走 HTTPException，但 detail 用 dict）
- **`niki/auth/account.py`**：`AuthError.__init__` 加可选 `code` 字段

### 4. 风险

| 风险 | 缓解 |
|------|------|
| 现有 939 测试期望旧 shape | 全部用 `error_code`/`error` 双写破坏最小；老 key 仍然存在 |
| 旧前端依赖 `error` 字符串 | 同上 |
| HTTPException 默认 handler 被覆盖可能改语义 | 加 `if "code" in detail: skip` 判断避免破坏现有 §11.1 命名 |

### 5. 不在本工单

- ❌ 给所有端点的 success body 也加 `success: true`/`data: ...` 包装（→ BE-API-001-rollout，后续 PR）
- ❌ 限流（→ BE-API-002）

## 后续工单

- **BE-API-001-rollout**：把所有端点的 success body 都包成 `APIResponse[T]`
- **BE-API-002**：slowapi 限流中间件
