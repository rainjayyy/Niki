# BE-AUTH-006：换绑手机 / 换绑邮箱

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0（合规 / 账号自助） |
| **域** | AUTH |
| **来源** | `docs/frontend-integration.md` §7.1 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-31） |
| **依赖** | BE-AUTH-002 ✅（SMS 验证码）、BE-COMP-004 ✅（`audit_logs`）、BE-API-002 ✅（slowapi） |
| **后续** | BE-AUTH-008（WebAuthn / passkey） |

## 背景

家长在用完我们的产品后，最容易踢到铁板的一个场景就是——「我
换号/换邮箱了，怎么改？」。现行 `/auth/sms/login` 的全部账户
身份都绑在手机号上，没有自助换号路径，家长只能走工单让客服手
工改（极慢，且容易踩 PIPL 数据最小化的红线——客服为了核对身
份通常要看到家长提供的身份证号或人脸，永久留下 PII）。

合规侧的要求（PIPL、GDPR-Art.16）也要求提供「及时」的自助修
正路径，否则属于「无技术手段保障数据主体权利」。

本工单提供 5 个端点：

- 换绑手机：发 SMS 验证码到新号 + 7 天冷静期 + 状态可追踪
  可取消。冷静期结束（cron / 手动）后真正落地。
- 换绑邮箱：发验证码到新邮箱（v1 = 直接日志），立即生效，无
  冷静期（邮箱不是登录身份，仅用于通知与找回）。

## 设计要点

1. **新表 `phone_change_requests`** —— 一请求一记录，方便审计、
   取消、过期 / 冷静期 sweep。
   - 状态机：`pending_verification` → `pending_cooling` →
     `completed`；`pending_verification` / `pending_cooling` 阶段
     可 `cancelled`；`pending_verification` 阶段 10 分钟未验证则
     标记 `expired`。
   - 索引：`(parent_id, status)`、`(effective_at, status)` for
     sweep。
2. **7 天冷静期** —— 普通用户走完后自动换号；想立刻换的可调
   `POST /phone/change/confirm-now`，但同时写一条
   `severity=warning` 审计事件（前端的「我已了解风险」勾选对应
   此路）。
3. **邮箱** —— 只加 `email_hash` + `email_verified` 两列到
   `parents`，新邮箱立即覆盖旧值，验证码 10 分钟 TTL。
4. **审计** —— 复用 `audit_logs`（BE-COMP-004），事件类型如下：
   `phone_change_requested`、`phone_change_verified`、
   `phone_change_completed`、`phone_change_cancelled`、
   `email_change_requested`、`email_change_completed`。
5. **限流** —— `/phone/change/request` 与 `/email/change/request`
   各 3/minute/IP。

## 端点

```
POST /api/v1/auth/phone/change/request
POST /api/v1/auth/phone/change/verify
POST /api/v1/auth/phone/change/{request_id}/cancel
POST /api/v1/auth/email/change/request
POST /api/v1/auth/email/change/verify
```

`POST /phone/change/confirm-now` 在前端不在第一版要求里，但留
service 方法方便 cron 之外的 admin 入口触发。

## 数据模型

`phone_change_requests`：

| 列 | 类型 | 说明 |
|----|------|------|
| id | UUID PK | |
| parent_id | string(16) FK | |
| old_phone_hash | string(64) | 申请时的旧号哈希（事后定位） |
| new_phone_hash | string(64) | 新号哈希（脱敏对比） |
| verification_code | string(6) | 缓存到 verify 步骤用 |
| requested_at | timestamptz | |
| expires_at | timestamptz | requested_at + 10min |
| effective_at | timestamptz | requested_at + 7d（verify 后填） |
| status | string(16) | enum |
| confirmed_at | timestamptz? | |
| completed_at | timestamptz? | sweep / confirm-now 后填 |
| cancelled_at | timestamptz? | |

`parents` 加列：

| 列 | 类型 | 默认 |
|----|------|------|
| email_hash | string(64)? | NULL |
| email_verified | bool | false |

## 验收标准（AC）

- [ ] **AC-1**：5 个端点全部暴露 + `Sweep-Cancel` 工具端点
- [ ] **AC-2**：7 天冷静期 cron + `confirm-now` 快捷
- [ ] **AC-3**：每个状态迁移写一行 `audit_logs`
- [ ] **AC-4**：`phone/change/request` 与 `email/change/request`
      限流 3/minute/IP，slowapi
- [ ] **AC-5**：横向鉴权 —— 家长 A 不能 cancel 家长 B 的 request
- [ ] **AC-6**：测试 ≥ 18，全部绿；零回归（1880 已存在）
- [ ] **AC-7**：i18n key 在 `MESSAGES_ZH` 与 `MESSAGES_EN` 都有
- [ ] **AC-8**：`alembic upgrade` + `downgrade` 可来回切
- [ ] **AC-9**：`PhoneChangeStatus` 是严格枚举（4 个值）

## 反向不变量

- 新手机号与原手机号相同 → 409 `PHONE_CHANGE_SAME_NUMBER`
- 新邮箱已被另一账户占用 → 409 `EMAIL_ALREADY_IN_USE`
- 同时只能有一个 in-flight（非 cancelled、非 expired、非
  completed）的 phone change request；命中已有 → 409
  `PHONE_CHANGE_ALREADY_PENDING`
- 验证短信 10 分钟过期，过期后 verify → 400
  `PHONE_CHANGE_CODE_EXPIRED`
