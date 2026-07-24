# BE-AUTH-002：短信码登录（mock provider）

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0 |
| **域** | AUTH |
| **来源** | `docs/frontend-integration.md` §7.1.1 + §7.2.1 (BE-AUTH-005) |
| **工作量** | 1-1.5 天（合并 BE-AUTH-002 + BE-AUTH-005 mock） |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-AUTH-001 ✅ |
| **后续** | BE-AUTH-003 refresh + BE-AUTH-004 logout + BE-AUTH-005-real 接阿里云/腾讯云 |

## 背景

BE-AUTH-001 提供了 JWT 签发 + FastAPI 依赖，但前端家长端拿不到 token：
- 没有 `/auth/login` 端点 → 注册后家长无法登录
- 没有 SMS 通道 → 无法做"手机号 + 验证码"主流方案

直接接阿里云 / 腾讯云 SMS 需要采购 + 资质 + 三方 secret 提交，本工单**先用 mock provider** 让登录端到端跑通，real provider 留给后续工单。

## 验收标准（AC）

- [ ] AC-1：`SMSCodeService` ABC + `InMemorySMSCodeService` 默认实现；5 分钟 TTL；每个手机号最多 5 次尝试；重复发送不重置 TTL
- [ ] AC-2：`POST /api/v1/auth/sms/send` 接受 `{phone}`，调用 SMS service，dev 返回 code，prod 只返回 200
- [ ] AC-3：`POST /api/v1/auth/sms/login` 接受 `{phone, code}`，校验通过后发 JWT 对
- [ ] AC-4：登录成功返回 `{access_token, refresh_token, token_type: "Bearer", expires_in: 3600, parent_id}`
- [ ] AC-5：登录成功 + phone 已注册 → 复用现有账号；phone 未注册 → 自动创建 PENDING 账号
- [ ] AC-6：错误码统一：`INVALID_PHONE_FORMAT` / `SMS_CODE_INVALID` / `SMS_CODE_EXPIRED` / `SMS_TOO_MANY_ATTEMPTS`
- [ ] AC-7：单元 + 集成测试 ≥ 8 个，全绿
- [ ] AC-8：现有 865 测试**零回归**

## 设计

### 1. 端点

```
POST /api/v1/auth/sms/send
Body: { "phone": "13800138000" }
→ 200 { "phone": "13800138000", "dev_code": "123456" }   ← dev only
→ 400 { "error": "INVALID_PHONE_FORMAT" }
→ 429 { "error": "SMS_RATE_LIMITED" }                    ← 60s 节流

POST /api/v1/auth/sms/login
Body: { "phone": "13800138000", "code": "123456" }
→ 200 {
    "access_token": "eyJ...",
    "refresh_token": "eyJ...",
    "token_type": "Bearer",
    "expires_in": 3600,
    "parent_id": "abc123..."
  }
→ 401 { "error": "SMS_CODE_INVALID" } | "SMS_CODE_EXPIRED" | ...
```

### 2. 触发流程

```
[前端]                    [后端]                   [业务]
  POST /sms/send    --->  SMSCodeService.send()
                          ├─ 生成 6 位数字
                          ├─ mock 发送 (logger.info("sms.send", code))
                          ├─ 存入 TTL=5min dict
                          └─ 60s 同号节流 (节流另起 task)

  POST /sms/login   --->  SMSCodeService.verify()
                          ├─ 失败 → 4xx + increment attempt counter
                          └─ 成功 → AccountLifecycle.find_by_phone()
                                     ├─ 找到  → 返回现有
                                     └─ 没找到 → start_signup() 自动建 PENDING
                          → 调 jwt_service.issue_access/refresh_token
                          → 返回 JWT 对 + parent_id
```

### 3. 安全约束

| 风险 | 缓解 |
|------|------|
| 暴力枚举 6 位码 | 5 次/手机号/TTL；超限冻结账号 30min |
| 短信轰炸 | 60s 同号节流（单手机 1 次/分钟） |
| dev 模式回 code 到响应 | dev=debug 模式；prod=不回 |
| 验证码不存日志 | `logger.info("sms.send", phone_masked=..., NOT code)` |

### 4. Auto-create 行为

PRD 没明说，但行业惯例是"用户首次登录即注册"。本工单临时方案：

```python
account = await lifecycle.find_by_phone(phone)
if account is None:
    # Auto-create: phone 已校验格式，name/id_number 用占位值
    account, _ = await lifecycle.start_signup(
        name=f"家长{phone[-4:]}",
        id_number="110101199003078811",  # placeholder; we validate format
        phone=phone,
    )
```

注意：`start_signup()` 里 `validate_id_number` 接受 18 位数字。占位用同一个合法号码即可让流程跑通。

后续 BE-AUTH-002-follow-up：登录成功后引导家长去补完实名 + 人脸 + 同意书，状态从 PENDING 升到 VERIFIED。

## 不在本工单

- ❌ 接入阿里云 / 腾讯云 SMS 服务商（→ BE-AUTH-005-real，采购 + 资质 + 0.5 天）
- ❌ `/auth/refresh`（→ BE-AUTH-003，0.5 天）
- ❌ `/auth/logout` + blacklist 失效（→ BE-AUTH-004，0.5 天）
- ❌ 60s 节流的持久化（in-memory 已够 PoC）

## 后续工单

- **BE-AUTH-003**：`/auth/refresh` 用 refresh_token 换发新 access_token
- **BE-AUTH-004**：`/auth/logout` 把当前 access_token 加入 blacklist
- **BE-AUTH-005-real**：阿里云 / 腾讯云 SMS provider 适配
- **BE-AUTH-002-followup**：登录后引导实名补完流程
