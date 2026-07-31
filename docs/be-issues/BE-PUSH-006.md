# BE-PUSH-006：推送测试端点（`POST /push/test`）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | PUSH / SUPPORT |
| **来源** | `docs/frontend-integration.md` §7.3.2 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-31） |
| **依赖** | BE-PUSH-001 ✅（device 注册 + prefs）、BE-COMP-004 ✅（`audit_logs` 统一表）、BE-API-002 ✅（slowapi 限流） |
| **后续** | BE-PUSH-002 / BE-PUSH-003（真实厂商 SDK 投递，替换本工单的 dry-run stub） |

## 背景

BE-PUSH-001 落地了 device 注册面（`/push/register`、`/push/devices`、
`/push/prefs`），但**没有任何办法验证一台设备到底能不能收到推送**。

后果：家长端反馈「我收不到告警」时，客服无法区分三种完全不同的故障：

| 可能故障 | 排查所需信号 | 本工单前 |
|---------|------------|---------|
| token 没注册上 | `GET /push/devices` | ✅ 已有 |
| token 注册了但设备被 kick | `GET /push/active` | ✅ 已有 |
| token 有效但投递链路没打通 | **发一条测试消息** | ❌ 无 |

第三行是本工单要补的洞。真实厂商投递（HMS / MiPush / OPPO / Vivo /
Flyme / APNs / FCM / Web Push）属于 BE-PUSH-002/003（P1+），本工单
**不做**真实投递 —— 但它把「投递」这件事收敛成一个可替换的 seam，
让 002/003 只需要换一个实现类，而不用改端点、审计、限流任何一行。

## 验收标准（AC）

- [ ] AC-1：新增 2 个端点 —— `POST /api/v1/push/test` + `GET /api/v1/push/test/history`
- [ ] AC-2：dry-run 模式（不打厂商），且每次测试写一行 `audit_logs`
      （`event_type="push_test_sent"`、`severity="info"`）
- [ ] AC-3：横向越权防护 —— 非本 parent 名下的 `device_id` 返回 404
- [ ] AC-4：`POST /push/test` 限流 5/minute/IP（复用 slowapi）
- [ ] AC-5：测试 ≥ 10 个（`tests/api/test_push_test.py`），全绿
- [ ] AC-6：现有测试零回归

## 设计

### 1. `PushDeliveryService` —— 唯一的投递 seam

```python
class PushDeliveryService(ABC):
    @abstractmethod
    async def deliver(
        self,
        device: PushDevice,
        payload: PushPayload,
        *,
        dry_run: bool = False,
    ) -> PushDeliveryResult: ...
```

三个类型：

| 类型 | 作用 |
|------|------|
| `PushPayload` | `{title, body, severity, dedupe_key, data}` —— 要发什么 |
| `PushDeliveryResult` | `{device_id, channel, status, error, latency_ms, dry_run}` —— 发生了什么 |
| `PushDeliveryService` | ABC，`deliver()` 一个方法 |

`status` 是四值枚举：`delivered` / `dry_run` / `failed` /
`skipped_no_token`。之所以把 `dry_run` 同时做成 **status 值**和
**布尔字段**，是因为两者回答的问题不同：布尔是「本次调用请求了
dry-run 吗」（入参回显），status 是「最终发生了什么」。BE-PUSH-002
会出现 `dry_run=True` 但 `status="skipped_no_token"` 的组合 ——
即请求了 dry-run，但设备连 token 都没有，压根到不了投递那一步。

### 2. 为什么默认实现是环形缓冲区

`InMemoryPushDeliveryService` 把每次投递写进一个 **最近 100 条**
的 ring buffer（`collections.deque(maxlen=100)`），并同时打一行
structlog JSONL。

ring buffer 是给 `GET /push/test/history` 用的：客服点了「发测试推送」
之后需要一个二次确认面 —— 「刚才那一下真的进到投递层了吗」。
只看 `POST` 的 200 响应不够，因为客服常常是在**别人**（家长）点完
之后才来查的。

`maxlen=100` 而不是无上限：这是进程内内存，且用途是「刚刚发生的事」，
不是审计留存。**留存**责任在 `audit_logs`（AC-2）—— 那张表是持久的、
可按 parent 查的、6 个月留存。ring buffer 只是它的低延迟前缀。

两者的分工：

| | ring buffer | `audit_logs` |
|--|------------|-------------|
| 生命周期 | 进程内，最近 100 条 | 持久，6 个月 |
| 查询维度 | 全局 + channel 过滤 | 按 parent / event_type / 时间 |
| 读者 | 客服（实时排查） | 家长 + 合规 |
| 丢失后果 | 无（重发一次即可） | 合规缺口 |

### 3. `latency_ms` 为什么是随机数

`random.randint(5, 50)` —— 这是**故意的假数据**，且必须显眼地假。

真实投递的 P50 在 20-200ms 区间。如果 stub 返回 `0` 或 `None`，
前端的「投递耗时」图表会画出一条贴地的线，看起来像「我们的推送
超快」，而不是「这里还没接真东西」。返回一个 5-50ms 的抖动值，
配合 `status="dry_run"` 和 `X-Push-Test-Mode: dry-run` 响应头，
让任何看到这个数的人都知道它不代表生产链路。

BE-PUSH-002 接真 SDK 时，`latency_ms` 换成真实的 wall-clock 测量，
`status` 换成 `delivered`，响应头消失 —— 端点签名不变。

### 4. 横向越权：为什么是 404 而不是 403

`POST /push/test` 的 `device_id` 是调用方给的。如果别人的 device_id
返回 403，攻击者就得到了一个 **device_id 存在性预言机**：403 = 存在
但不是你的，404 = 不存在。逐个试就能枚举出平台上的有效 device_id。

所以两种情况都返回 404 + 同一条 i18n 文案（`push.device_not_found`），
响应体不可区分。这与 `/kick`、`/enable` 的既有约定一致。

### 5. 限流 5/minute

比其他端点（20-60/minute）严格得多，因为：

1. BE-PUSH-002 接上之后，每次调用都会真的打厂商 API —— 厂商侧有
   自己的配额，刷爆了会影响**真实告警**的投递；
2. 推送是会响铃的。一个失控的重试循环等于对家长手机的 DoS；
3. 正常使用场景是「客服排查时点一下」，5 次/分钟绰绰有余。

### 6. i18n

| key | zh | en |
|-----|----|----|
| `push.device_not_found` | 未找到该设备，或该设备不属于当前账户 | Device not found or not owned by this account |
| `push.test_rate_limited` | 测试推送请求过于频繁，请稍后再试 | Too many test push requests, please retry later |

## 接口

### `POST /api/v1/push/test`

请求：

```json
{
  "device_id": "d-1",
  "title": "测试推送",
  "body": "这是一条测试推送",
  "severity": "info"
}
```

除 `device_id` 外全部可选（有默认值）。

响应 `200`：

```json
{
  "device_id": "d-1",
  "status": "dry_run",
  "channel": "apns",
  "latency_ms": 23
}
```

响应头：`X-Push-Test-Mode: dry-run`

错误：

| 码 | 场景 |
|----|------|
| 404 | `device_id` 不存在或不属于当前 parent |
| 422 | 缺 `device_id`（Pydantic 校验） |
| 429 | 超过 5/minute |

### `GET /api/v1/push/test/history?limit=20&channel=apns`

响应 `200`：

```json
{
  "deliveries": [
    {
      "device_id": "d-1",
      "channel": "apns",
      "status": "dry_run",
      "error": null,
      "latency_ms": 23,
      "dry_run": true
    }
  ],
  "count": 1
}
```

`limit` 默认 20，上限 100（超过则截到 100，不报错 —— 上限就是
ring buffer 的容量，报错没有意义）。`channel` 可选，精确匹配过滤。

## 测试计划

`tests/api/test_push_test.py`：

1. dry-run 成功 → 200 + `status="dry_run"` + `latency_ms` 在 5-50
2. 非本人 device → 404
3. 缺 `device_id` → 422
4. 空 body（仅 device_id）→ 200，标题/正文走默认值
5. history 能看到刚才那次投递
6. `limit=500` → 截到 100
7. `audit_logs` 里有 `push_test_sent` 行（含 parent_id / device_id / dry_run）
8. 第 6 次调用 → 429
9. `set_push_delivery_service(Mock...)` 注入生效
10. ring buffer 推 150 条 → 只留最后 100 条
11. `X-Push-Test-Mode: dry-run` 响应头存在
12. history 的 `channel` 过滤生效
