# BE-AUTH-002-real：短信码登录接真厂商（阿里云 SMS / 腾讯云 SMS）

| 字段 | 值 |
|------|-----|
| **优先级** | 🔴 P0 |
| **域** | AUTH |
| **来源** | `docs/frontend-integration.md` §7 + `BE-AUTH-002`（mock）后续 |
| **工作量** | 1-2 天（不算阿里云账号申请 / 模板审核的等待时间） |
| **状态** | ❌ 未开始 |
| **依赖** | BE-AUTH-002 ✅（mock 跑通）+ BE-COMP-006 ✅（captcha） |
| **前置商务** | 阿里云 / 腾讯云账号（**个人身份证可注册**）+ 短信签名审核 + 短信模板审核 + 充值 |

## 背景

BE-AUTH-002 用 `InMemorySMSCodeService` 跑通了端到端流程：

- ✅ `/auth/sms/send` → 生成 6 位码 + 内存存 5 分钟
- ✅ `/auth/sms/login` → 校验码 → 发 JWT
- ✅ 限流 3/min/IP + 5 次错误锁定 15 分钟
- ✅ 16 个测试覆盖（TTL / 重发 / 锁定 / 限流）

**但 mock 不真发短信**。`POST /sms/send` 的响应里有 `dev_code: "123456"`，前端拿这个直接登录。**生产环境绝对不能这么干** —— 任何人调 send 就能看到别人的验证码。

`docs/be-issues/BE-AUTH-002.md` AC-2 已经写了「dev 返回 code，prod 只返回 200」，但**没有真实厂商对接**。

## 验收标准（AC）

- [ ] AC-1：`AliyunSMSCodeService`（或 `TencentSMSCodeService`）实现 `SMSCodeService` ABC，`send` 真发短信
- [ ] AC-2：production 配置下 `/sms/send` 响应**不包含** `dev_code` 字段
- [ ] AC-3：SMS code 存 Redis 而非内存（防重启丢，支持多实例共享）
- [ ] AC-4：阿里云签名 / 模板变量通过 settings 注入（`SMS_ACCESS_KEY_ID` / `SMS_ACCESS_KEY_SECRET` / `SMS_SIGN_NAME` / `SMS_TEMPLATE_CODE`）
- [ ] AC-5：阿里云 API 错误处理（`isv.BUSINESS_LIMIT_CONTROL` 限流 / `isv.INVALID_PARAMETERS` 模板错 / 网络超时）映射到 §6.3 错误码
- [ ] AC-6：验证码发送失败时，**不**消耗限流配额（fail-open retry-able）
- [ ] AC-7：Redis 故障时**降级**到 in-memory + 告警（不能挂掉登录）
- [ ] AC-8：测试 ≥ 10 个，包含 mock 阿里云 SDK 的发送断言、Redis 故障降级、错误码映射
- [ ] AC-9：现有 1902 测试零回归
- [ ] AC-10：dev / staging / prod 三环境 SMS 配置隔离（settings 环境变量）

## 设计

### 1. `AliyunSMSCodeService` 抽象

```python
class AliyunSMSCodeService(SMSCodeService):
    """Real SMS provider via Aliyun Dysmsapi.
    
    Requires:
      - settings.sms_provider = "aliyun"
      - settings.sms_access_key_id
      - settings.sms_access_key_secret
      - settings.sms_sign_name (e.g. "Niki")
      - settings.sms_template_code (e.g. "SMS_123456789")
    
    Redis-backed code storage (so multi-instance share the same TTL).
    Falls back to in-memory dict if Redis is unavailable, with a
    `warning`-severity audit event so ops sees the degradation.
    """

    async def send(self, phone: str) -> str:
        # 1. Generate 6-digit code.
        # 2. Store in Redis with 300s TTL.
        # 3. Call aliyun client.send_sms(phone, sign, template, code).
        # 4. On success → return code (in dev) or "" (in prod).
        # 5. On failure → raise AliyunSMSError subclass.

    async def verify(self, phone: str, code: str) -> bool:
        # Lookup Redis; if miss → SMS_CODE_EXPIRED.
        # Compare; if match → pop from Redis + reset attempts.
```

### 2. SMS 模板（要在阿里云后台审核）

```
【Niki】您的验证码是 ${code}，5 分钟内有效。请勿向他人泄露。
```

变量：`${code}` 由后端替换。

### 3. 阿里云 SDK 调用

```python
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from aliyunsdkdysmsapi.request.v20170525 import SendSmsRequest

client = AcsClient(
    settings.sms_access_key_id,
    settings.sms_access_key_secret,
    "cn-hangzhou",  # SMS API region
)
request = SendSmsRequest.SendSmsRequest()
request.set_PhoneNumbers(phone)
request.set_SignName(settings.sms_sign_name)
request.set_TemplateCode(settings.sms_template_code)
request.set_TemplateParam(json.dumps({"code": code}))
response = client.do_action_with_exception(request)
# response is bytes; parse JSON
body = json.loads(response)
# body["Code"] == "OK" on success
```

### 4. 配置注入

`niki/config/settings.py`:

```python
class Settings(BaseSettings):
    # SMS provider
    sms_provider: str = "mock"  # "mock" | "aliyun" | "tencent"
    sms_access_key_id: str | None = None
    sms_access_key_secret: str | None = None
    sms_sign_name: str | None = None  # e.g. "Niki"
    sms_template_code: str | None = None  # e.g. "SMS_123456789"
    
    # dev echo control
    echo_sms_code_in_response: bool = False  # 必须 prod=false
```

### 5. 启动时校验

```python
def validate_sms_config():
    if settings.sms_provider == "aliyun":
        for f in (sms_access_key_id, sms_access_key_secret, 
                  sms_sign_name, sms_template_code):
            assert f, f"missing SMS config: {f}"
```

`create_app()` startup 时跑一次，缺配置直接抛错（fail-fast）。

### 6. Redis 降级

```python
async def _store_code(self, phone, code):
    try:
        await self._redis.setex(f"sms:{phone}", 300, code)
    except RedisConnectionError:
        logger.warning("sms.redis_unavailable", fallback="in_memory")
        # Fallback: in-memory dict with same TTL
        self._fallback[phone] = (code, time.time() + 300)
```

登录流程**不阻塞**：Redis 挂了让用户能登录，但每次降级写一条 `audit_logs` 警告，运维收到告警。

## 商务 / 账号清单（你做）

| 步骤 | 时间 | 在哪 |
|---|---|---|
| 1. 注册阿里云账号（**个人身份证**可注册） | 5 分钟 | https://cn.aliyun.com |
| 2. 实名认证（上传身份证正反面） | 1-2 工作日 | 阿里云控制台 |
| 3. 开通短信服务 Dysmsapi | 立即 | 阿里云市场 |
| 4. 申请短信签名「Niki」 | 1-2 工作日 | 短信服务控制台 |
| 5. 申请短信模板（验证码） | 1-2 工作日 | 短信服务控制台 |
| 6. 创建 AccessKey ID + Secret | 5 分钟 | RAM 控制台 |
| 7. 充值 ¥100（覆盖内测期） | 立即 | 阿里云充值 |
| 8. 把上面 6 个值填到 `.env` 或 deployment secrets | 5 分钟 | 部署系统 |

**总等待时间**：1-2 个工作日（步骤 4 + 5）

## 测试设计

- `test_aliyun_send_calls_sdk_with_correct_params` - mock aliyun client，验证参数
- `test_aliyun_send_response_omits_code_in_prod` - prod 配置下响应无 dev_code
- `test_aliyun_send_includes_dev_code_in_dev_mode` - dev 配置下保留 dev_code（方便联调）
- `test_redis_failure_falls_back_to_memory` - Redis 抛错时用内存
- `test_redis_failure_writes_audit_warning` - 降级审计日志
- `test_aliyun_business_limit_returns_429` - 限流错误码映射
- `test_aliyun_invalid_template_returns_500` - 模板错误码映射
- `test_aliyun_timeout_retries_3_times` - 网络超时重试
- `test_sms_code_stored_in_redis_not_memory` - 验证存储路径
- `test_sms_code_removed_after_verify` - 验证后立即清除
- `test_missing_sms_config_at_startup_raises` - 启动时缺配置 fail-fast
- `test_phone_format_validation_before_provider_call` - 错误格式不调阿里云

## 阻塞原因

| 阻塞 | 说明 |
|---|---|
| 阿里云账号申请 | 个人身份证 5 分钟 |
| **签名审核** | 1-2 工作日（必须） |
| **模板审核** | 1-2 工作日（必须） |
| 充值 | ¥100 起 |

**纯代码工时**：1-2 天后端（含测试）。

## 替代方案

如果暂时不想接阿里云：
- 当前 `InMemorySMSCodeService` 已能跑通 demo / 内测
- 在 `Settings.sms_provider = "mock"` 下，dev_code 会返回（前端能拿到），方便前端联调
- 生产部署前必须改成 `sms_provider = "aliyun"` 并填 6 个环境变量

## 当前状态（2026-07-31）

`BE-AUTH-002`（mock）已完成（commit `fd78047`，16 个测试通过）。
本工单 `BE-AUTH-002-real` 是它的真实化 follow-up。

## 风险

| 风险 | 缓解 |
|---|---|
| 短信被刷（成本攻击） | 限流 3/min/IP + 5 次错误锁定 15 分钟 + 阿里云侧配额 |
| AccessKey 泄露 | 用 RAM 子账号，只授权 Dysmsapi；存到 secret manager |
| 验证码泄漏（前端日志） | 响应里 prod 不带 code；前端不打印 |
| 国际短信费用（误发） | BE-AUTH-002 已有 phone 格式校验，只发中国大陆号 |