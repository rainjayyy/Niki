# BE-AUTH-007：家长端 2FA（TOTP + 备份码）

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0（安全） |
| **域** | AUTH |
| **来源** | `docs/frontend-integration.md` §7.1 |
| **工作量** | 2 天 |
| **状态** | 🚧 进行中（2026-07-31） |
| **依赖** | BE-AUTH-002 ✅（SMS 登录）、BE-AUTH-007-rollout ✅（JWT scope）、BE-COMP-004 ✅（`audit_logs`）、BE-API-002 ✅（slowapi） |
| **后续** | BE-AUTH-008-webauthn（硬件密钥 / passkey，作为更高安全等级的升级路径） |

## 背景

家长端目前只有 SMS 验证码登录（BE-AUTH-002）。两个独立的失效
路径足以造成账户失陷：

1. **设备丢失**：捡到设备的攻击者读出 SIM 卡槽或拦截短信通道，
   即可走 `POST /auth/sms/login` 拿到完整 JWT；
2. **短信劫持**：伪基站 / SS7 信令攻击 / 短信嗅探可让攻击者
   在家长不知情的情况下完成同样的流程。

后果：拿到 JWT 即可触发 `POST /auth/deletion/request` 等敏感
操作，30 天冷静期后整个账户（含孩子档案、记忆、同意书）永久
不可恢复地销毁。

任何家长安全模块的最终兜底都应当是「除了你拥有的某样东西之外，
还要证明你是你」。TOTP（RFC 6238，Google Authenticator 等标准
app 全部支持）正是那个「拥有的东西」—— 共享密钥留在家长手机
上，离线生成 30 秒一次的 6 位码。

## 验收标准（AC）

- [ ] **AC-1**：7 个端点 ——
      `POST /api/v1/auth/2fa/enroll`、
      `POST /api/v1/auth/2fa/enroll/verify`、
      `POST /api/v1/auth/2fa/verify`、
      `DELETE /api/v1/auth/2fa`、
      `POST /api/v1/auth/2fa/backup-codes`、
      `GET /api/v1/auth/2fa/status`、
      `POST /api/v1/auth/2fa/login`
- [ ] **AC-2**：TOTP 用 `pyotp`，30 秒步进，±1 step 容差（`valid_window=1`）
- [ ] **AC-3**：10 个备份码、一次性使用、可重新生成
- [ ] **AC-4**：TOTP secret 用 `cryptography.fernet.Fernet` 加密存储
- [ ] **AC-5**：replay 防御 —— 同一 TOTP 码在同一 30 秒窗口内不可重放
- [ ] **AC-6**：敏感操作闸门 —— `POST /auth/deletion/request` 在 2FA
      启用时必须携带 `X-2FA-Code` 或 `X-2FA-Backup-Code`，否则
      401 `TWO_FA_REQUIRED`
- [ ] **AC-7**：每个 2FA 事件写一行 `audit_logs`（event_type 见下表）
- [ ] **AC-8**：`POST /2fa/verify` 限流 5/minute/IP，`POST /2fa/enroll`
      限流 3/hour/IP
- [ ] **AC-9**：测试 ≥ 18 个（`tests/api/test_two_factor.py` + `tests/services/test_two_factor_service.py`），全绿
- [ ] **AC-10**：现有 1775 测试零回归
- [ ] **AC-11**：i18n 中英文条目齐（`MESSAGES_ZH` + `MESSAGES_EN`）
- [ ] **AC-12**：Alembic migration `2026_07_31_0004_two_factor.py` 可
      upgrade / downgrade

### `audit_logs` 事件表

| event_type | severity | 触发时机 |
|------------|----------|---------|
| `2fa_enrollment_started` | info | `POST /2fa/enroll` 成功（生成 secret） |
| `2fa_enrollment_completed` | info | `POST /2fa/enroll/verify` 首次验证成功，2FA 启用 |
| `2fa_verification_success` | info | `POST /2fa/verify` TOTP 验证通过 |
| `2fa_verification_failure` | warning | `POST /2fa/verify` 验证失败 |
| `2fa_disabled` | info | `DELETE /2fa` 成功禁用 |
| `2fa_backup_code_used` | info | 备份码验证通过 |
| `2fa_login_success` | info | `POST /2fa/login` 完成第二段登录 |
| `2fa_login_failure` | warning | `POST /2fa/login` 第二段验证失败 |

## 设计

### 1. 关键决策

#### 1.1 「partial_token」必须可被原 `get_current_parent` 接受

`POST /auth/sms/login` 在 2FA 启用时不再返回 access+refresh，而
是返回 `{status: "2fa_required", partial_token}`。`partial_token`
是一个 5 分钟 TTL、scope=`2fa_pending` 的 access JWT，**其它字段
完全兼容** —— 它的 `sub` 就是 `parent_id`，`typ=access`，只是
多了一个 `scope` claim。

这意味着：

- `GET /auth/2fa/status` 之类的 2FA-相关端点可以继续走
  `Depends(get_current_parent)`，无需新增 dep；
- 「`/2fa/login`」端点用 `get_current_parent` 解析 partial_token，
  然后校验 `scope=="2fa_pending"`，最后调用 TOTP 校验，成功则
  签发新 access + refresh，并把 `scope` 设回 `full`。

`partial_token` 不能用于任何其它端点 —— 这点由
`require_2fa_verified` 这个新 dep 强制：

```python
def require_2fa_verified(request: Request, parent=Depends(get_current_parent)):
    if getattr(request.state, "jwt_scope", None) != "full":
        raise HTTPException(401, "TWO_FA_REQUIRED")
    return parent
```

任何敏感端点只需 `Depends(require_2fa_verified)`。`request.state`
在 `get_current_parent` 里被赋值。

#### 1.2 备份码：bcrypt 而非明文存储

10 个备份码以 **一个 bcrypt hash** 存在 `backup_codes_hash` 列
里。验证逻辑：

```
bcrypt.checkpw(submitted_code.encode(), cfg.backup_codes_hash.encode())
```

成功 → 把该码的索引加入 `backup_codes_used`（JSON list），再次
验证时先查 `backup_codes_used` 跳过 —— 这是「一次性使用」的实际
执行点（bcrypt 不区分大小写且耗时固定，无法对同一 hash 重复匹配
区分「用过的码」和「未用的码」，所以需要显式记下用过的索引）。

10 个码来自 `secrets.choice(ALPHABET)` 循环，ALPHABET 排除掉
`0/O/1/l/I` 这 5 个视觉歧义字符，剩下 57 个 8 位字符 → 候选空间
57^8 ≈ 7×10^13，足够防爆破。

#### 1.3 encryption_key 的生成时机

`Settings.totp_encryption_key` 默认 `None`。第一次启动时
`TwoFactorService` 用 `Fernet.generate_key()` 生成一个 key，写回
`Settings` 实例（**不是 env**，仅 in-memory）。重启会换 key，旧
`two_factor_configs` 里的密文就废了。

生产部署必须在 `.env` 里设置
`NIKI_TOTP_ENCRYPTION_KEY=<urlsafe-base64-32-bytes>` 并保证
跨重启稳定。本工单默认生成在内存里 —— 测试也用同一个 key
实例化，进程内可以解密。

#### 1.4 为什么不强制所有端点都开 2FA

「强制开启」是策略决定，不是工程决定。家长端 app 需要给老用户
一段过渡期，而且部分家长可能在用无 auth-app 的设备。本工单只
提供**能力**——强制开启是 BE-AUTH-008（policy engine）的范畴。

### 2. 数据流：登录

```
       SMS 登录                          2FA 二段登录
┌──────────────────┐               ┌──────────────────────┐
│ POST /auth/sms/login │               │ POST /auth/2fa/login │
└────────┬─────────┘               └─────────┬────────────┘
         │                                     │
         ▼                                     ▼
   SMS 验证通过                          partial_token JWT 解码
         │                                     │
         ▼                                     ▼
  查 2FA 是否启用?                       scope == "2fa_pending"?
    ├── 否 → 直接签发 full token              ├── 否 → 401
    └── 是 → 签发 partial_token (5min, scope="2fa_pending")
                                               │
                                               ▼
                                          verify totp_code
                                               ├── 错 → 401
                                               └── 对 → 重签 full token
                                                          (scope="full")
```

### 3. 数据流：敏感操作

```
POST /auth/deletion/request
  ↓
get_current_parent (验证 JWT)
  ↓
require_2fa_for_sensitive_ops (本工单新增 dep)
  ↓
  2FA 未启用? ── 是 → 直接放行
  ↓
  2FA 启用?
    ├── JWT scope=="full" 且验证时间 ≤ 5min 前 → 放行
    ├── header X-2FA-Code 提供且 verify_code 成功 → 放行 + 升级 scope 为 "full"
    ├── header X-2FA-Backup-Code 提供且 verify_backup_code 成功 → 放行
    └── 都没有 → 401 TWO_FA_REQUIRED
```

「升级 scope 为 full」的实现：`POST /2fa/verify` 在校验成功后
返回一个 **新的 full-token**（短 TTL 5min），前端把它存到
sessionStorage，下一次删除请求把这个 token 换上来即可。
本工单同时支持 header（一次性、单次）和 JWT scope（短时、复用），
给前端两条互不互斥的路径。

## 接口

### `POST /api/v1/auth/2fa/enroll`

需要 `get_current_parent`。返回
`{secret, otpauth_url, qr_code}`。**不启用** 2FA —— 用户必须
再调 `/enroll/verify` 输入第一个 TOTP 码才真正启用。

```json
{
  "secret": "JBSWY3DPEHPK3PXP...",
  "otpauth_url": "otpauth://totp/Niki:p_123?secret=...&issuer=Niki",
  "qr_code": "data:image/svg+xml;base64,..."
}
```

### `POST /api/v1/auth/2fa/enroll/verify`

需要 `get_current_parent`。

请求：`{"totp_code": "123456"}`。校验通过 → 启用 2FA，生成 10 个
备份码（**仅此一次返回**，必须立刻展示给家长）。

```json
{
  "status": "enabled",
  "backup_codes": ["aB3xY9zQ", ...]  // 10 个
}
```

### `POST /api/v1/auth/2fa/verify`

需要 `get_current_parent`。**也接受 partial_token**（路径是
`/2fa/login`，但内部走同一个 `verify_totp` 逻辑；verify 端点本
身仅用于敏感操作 header 场景）。

请求：`{"totp_code": "..."}` 或 `{"backup_code": "..."}`。
成功 → 返回 `{"status": "ok", "scope_token": "<5min full JWT>"}`。

### `DELETE /api/v1/auth/2fa`

需要 `get_current_parent` + password re-auth（`X-Reauth-Password`
header 或 body 字段）。

### `POST /api/v1/auth/2fa/backup-codes`

需要 `get_current_parent` + password re-auth。返回新 10 个码。

### `GET /api/v1/auth/2fa/status`

需要 `get_current_parent`。

```json
{
  "is_enabled": true,
  "enrolled_at": "2026-07-31T10:00:00Z",
  "last_used_at": "2026-07-31T11:00:00Z",
  "backup_codes_remaining": 9
}
```

### `POST /api/v1/auth/2fa/login`

**不需要** `get_current_parent`（要的就是 partial_token）。
请求：`{"partial_token": "...", "totp_code": "..."}`。
成功 → `{access_token, refresh_token, expires_in, scope: "full"}`。

## 实现清单

| 文件 | 作用 |
|------|------|
| `pyproject.toml` | + `pyotp>=2.9.0` |
| `niki/config/settings.py` | + `totp_encryption_key`, `totp_issuer` |
| `niki/db/models/two_factor.py` | `TwoFactorConfig` ORM |
| `niki/db/models/__init__.py` | 导出新模型 |
| `niki/db/repositories/two_factor_repo.py` | ABC + InMemory + Postgres |
| `niki/db/repositories/__init__.py` | 导出 |
| `alembic/versions/2026_07_31_0004_two_factor.py` | 可逆 migration |
| `niki/services/two_factor.py` | `TwoFactorService` (TOTP + 备份码 + Fernet) |
| `niki/auth/jwt_service.py` | + `scope` claim 支持 + `issue_scoped_access_token` |
| `niki/auth/two_factor_gate.py` | `require_2fa_for_sensitive_ops` dep |
| `niki/api/two_factor.py` | 7 个端点 |
| `niki/api/auth.py` | 改造 `sms_login` + 注入 deletion gate |
| `niki/api/router.py` | include new router |
| `niki/i18n/messages.py` | + 8 条 key |
| `tests/db/test_models_metadata.py` | + `two_factor_configs` |
| `tests/api/test_two_factor.py` | ≥ 18 测试 |

## 测试计划（≥ 18）

1. enroll 返回 secret + otpauth URL + QR code（SVG）
2. verify_enrollment 启用 2FA + 返回 10 个备份码
3. verify_code 合法 TOTP 成功
4. verify_code 过期 TOTP 失败（mock time）
5. verify_code 错误 TOTP 失败
6. verify_backup_code 成功 + 把索引标记为 used
7. verify_backup_code 同一码第二次失败
8. disable 错误密码 → 失败
9. disable 正确密码 → 移除 2FA
10. regenerate_backup_codes 旧码失效
11. status 端点展示 enrolled info
12. SMS 登录 + 2FA 启用 → `partial_token` + `2fa_required` status
13. SMS 登录 + 2FA 未启用 → 完整 token（既有行为）
14. `/2fa/login` 合法 TOTP → full token
15. `/2fa/login` 非法 TOTP → 401
16. deletion request 2FA 启用时缺 header → 401 TWO_FA_REQUIRED
17. deletion request + `X-2FA-Code` 正确 → 成功
18. deletion request + `X-2FA-Backup-Code` 正确 → 成功
19. rate limit：第 6 次 verify/minute → 429
20. 每个 2FA 事件都有 audit_logs 行
21. DB 中 `secret_encrypted` ≠ 明文
22. TOTP replay：同码同窗口第二次失败
23. `require_2fa_for_sensitive_ops`：JWT scope=full 且 ≤5min → 通过