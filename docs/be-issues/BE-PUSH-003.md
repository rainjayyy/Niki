# BE-PUSH-003 — Web Push (VAPID) 投递

> 家长端关闭浏览器后也能收到告警。`pywebpush` + service worker，
> VAPID 公私钥落在 `settings`（缺省自动生成到磁盘）。
>
> 状态：已实现 · 分支 `feat/be-push-003-web-push-vapid` · 预估 1 天

---

## 1. 背景

BE-PUSH-006 已经把「投递」抽成了一条缝：

```
PushDeliveryService.deliver(device, payload, *, dry_run) -> PushDeliveryResult
```

在此之前所有平台都走 `InMemoryPushDeliveryService`（dry-run 桩）。
本票据只做一件事：**给 `platform == "web"` 的设备接上真实的 Web Push
（RFC 8030 + VAPID / RFC 8292）投递**，其余平台（ios / android）继续走
dry-run 桩，等 BE-PUSH-002 补厂商 SDK。

### 为什么不用票据里写的 `py_vapid`

`py_vapid` 只负责签 VAPID JWT，不负责 payload 加密（aes128gcm）和
POST 到 push service，且已经停止维护。`pywebpush>=2.0.0` 内部依赖
`py_vapid`，同时把「签名 + 加密 + 发送」三件事收在一个函数里，是社区
事实标准。**决定：依赖 `pywebpush>=2.0.0`，不直接依赖 `py_vapid`。**

> ⚠️ 本次开发环境的 PyPI 出网被阻断（`SSLEOFError`），`pywebpush` 未能
> 装上。因此代码里 `pywebpush` 是**惰性导入 + 可注入**的：没装时模块照常
> import，只有真正发起非 dry-run 投递时才会失败并返回
> `status="failed", error="PYWEBPUSH_UNAVAILABLE"`。落地时执行
> `pip install "pywebpush>=2.0.0"` 即可。

---

## 2. 验收标准（AC）

| # | 内容 |
|---|---|
| AC-1 | `WebPushDeliveryBackend` 实现 `PushDeliveryService`；组合服务按 `device.platform` 把 `web` 路由给它 |
| AC-2 | VAPID 密钥未配置时首次使用自动生成（EC P-256），落盘缓存，二次调用不再生成 |
| AC-3 | `GET /api/v1/push/vapid-public-key` 返回公钥（免鉴权），供 service worker 订阅 |
| AC-4 | `service-worker.js` 处理 `push` / `notificationclick` / `pushsubscriptionchange` |
| AC-5 | 静态文件挂在 `/static/` |
| AC-6 | 每次投递尝试都写 `audit_logs` |
| AC-7 | 新增测试 ≥ 12 全绿 |
| AC-8 | 存量 1842 条测试零回归 |

**结果**：新增 41 条测试（`tests/api/test_push_web.py`），全套
`1879 passed, 4 skipped`（原 1838 + 41）。

---

## 3. 设计

### 3.1 密钥管理（`niki/config/settings.py`）

新增设置项：

| 字段 | 默认 | 含义 |
|---|---|---|
| `vapid_private_key` | `None` | PEM 编码的 EC 私钥（PKCS#8） |
| `vapid_public_key` | `None` | base64url 的未压缩公钥点（65 字节，`0x04` 开头） |
| `vapid_subject` | `mailto:admin@niki.app` | VAPID JWT 的 `sub` claim（push service 出问题时联系人） |
| `vapid_private_key_file` | `./.vapid_private.pem` | 自动生成时的落盘位置 |
| `vapid_public_key_file` | `./.vapid_public.b64` | 同上 |
| `web_push_ttl_seconds` | `86400` | push service 缓存未送达消息的时长 |
| `web_push_urgency` | `normal` | RFC 8030 `Urgency` 头 |

`get_or_generate_vapid_keys() -> tuple[str, str]` 的解析顺序：

1. `settings` 上两个字段都非空 → 直接返回（生产：从环境变量注入）。
2. 两个缓存文件都存在且可读 → 读出来写回 settings 后返回。
3. 否则生成 `ec.generate_private_key(ec.SECP256R1())`，
   私钥 → PEM（PKCS#8 / 无口令），公钥 → 未压缩点的 base64url（去 `=`），
   尽力落盘（写失败只 warning，不抛），写回 settings 后返回。

**幂等性**是这个函数的核心契约：第二次调用必须返回同一对密钥，否则
已经订阅的浏览器全部失效（`applicationServerKey` 变了 → push service 401）。
幂等性由「settings 缓存」和「文件缓存」两层共同保证。

### 3.2 `WebPushDeliveryBackend`（`niki/services/push_delivery.py`）

```
deliver(device, payload, *, dry_run=False) -> PushDeliveryResult
```

判定顺序（早退优先，越便宜的检查越靠前）：

| 条件 | 结果 |
|---|---|
| `device.platform != "web"` | `skipped_no_token`（不是我该管的设备） |
| `push_token` 为空 / 不以 `https://` 开头 | `skipped_no_token` |
| `dry_run=True` | `dry_run`，`latency_ms = randint(10, 50)`，不发 HTTP |
| push service 返回 `201`（或任意 2xx） | `delivered` |
| 4xx | `failed`，`error="HTTP_4xx"` |
| 5xx / 网络异常 / `WebPushException` | `failed` |

`push_token` 的两种形态都支持：

* 纯 endpoint URL（`https://fcm.googleapis.com/fcm/send/...`）——
  此时 `p256dh` / `auth` 从 device 行的同名字段取（可能没有，
  push service 会自己拒；我们不猜）。
* 完整的 `PushSubscription.toJSON()` 字符串（`{"endpoint": ..., "keys": {...}}`）
  —— 直接透传。这是前端最省事的注册方式。

`pywebpush` 的调用参数：

```python
webpush(
    subscription_info=...,
    data=json.dumps(payload_dict),
    vapid_private_key=private_pem,
    vapid_claims={"sub": settings.vapid_subject},
    ttl=86400,
    headers={"TTL": "86400", "Urgency": "normal", "Topic": dedupe_key?},
)
```

`Topic` 只在 `payload.dedupe_key` 存在时带上——push service 会用它折叠
同 topic 的未送达消息，正好是我们 `dedupe_key` 的语义。

**可注入性**：`WebPushDeliveryBackend(webpush_fn=...)` 允许构造时注入；
不注入时在 `deliver()` 里通过模块级 `_default_webpush_send` 解析（惰性
`import pywebpush`）。测试两条路都能用：注入假函数，或 monkeypatch
模块属性。

### 3.3 组合分发

```python
class CompositePushDeliveryService(InMemoryPushDeliveryService):
    #  platform == "web" → WebPushDeliveryBackend
    #  其它             → super()（dry-run 桩，BE-PUSH-002 接管）
```

刻意**继承**而不是组合 `InMemoryPushDeliveryService`：`GET /push/test/history`
读的是同一个环形缓冲区，继承让 web 投递的结果也自动进 buffer，
支持工程师看到的历史不会缺一半。`get_push_delivery_service()` 的
默认实例改成它。

### 3.4 `GET /api/v1/push/vapid-public-key`

免鉴权。公钥本来就是要发给任意浏览器的，加鉴权只会让 service worker
注册流程多一次 401 往返。返回：

```json
{"public_key": "BEl62i...", "subject": "mailto:admin@niki.app"}
```

服务端没配也没法生成时返回 503 + i18n `push.web_vapid_key_unavailable`。

### 3.5 审计

| 事件 | severity | 触发 |
|---|---|---|
| `web_push_delivered` | info | 2xx |
| `web_push_failed` | warning | 4xx / 5xx / 异常 |
| `web_push_dry_run` | info | `dry_run=True` |

metadata：`subscription_id`（device_id）、`endpoint`（**只有 host**，
不含 path——path 段本身就是订阅凭证，写进审计表等于把推送权限
泄露给任何能读审计的人）、`status_code`、`latency_ms`。

#### 顺手修掉的一个潜伏 bug

`AuditLogService.log_event` 把 `metadata` 直接 `**` 展开进 structlog 的
调用里，而它自己已经占用了 `endpoint` / `severity` / `method` 等关键字。
任何 metadata 里带 `endpoint` 的调用方都会撞出
`TypeError: got multiple values for keyword argument 'endpoint'`——
而且因为日志行在 DB 写入**之前**发出，整条审计记录会连带丢失。

修法（`_rename_reserved`）：**只在日志行上**给撞车的 key 加 `meta_` 前缀，
落库的 metadata 保持调用方原样。这样本票据能按票面要求用 `endpoint`
这个 key，其它调用方也不再有这颗地雷。

### 3.6 前端资产

* `niki/static/service-worker.js` — `push` / `notificationclick` /
  `pushsubscriptionchange` 三个监听器。
* `niki/static/push-helper.js` — `urlB64ToUint8Array` + `subscribeToPush`
  示例。真正的前端集成不在本票据范围内，这里只提供可直接 `<script>`
  引的参考实现。
* `niki/main.py` 挂 `/static`，并 `mimetypes.add_type("application/javascript", ".js")`
  —— Windows 注册表里 `.js` 有时被映射成 `text/plain`，显式注册避免
  浏览器拒绝执行 SW。

### 3.7 i18n

`push.web_unsupported_browser` / `push.web_subscription_failed` /
`push.web_vapid_key_unavailable` / `push.web_unsubscribe_failed`
（ZH + EN 双写）。

---

## 4. 不在范围内

* 真实的 ios / android 厂商投递（BE-PUSH-002）。
* 订阅失效（410 Gone）后自动删设备行——需要 repo 侧的删除策略，
  下一票据处理；本票据只把 410 记成 `failed` + warning 审计。
* 前端真正的订阅 UI。
