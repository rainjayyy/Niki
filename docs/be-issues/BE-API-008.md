# BE-API-008: Webhook 回调（异步任务）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | API / 集成 |
| **来源** | `docs/frontend-integration.md` §7.3.5 |
| **工作量** | 2 天 |
| **状态** | 🚧 进行中（2026-07-31） |
| **依赖** | BE-COMP-004 ✅（`audit_logs` 统一审计）、BE-API-001 ✅（错误体 §6.3）、BE-API-002 ✅（slowapi 限流）、BE-I18N-001 ✅（多语种错误） |
| **后续** | BE-API-008-rollout（异步任务进度回调，把 cron 的 30 天删除过程拆成阶段 webhook） |

## 背景

30 天删除走 cron，但用户在 App 内根本拿不到任何进度：申请
后 24 小时没动静是「还在冷静期」？7 天没动静是「系统在处理」
还是「请求丢了」？14 天后收到删除完成邮件的时候，App 内的
状态还停留在「冷静期」—— 因为前端没有任何事件可以订阅。

更广义地，任何一个跨日的异步流程（30 天删除、7 天静默期聚合、
数据导出）目前都是「黑盒」：客户端只能轮询。本工单把
**「跨系统异步事件」**这一面收敛成一个 webhook 机制：

| 流程 | 现状 | 本工单后 |
|------|------|---------|
| 30 天删除 | cron 静默运行 | 阶段事件（requested / completed）实时推送 |
| 数据导出 | 提交后 200 但不知何时完成 | `safety.escalated` 风格的回调 |
| 安全升级 | 客户端轮询 `/admin/audit` | `safety.escalated` 主动推 |

## 验收标准（AC）

- [x] AC-1：6 个端点（register / list / get / update / delete /
      deliveries），全部在 `/api/v1/webhooks` 下
- [x] AC-2：HMAC-SHA256 签名，header 形如
      `X-Niki-Signature: sha256=<hex>`；hex = lowercase
- [x] AC-3：连续失败 5 次后自动 disable（`is_active=false` +
      `disabled_at` 戳）
- [x] AC-4：重试退避 1s / 4s / 16s（指数），最多 3 次尝试
- [x] AC-5：每次状态变化（register / update / disable）写一行
      `audit_logs`（`event_type` = `webhook_registered` /
      `webhook_updated` / `webhook_disabled`）
- [x] AC-6：横向越权 — 跨 owner 访问统一 404（不是 403），
      避免 id oracle
- [x] AC-7：生产模式强制 HTTPS（`allow_insecure_webhook_targets`
      = False 时拒绝 http://）
- [x] AC-8：测试 ≥ 20 个，全绿
- [x] AC-9：现有测试零回归

## 设计

### 1. 端点总览

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/api/v1/webhooks` | 注册（生成 secret） |
| `GET`  | `/api/v1/webhooks` | 列当前 owner 全部订阅（无 secret） |
| `GET`  | `/api/v1/webhooks/{id}` | 查单个（无 secret） |
| `PATCH`| `/api/v1/webhooks/{id}` | 改 target_url / event_types / is_active |
| `DELETE`| `/api/v1/webhooks/{id}` | 软删（`is_active=false`） |
| `GET`  | `/api/v1/webhooks/{id}/deliveries` | 最近投递记录（默认 50，max 100） |

`POST` 限流 10/minute/IP（slowapi），避免恶意刷 secret。其余端点
不单独限流 —— owner-scoped listing 自然受横向越权保护。

### 2. 安全模型

#### 2.1 签名

```
X-Niki-Signature: sha256=<hex>
X-Niki-Event:     <event_type>
X-Niki-Event-Id:  <event_id>          # 通常就是 audit_logs.id
User-Agent:       Niki-Webhooks/1.0
Content-Type:     application/json
```

签名算法：
```python
hmac.new(
    secret.encode("utf-8"),
    payload_bytes,         # request body, exactly as sent
    hashlib.sha256,
).hexdigest()
```

`payload_bytes` 必须是**请求体原文**（不是 `json.dumps(payload)），
因为接收方验签时也只能拿到字节流。测试用 `httpx.MockTransport`
或 monkey-patch `httpx.AsyncClient.post` 时同样传 `content=body`。

#### 2.2 Secret 暴露

只在 `POST` 响应里**一次性**返回 secret。`GET` / `LIST` /
`PATCH` / `GET /deliveries` 全部不带 secret 字段。订阅者必须
在创建时把 secret 保存下来 —— 服务端不存明文回放路径（`secret`
虽然存了明文用于签名，但**绝不**在 list/get 中返回）。

#### 2.3 横向越权

所有 owner-scoped 端点都用 `get(subscription_id, owner_id)`
过滤；跨 owner 直接返回 404 + `webhook.not_found`，**不**返回
403，避免 id 存在性 oracle。

### 3. 投递器

`WebhookDispatcher.dispatch(event_type, payload, *, event_id=None)`：

1. 用 `event_id`（缺省时 `uuid4()`）做幂等键。
2. 查所有 `is_active=True` 且 `event_type in event_types` 的订阅。
3. 对每个订阅：
   - 写一行 `webhook_deliveries`（`delivered_at=NULL`）。
   - 用 `httpx.AsyncClient(timeout=10s)` POST。
   - 2xx → 标记 `delivered_at`，清零 `failure_count`。
   - 5xx / 网络异常 → 重试，间隔 1s / 4s / 16s。
   - 第 3 次仍失败 → `failure_count += 1`；
     累计到 5 → 自动 `is_active=False` + `disabled_at` 戳。
4. 同一 `(subscription_id, event_id)` 通过
   `UNIQUE (event_id, subscription_id)` 保证只一行；已存在
   则跳过（idempotency）。

为什么是 5 次 + 3 次重试：
- 5 次 = 「这个 target 真的不在线」，不是「这单一波动了一下」。
- 3 次重试 + 1s/4s/16s 退避 ≈ 21s 撑住瞬时抖动。

### 4. i18n 新键

| key | zh | en |
|-----|----|----|
| `webhook.invalid_url` | URL 格式不正确 | Invalid webhook URL |
| `webhook.invalid_event_type` | 事件类型不在支持范围 | Event type not supported |
| `webhook.not_found` | 订阅不存在或不属于当前账户 | Subscription not found or not owned by this account |
| `webhook.too_many_event_types` | 事件类型超过 10 个上限 | Too many event types (max 10) |
| `webhook.url_not_https` | 生产环境必须使用 HTTPS | HTTPS is required in production |
| `webhook.rate_limited` | 创建订阅请求过于频繁 | Too many subscription attempts, retry later |

### 5. Settings 扩展

```python
allow_insecure_webhook_targets: bool = False   # 允许 http://
# 测试环境可通过 NIKI_ALLOW_INSECURE_WEBHOOK_TARGETS=true 打开
```

### 6. 数据模型

`webhook_subscriptions`：
- `id` (PK, uuid str)
- `owner_id` (string, indexed)
- `target_url` (string)
- `secret` (string, server-generated `secrets.token_urlsafe(32)`)
- `event_types` (JSON list)
- `is_active` (bool, default true)
- `failure_count` (int, default 0)
- `last_delivery_at` (datetime nullable)
- `disabled_at` (datetime nullable)
- `created_at`, `updated_at`

`webhook_deliveries`：
- `id` (PK, uuid str)
- `subscription_id` (FK → webhook_subscriptions.id)
- `event_id` (string, 幂等键)
- `event_type` (string)
- `payload` (JSON)
- `target_url` (string, denormalized)
- `attempt_count` (int, default 0)
- `last_attempt_at` (datetime nullable)
- `last_status_code` (int nullable)
- `last_error` (string nullable)
- `delivered_at` (datetime nullable)
- `created_at`
- INDEX (subscription_id, created_at)
- UNIQUE  (event_id, subscription_id)

### 7. 跟现有 BE-COMP-004 的关系

每次状态变化 → `audit_logs` 一行：

| 行为 | event_type | severity | metadata |
|------|-----------|----------|----------|
| `POST /webhooks` | `webhook_registered` | info | `{subscription_id, target_url, event_types_count}` |
| `PATCH /webhooks/{id}` | `webhook_updated` | info | `{subscription_id, target_url, fields_changed}` |
| `DELETE /webhooks/{id}` | `webhook_disabled` | info | `{subscription_id, target_url}` |

`metadata` 不带 `secret`（PII 风险 + 重放风险）。
