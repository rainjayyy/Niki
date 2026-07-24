# BE-API-002：限流中间件

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API |
| **来源** | `docs/frontend-integration.md` §7.1.7 + §7.4 |
| **工作量** | 1 天（与 BE-API-001 合并为 3 天，本次独立提） |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-API-001 ✅（429 也要走 §6.3 shape） |
| **后续** | BE-API-002-redis 用 Redis storage 跨实例 |

## 背景

§7.1.7 明确写出：

> 限流中间件 | 缺失 | 儿童端语音频繁请求会爆 token | 1 天

实际痛点：
- 儿童端 button 长按 → 反复触发 `/audio/transcribe` → 反复扣 LLM token（DeepSeek / doubao）→ 月底算账哗哗流
- 没限流的话，恶意脚本可以 1 秒内发 1000 次 `/auth/sms/send` 把上游 SMS 服务商配额烧光
- 没限流的话，家长端可以无限并发 POST `/dashboard/...` 把 DB 打爆

本工单接入 slowapi，提供分级 per-route 限流。

## 验收标准（AC）

- [ ] AC-1：集成 slowapi (基于 limits 库)，单实例内存 storage
- [ ] AC-2：429 响应返回 §6.3 shape：`{success: false, error: {code: "RATE_LIMIT_EXCEEDED", ...}, request_id}`
- [ ] AC-3：`Retry-After` header 出现在 429 响应
- [ ] AC-4：`X-RateLimit-Limit` / `X-RateLimit-Remaining` headers 在成功响应里
- [ ] AC-5：分级限流 —— 至少 4 个端点带 `@limiter.limit(...)`：ASR / TTS / chat-stream / sms-send
- [ ] AC-6：默认 limit 全局（兜底）
- [ ] AC-7：单测 + 集成测试 ≥ 8 个
- [ ] AC-8：现有 979 passed 测试**零回归**（限流只在超额时触发）

## 设计

### 1. 限流规则（per-route）

| 端点 | 限制 | 理由 |
|------|------|------|
| `POST /auth/sms/send` | 3 / minute / IP | 防 SMS 轰炸 |
| `POST /auth/sms/login` | 10 / minute / IP | 防暴力枚举 |
| `POST /auth/refresh` | 60 / minute / IP | 正常 1 天几次 |
| `POST /audio/transcribe` | 30 / minute / IP | LLM token 守护 |
| `POST /audio/synthesize` | 60 / minute / IP | TTS provider 守护 |
| `POST /chat/stream` | 30 / minute / IP | LLM 流式 token 守护 |
| `POST /media/upload` | 20 / minute / IP | 上传带宽 |
| 默认（其他） | 100 / minute / IP | 兜底 |

### 2. 实现

- `niki/middleware/rate_limit.py`：
  - `limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])`
  - 注册到 FastAPI app state
  - 加自定义 `RateLimitExceeded` handler，让 429 走 §6.3 shape
  - 加 `request_id` 注入
  - 加 SlowAPIMiddleware + SlowAPIException → handler
- 装饰器应用：
  ```python
  @router.post("/audio/transcribe")
  @limiter.limit("30/minute")
  async def transcribe(request: Request, audio: UploadFile = File(...)):
      ...
  ```
  ⚠️ 注意：slowapi 要求 endpoint 显式接 `request: Request` 参数，否则装饰器会抛错

### 3. 风险

| 风险 | 缓解 |
|------|------|
| NAT 后多个用户同一 IP 互相影响 | key 也可以是 parent_id（auth 后）；v2 支持 |
| 内存 storage 重启丢失 | 接受；production 用 Redis |
| 限流绕过（X-Forwarded-For 伪造） | 默认用 `request.client.host`；trust proxy 名单收窄 |
| 测试隔离 | per-test 关闭 limiter 或重置 storage |

### 4. 不在本工单

- ❌ Redis storage（→ BE-API-002-redis）
- ❌ 按 parent_id 限流（→ BE-API-002-ext-A）
- ❌ 滑动窗口算法（用 slowapi 默认 fixed-window 即可）

## 后续工单

- **BE-API-002-redis**：跨实例限流（双实例部署时不漏放）
- **BE-API-002-ext-A**：按 `parent_id` 限流（登录后）
