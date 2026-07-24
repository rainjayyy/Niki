# BE-AUTH-001：JWT 鉴权中间件

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0 |
| **域** | AUTH |
| **来源** | `docs/frontend-integration.md` §7.1.1 |
| **工作量** | 1-2 天 |
| **状态** | 🚧 进行中（2026-07-24） |
| **依赖** | BE-STOR-001 ✅ |
| **后续** | BE-AUTH-002 /auth/login + BE-AUTH-003 refresh + BE-AUTH-005 SMS 码 |

## 背景

`niki/config/settings.py` 里 `jwt_secret = "change-me-in-production"` 已经定义了，但**全工程零引用**：

```bash
$ grep -rn jwt_secret niki/   # → 0 个业务引用
```

后果：

1. **99 个端点全部无身份校验** ——任何人都能调 `/api/v1/dashboard/...` 拿到任意家长数据
2. **前端拿不到 token** ——家长端注册后无登录态
3. **越权风险** ——跨家长 ID 攻击完全没拦
4. **PIPL 合规** ——未成年人数据需要家长 token 才能访问

本工单**只交付 JWT 基础设施**：

- `jwt_service.py` 签发/校验 token
- `token_blacklist.py` 黑名单（in-memory，Redis 接口预留）
- `dependencies.py` FastAPI `get_current_parent` 依赖
- **演示端点** `GET /api/v1/auth/whoami` 端到端验证
- 全套单测 + 集成测试

**不**做的事（明确延后）：

- ❌ 不批量替换所有端点依赖（→ BE-AUTH-001-rollout）
- ❌ 不实现 `/auth/login` 短信码登录（→ BE-AUTH-002 + BE-AUTH-005）
- ❌ 不接 Redis（→ 性能优化阶段）

## 验收标准（AC）

### 必需完成

- [ ] AC-1：`niki.auth.jwt_service` 提供 `issue_access_token(parent_id, ttl=3600)`, `issue_refresh_token(parent_id, ttl=30d)`, `verify_token(token, expected_type)` 三个公共方法
- [ ] AC-2：HS256 签名，读 `settings.jwt_secret`；token 过期时间在 claim `exp` 里
- [ ] AC-3：token 类型用 claim `typ`（`access` / `refresh`），verify 时校验类型，不允许 access 当 refresh 用
- [ ] AC-4：`niki.auth.dependencies.get_current_parent` 是 FastAPI 依赖，校验 Bearer token，注入 `ParentAccount` / 抛 401
- [ ] AC-5：`niki.auth.token_blacklist` 提供 `revoke(jti)` / `is_revoked(jti)` / `cleanup_expired()`；in-memory 实现 + `RedisTokenBlacklist` 抽象（暂不实现 Redis）
- [ ] AC-6：`GET /api/v1/auth/whoami` 新端点演示依赖使用，返回 `{parent_id, name, status}` 或 401
- [ ] AC-7：`pytest tests/auth/test_jwt.py tests/auth/test_auth_dep.py -v` 全绿
- [ ] AC-8：现有 828 passed 测试**零回归**

### 不在本工单范围

- ❌ 改 `/auth/login`（端点还不存在，留给 BE-AUTH-002）
- ❌ 把依赖应用到 `/dashboard/*` `/memory/*` 等（→ BE-AUTH-001-rollout）
- ❌ Redis 后端实现（→ 性能优化）
- ❌ JWT 私钥轮换策略（→ 生产 SRE 工单）

## 设计

### 1. Token 结构

```json
// Header
{"alg": "HS256", "typ": "JWT"}
// Payload
{
  "sub": "parent_abc123def",   // parent_id
  "typ": "access",             // 'access' | 'refresh'
  "iat": 1700000000,           // issued at
  "exp": 1700003600,           // expiry
  "jti": "uuid-v4"             // unique ID (for blacklist)
}
```

### 2. 模块布局

```
niki/auth/
├── jwt_service.py        # NEW — issue/verify/decode
├── token_blacklist.py    # NEW — in-mem + Redis adapter placeholder
└── dependencies.py       # NEW — FastAPI Depends
```

### 3. 失败模式

| 情况 | 返回 |
|------|------|
| 没 token | 401 `MISSING_TOKEN` |
| token 签名错 | 401 `INVALID_SIGNATURE` |
| token 过期 | 401 `TOKEN_EXPIRED` |
| token 类型错（access 当 refresh） | 401 `WRONG_TOKEN_TYPE` |
| token 在黑名单 | 401 `TOKEN_REVOKED` |
| token 格式错 | 401 `MALFORMED_TOKEN` |

每个失败都返回结构化 `{"error": code, "message": desc}` (matches §6.3 风格)。

### 4. Settings 调整

新增 2 个 settings（保持 backward-compatible default）：

```python
jwt_access_ttl_seconds: int = 3600     # 1 hour
jwt_refresh_ttl_seconds: int = 30 * 24 * 3600  # 30 days
```

### 5. 端到端演示

```bash
# 签发
$ python -c "from niki.auth.jwt_service import issue_access_token; \
            print(issue_access_token('test_parent', ttl=3600))"
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWI...

# 调用
$ curl -H 'Authorization: Bearer eyJ...' http://localhost:8000/api/v1/auth/whoami
{"parent_id": "test_parent", "name": "测试家长", "status": "verified"}

# 拒绝
$ curl http://localhost:8000/api/v1/auth/whoami
401 {"error": "MISSING_TOKEN", "message": "Bearer token required"}
```

## 测试矩阵

| 测试 | 文件 | 覆盖 |
|------|------|------|
| 单元：issue/verify roundtrip | `tests/auth/test_jwt.py` | 签发后能解析 |
| 单元：access 不能 refresh | `tests/auth/test_jwt.py` | 类型错 401 |
| 单元：过期 token | `tests/auth/test_jwt.py` | exp 校验 |
| 单元：bad signature | `tests/auth/test_jwt.py` | secret 错 |
| 单元：blacklist revoke | `tests/auth/test_token_blacklist.py` | 撤销后拒绝 |
| 集成：dep valid token | `tests/auth/test_auth_dep.py` | 200 |
| 集成：dep no token | `tests/auth/test_auth_dep.py` | 401 |
| 集成：dep expired | `tests/auth/test_auth_dep.py` | 401 |
| 集成：dep revoked | `tests/auth/test_auth_dep.py` | 401 |
| 端到端：/auth/whoami 真实请求 | `tests/auth/test_whoami_endpoint.py` | 黑盒 |

## 风险与回滚

| 风险 | 缓解 |
|------|------|
| `jwt_secret` 生产仍是默认值 | 启动时加 `if settings.jwt_secret == "change-me-in-production": logger.warning(...)`，不强制 |
| 横向越权（token A 看家长 B 的数据） | 当前 demo 端点只用 `sub` claim，不接 parent_id 路径参数 |
| In-memory blacklist 重启失效 | 接受；登录数 < 1000 时 Redis 价值不大 |
| 同时支持 Bearer 和 Cookie 登录 | v1 只支持 Bearer |

## 提交流程

1. 单测全绿 + 全套零回归
2. `ruff check niki/auth/jwt_service.py niki/auth/token_blacklist.py niki/auth/dependencies.py`
3. commit：`feat(auth): BE-AUTH-001 JWT signing + verification + FastAPI dependency (scaffolding)`
4. 推分支 `be/BE-AUTH-001`

## 后续工单

- **BE-AUTH-001-rollout**：把 `Depends(get_current_parent)` 应用到 `/dashboard/*` `/memory/*` `/emotion/*` 等 30+ 端点（约 1 天，给现有测试加 `client.app.dependency_overrides`）
- **BE-AUTH-002**：`/auth/login` + 短信码登录（1.5 天）
- **BE-AUTH-003**：`/auth/refresh`（0.5 天）
- **BE-AUTH-004**：`/auth/logout` + blacklist 集成（0.5 天）
