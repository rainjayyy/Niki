# BE-DEV-003：多端同时登录冲突处理

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | DEVICE / AUTH |
| **来源** | 前端对接文档 §2.2 + §7.1.2 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-DEV-002 ✅（设备清单） |
| **后续** | BE-PUSH-002（5 厂商适配） |

## 背景

§2.2 明确：**同一家长账户同一时刻只能激活一个家长端**。但当前：
- 家长 A 在手机登录 → 后端签发 token
- 家长 A 在平板也登录 → 又签发一个 token
- 两个设备都活，dashboard 重复推送
- 谁是 "主设备"？不清楚

本工单加**"踢下线"机制** + 活跃 session 追踪。

## 验收标准（AC）

- [ ] AC-1：登录时检查活跃 device_count，超过 1 → 自动踢最早的
- [ ] AC-2：`GET /devices/active` 返回当前活跃设备
- [ ] AC-3：`POST /devices/{device_id}/kick` 立即禁用（前端可远程踢自己其他设备）
- [ ] AC-4：测试覆盖 ≥ 4 个
- [ ] AC-5：现有 1355 passed 测试**零回归**

## 设计

### 1. Active flag

`push_devices.enabled` 字段已存在。利用：
- 登录时：新 device upsert 后 `enabled=True`
- 旧设备的 `enabled` 在新设备上线时设为 False（push 不再发）
- "踢" 直接 `enabled=False`

### 2. 端点

```python
@router.get("/active")
async def get_active_device(parent): ...

@router.post("/{device_id}/kick")
async def kick_device(device_id, parent):
    # lateral-auth: device must belong to parent
    await repo.set_enabled(parent.id, device_id, False)
    return {"kicked": True}
```

### 3. 多端登录自动轮换

`POST /push/register` 已经 upsert（按 device_id）。当同一 parent 多个 device 注册时：
- 全部 `enabled=True` → 多端活跃
- 解决：login 时只允许一个 active；超过则禁用最旧

本期简化：让前端 dashboard 控制（提供 GET /active + POST /kick），后端不强制。后端只是状态机。

## 后续

- BE-PUSH-002 真实推送时只发给 `enabled=True` 的设备
- BE-DEV-004 配对码
