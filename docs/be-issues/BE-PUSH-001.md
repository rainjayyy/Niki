# BE-PUSH-001：推送通道注册端点

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0 |
| **域** | PUSH |
| **来源** | 前端对接文档 §7.1.3 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-AUTH-001 ✅ / BE-STOR-001 ✅ |
| **后续** | BE-PUSH-002（5 厂商适配，P1）/ BE-PUSH-003（Web Push，P2） |

## 背景

家长端需要接收高危告警（PRD #29 自残/自伤信号）的实时推送。但目前后端**完全没有 push 通道注册端点**：

- 家长端拿不到 `device_id` 持久化
- 后端不知道该往哪台设备推
- 没有偏好设置（静默时段 / 勿扰）

后果：家长端只能在 H5 里**轮询** `/dashboard/alerts/{child_id}`，延迟 30s+；App 端推送完全断。

本工单补齐注册端点 + 偏好设置（**先做基础设施**，实际推送通道适配在 `BE-PUSH-002` / `BE-PUSH-003`）。

## 验收标准（AC）

- [ ] AC-1：`POST /api/v1/push/register` 接受 `{device_id, channel, push_token, platform}`，返回 `{id, device_id, channel, created_at}`
- [ ] AC-2：`POST /api/v1/push/unregister` 接受 `{device_id}` 或 `{push_token}`，返回 `{unregistered: true}`
- [ ] AC-3：`GET /api/v1/push/devices` 列出当前 parent_id 的所有注册设备
- [ ] AC-4：`GET /api/v1/push/prefs` 返回默认 prefs（首次访问）
- [ ] AC-5：`PATCH /api/v1/push/prefs` 接受 `{quiet_start?, quiet_end?, enabled_channels?, high_risk_only?}`，部分更新
- [ ] AC-6：`channel` enum 校验：`hms` / `mipush` / `oppo` / `vivo` / `flyme` / `apns` / `fcm` / `web`
- [ ] AC-7：`push_token` 唯一索引（防止重复注册）
- [ ] AC-8：测试覆盖 ≥ 6 个，含：注册成功 / 重复注册同 device_id → 更新 token / unregister / list / prefs PATCH 校验
- [ ] AC-9：现有 1186 passed 测试**零回归**

## 设计

### 1. ORM model

```python
class PushDevice(Base):
    __tablename__ = "push_devices"
    id: Mapped[str]  # UUID
    parent_id: Mapped[str]  # FK parents.id CASCADE
    device_id: Mapped[str]  # client-supplied (X-Device-Id)
    channel: Mapped[str]  # enum
    push_token: Mapped[str]  # vendor token
    platform: Mapped[str]  # ios|android|web|weapp|flutter
    enabled: Mapped[bool]  # soft-delete / opt-out
    last_active_at: Mapped[datetime]
    created_at: Mapped[datetime]
    # UNIQUE (device_id) — 同一 device 重复注册 = 更新 token
    # UNIQUE (push_token) — 同一 token 不能绑多 device

class PushPrefs(Base):
    __tablename__ = "push_prefs"
    parent_id: Mapped[str]  # PK
    quiet_start: Mapped[int | None]  # 0-23
    quiet_end: Mapped[int | None]  # 0-23
    enabled_channels: Mapped[list[str]]  # JSON
    high_risk_only: Mapped[bool]
    updated_at: Mapped[datetime]
```

### 2. 端点

```
POST /api/v1/push/register
Body: { device_id, channel, push_token, platform }
→ 200 { id, device_id, channel, platform, created_at }

POST /api/v1/push/unregister
Body: { device_id } OR { push_token }
→ 200 { unregistered: true }   # 找不到也返回 true（幂等）

GET  /api/v1/push/devices
→ 200 { devices: [{ id, device_id, channel, platform, last_active_at, created_at }] }

GET  /api/v1/push/prefs
→ 200 { quiet_start, quiet_end, enabled_channels, high_risk_only }

PATCH /api/v1/push/prefs
Body: { quiet_start?, quiet_end?, enabled_channels?, high_risk_only? }
→ 200 { quiet_start, quiet_end, enabled_channels, high_risk_only }
```

### 3. 安全

- 所有端点 require Bearer token
- `parent_id` 从 token 解析（不接 body 参数）→ 防越权
- `device_id` 由客户端生成 UUID，服务端只校验格式
- `channel` enum 校验防 SQL injection
- `push_token` 不返回（注册响应里只给 `id`，详情见 `GET /push/devices` 时也 mask）

### 4. 后续工单

- **BE-PUSH-002**：5 厂商适配（HMS / MiPush / OPPO / Vivo / Flyme），把 push_token 真正发到厂商服务器
- **BE-PUSH-003**：Web Push（VAPID），浏览器后台推送
- **BE-PUSH-004**：通知中心 `GET /notifications?cursor=`
- **BE-PUSH-005**：推送测试端点 `POST /push/test`