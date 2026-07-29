# BE-AUTH-001-rollout-alerts-rollout：飞书 / 邮件双通道告警

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | SECURITY |
| **来源** | 前端对接文档 §7.1.3（推送通道 P0 的告警后端） |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | `BE-AUTH-001-rollout-alerts`（webhook sink 已上）✅ |
| **后续** | 飞书机器人 / 邮件服务真实接入（→ BE-AUTH-001-rollout-alerts-rollout-rollout） |

## 背景

`BE-AUTH-001-rollout-alerts`（上一工单）已经做了：
- 越权 5 分钟 3 次 → 触发 SecurityAlert
- 单 sink：Webhook（飞书 / Slack 通用 webhook 协议）
- Settings: `auth_alert_webhook_url`

但**只有 1 个 sink**。生产应该：
- 多个 sink 互补（飞书 + 邮件，避免单点）
- 失败 graceful degradation（某个 sink 挂了，另一个还能投）
- fan-out 拓扑（一次告警，多个 sink 同时收）

## 验收标准（AC）

- [ ] AC-1：新增 `EmailAlertSink` 走 SMTP / SES / SendGrid 通用（用 stdlib `smtplib`）
- [ ] AC-2：`CompositeAlertDispatcher` 改为**真正 fan-out**（之前是顺序调用第一个成功的）
- [ ] AC-3：单 sink 失败不阻塞其他 sink
- [ ] AC-4：Settings 加 `auth_alert_email_to` / `auth_alert_smtp_host` / `_port` / `_username` / `_password`
- [ ] AC-5：测试覆盖 ≥ 6 个，含：fan-out / 单 sink 失败不影响其他 / 邮件格式
- [ ] AC-6：现有 1273 passed 测试**零回归**

## 设计

### 1. EmailAlertSink

```python
class EmailAlertSink(AlertDispatcher):
    def __init__(self, *, smtp_host, smtp_port, username, password,
                 from_addr, to_addrs: list[str], timeout=5):
        ...
    
    async def dispatch(self, alert: SecurityAlert) -> None:
        # Build MIMEText email with subject
        # SMTP_SSL / SMTP + login + sendmail
        # All in a thread pool (smtplib is sync)
```

主题模板：`[Niki 安全告警] {alert.alert_type} - {actor_parent_id or 'unknown'}`

正文：标题 + 详情 + structlog JSON 行

### 2. CompositeAlertDispatcher 升级

```python
class CompositeAlertDispatcher(AlertDispatcher):
    async def dispatch(self, alert: SecurityAlert) -> None:
        tasks = [
            asyncio.create_task(self._safe_dispatch(s, alert))
            for s in self._dispatchers
        ]
        # gather with return_exceptions=True — collect all outcomes
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for s, r in zip(self._dispatchers, results):
            if isinstance(r, Exception):
                logger.error("alert.sink_failed", sink=..., error=str(r))
```

并行 fan-out，单 sink 失败不影响其他。

### 3. Settings

```python
class Settings:
    # BE-AUTH-001-rollout-alerts-rollout
    auth_alert_email_to: str = ""          # 逗号分隔
    auth_alert_smtp_host: str = ""
    auth_alert_smtp_port: int = 587
    auth_alert_smtp_username: str = ""
    auth_alert_smtp_password: str = ""
    auth_alert_email_from: str = "niki-alerts@example.com"
```

### 4. 风险

| 风险 | 缓解 |
|------|------|
| SMTP 慢阻塞 event loop | `asyncio.to_thread` 包裹 smtplib |
| SMTP 凭据泄漏 | 走 env / secrets manager（不写代码） |
| 邮件被当垃圾邮件 | 用 `niki-alerts@` 子域 + DKIM 配 SPF |
| 邮件重复发送 | 当前告警触发本身有速率限制（5min 内同 actor 一次），够用 |

### 5. 不在本工单

- ❌ 飞书原生 SDK（webhook 已够）
- ❌ 邮件模板化（→ BE-DASH-001-rollout）
- ❌ 飞书机器人（@Niki 报警）—— 后续

## 后续工单

- **BE-AUTH-001-rollout-alerts-rollout-rollout**：飞书机器人 + 邮件签名 + 告警聚合（5 分钟内合并）
- **BE-ADMIN-001**：实时告警看板