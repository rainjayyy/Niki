# BE-COMP-006：图形验证码 / Turnstile 验证层

| 字段 | 值 |
|------|-----|
| **优先级** | 🟠 P0（合规 / 风控） |
| **域** | COMPLIANCE / AUTH |
| **来源** | 风控 review（2026-07-30）— SMS provider 计费风险 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-AUTH-002 ✅（SMS service 已上） |
| **后续** | BE-COMP-006-real 接 阿里云 / 网易 / Cloudflare Turnstile |

## 背景

今天**唯一**的 rate-limit 是 SMS 验证码本身：60s 一次 send，每号 5 次重试。

后果：攻击者可以疯狂 `POST /api/v1/auth/sms/send`，直到短信厂商账单爆掉。
PIPL Art. 51 + 工信部 164 号文都要求"防止自动化攻击滥用通信通道"。

本工单补一个**可插拔**的 captcha 层：

- v1：**算术题**（"3 + 4 = ?"）— 0 外部依赖、足够挡 95% 脚本
- v2（follow-up）：替换 backend 为 阿里云 / 网易 / Cloudflare Turnstile

API 表面**不变**，所以前端不用改。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/auth/captcha/challenge` 返回 `captcha_id` + `question` + `expires_at`
- [ ] AC-2：`POST /api/v1/auth/captcha/verify` 返回 `{valid: true/false}`
- [ ] AC-3：answer **不存明文**，只存 SHA-256 哈希
- [ ] AC-4：5 分钟 TTL 强制
- [ ] AC-5：单进程最大 1000 条，超过 drop 最旧
- [ ] AC-6：`/auth/sms/send` 在 dev（默认）下 captcha 可选
- [ ] AC-7：`/auth/sms/send` 在 `sms_captcha_required=True` 时缺 captcha → 400 `CAPTCHA_INVALID`
- [ ] AC-8：`Settings.sms_captcha_required: bool = False`
- [ ] AC-9：测试覆盖 ≥ 9 个
- [ ] AC-10：现有 1453 passed + 2 skipped 测试**零回归**

## 设计

### 1. `niki/auth/captcha.py`

```python
@dataclass
class CaptchaChallenge:
    id: str               # uuid4 hex
    question: str         # "3 + 4 = ?"
    answer_hash: str      # sha256(answer)
    expires_at: float     # time.time() + ttl

class CaptchaService(ABC):
    def create_challenge() -> CaptchaChallenge
    def verify(challenge_id, answer) -> bool     # 用一次即销毁

class InMemoryCaptchaService(CaptchaService):
    dict + asyncio.Lock
    ttl=300s, cap=1000 (drop oldest)

get_captcha_service() / set_captcha_service()  # 同 SMS 的 singleton 模式
```

算术题生成：

```python
a = random.randint(1, 9); b = random.randint(1, 9)
op = random.choice(["+", "-", "*"])
# "*" 限制在 1..9（孩子向，成人向安全范围）
answer = eval(f"{a} {op} {b}")
```

### 2. `niki/api/auth.py`

加两个端点：

```python
@router.get("/captcha/challenge")
async def captcha_challenge() -> dict:
    ch = await get_captcha_service().create_challenge()
    return {"captcha_id": ch.id, "question": ch.question, "expires_at": ch.expires_at}

@router.post("/captcha/verify")
async def captcha_verify(payload: dict) -> dict:
    valid = await get_captcha_service().verify(payload["captcha_id"], payload["answer"])
    return {"valid": valid}
```

`/auth/sms/send` 改造：

```python
settings = get_settings()
if settings.sms_captcha_required:
    cid = payload.get("captcha_id")
    ans = payload.get("captcha_answer")
    if not cid or ans is None:
        raise HTTPException(400, detail={"error": "CAPTCHA_INVALID", ...})
    ok = await get_captcha_service().verify(cid, str(ans))
    if not ok:
        raise HTTPException(400, detail={"error": "CAPTCHA_INVALID", ...})
```

Dev 模式（flag off）：captcha 可选，但**如果传了**，仍走 verify（便于前端测试）。

### 3. `Settings.sms_captcha_required: bool = False`

放在 `# --- BE-COMP-006 ---` 区块。

### 4. 多进程

v1 是 **process-local**。多 worker 需要 Redis pub/sub 共享 answer store。
→ `BE-COMP-006-real` 工单。

## 改动文件

- **NEW** `niki/auth/captcha.py`（~120 行）
- **EDIT** `niki/api/auth.py`（+ 30 行）
- **EDIT** `niki/config/settings.py`（+ 4 行）
- **NEW** `tests/auth/test_captcha.py`（≥ 9 个测试）

## 后续工单

- **BE-COMP-006-real**：接 阿里云 / 网易 / Cloudflare Turnstile，
  共享 Redis 答案存储，process-local → distributed。