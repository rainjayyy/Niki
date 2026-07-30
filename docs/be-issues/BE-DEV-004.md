# BE-DEV-004: 家长端 ↔ 孩子端配对码

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | DEVICE / AUTH |
| **来源** | 前端对接文档 §2.2 + §4.2 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-DEV-002 ✅ / BE-DEV-003 ✅ / BE-PUSH-001 ✅（注册端点已上） |
| **后续** | BE-PUSH-002 真实厂商推送（确认推送） / 配对码 Redis 化（多 worker） |

## 背景

家长端要绑定孩子的 H5。当前没有 binding 通道：
- 家长不能"扫"孩子的设备
- 孩子的 H5 不知道要代表哪个 parent 发数据
- 家长也不知道"哪台孩子设备刚加入"

按 `docs/frontend-integration.md` §2.2 + §4.2，binding 流程应该是：

1. 家长在 dashboard 点"绑定设备"→ 后端生成 6 位数字码
2. 码展示给孩子（家长拿着手机让孩子输入，或家长扫码——本期简化：手动输入）
3. 孩子 H5 调 `consume(code)` → 后端把 child_id 关联到该 parent
4. 一条确认推送发到家长所有已注册设备

本工单交付：配对码生成 / 消费端点 + 后端 TTL 清理。

## 验收标准（AC）

- [ ] AC-1：`POST /api/v1/pairing/code` 返回 6 位数字码 + 5 分钟 TTL
- [ ] AC-2：`POST /api/v1/pairing/consume` 原子消费；二次消费返回 404
- [ ] AC-3：5 分钟 TTL 过期后消费返回 404
- [ ] AC-4：lateral-auth：parent 必须拥有该 child_id（否则 403）
- [ ] AC-5：`/consume` 端点是公开的（孩子 H5 还没 JWT），靠 5 分钟 TTL 限制爆破
- [ ] AC-6：`InMemoryPairingCodeStore` 线程安全（dict + `threading.Lock`）
- [ ] AC-7：6 位码碰撞防护（生成时回退到下一个空位，最多 1000 次）
- [ ] AC-8：过期清理接入 `HeartbeatService`（`pairing_cleanup`，60s 间隔）
- [ ] AC-9：测试覆盖 ≥ 11 个，含 roundtrip / TTL / single-use / 50 码全唯一 / cleanup / 端点 + lateral-auth
- [ ] AC-10：现有 1381 passed 测试**零回归**

## 设计

### 1. 数据模型

```python
@dataclass
class PairingCode:
    code: str                  # 6 位数字字符串（000000-999999）
    parent_id: str             # 家长 id
    child_id: str              # 孩子设备 id
    created_at: datetime       # UTC aware
    expires_at: datetime       # created_at + 300s
    consumed_at: datetime | None
```

### 2. Store ABC

```python
class PairingCodeStore(ABC):
    def create(self, parent_id, child_id) -> PairingCode: ...
    def consume(self, code: str) -> PairingCode | None: ...   # 原子
    def cleanup_expired(self) -> int: ...
```

`InMemoryPairingCodeStore` 默认实现：`dict[str, PairingCode]` + `threading.Lock`。所有 `create` / `consume` / `cleanup_expired` 都在锁内完成，保证：

- **碰撞防护**：`create` 循环 `randint(0, 999_999)` 直到找到 dict 里没有的 key（最多 1000 次）
- **单次消费**：`consume` 改 `consumed_at` 在锁内，第二次进来直接看到 `is_consumed=True`
- **过期检查**：同一把锁内读 `expires_at`，过期直接返 `None`

### 3. 端点

```python
@router.post("/pairing/code")          # 需 JWT；parent 必须拥有 child
async def create_pairing_code(payload, parent): ...

@router.post("/pairing/consume")       # 公开；只校验 6 位数字格式
async def consume_pairing_code(payload): ...
```

错误码（§6.3 风格）：
- `400 BAD_REQUEST` — body 缺字段 / code 格式错
- `403 PARENT_CHILD_MISMATCH` — parent 不是该 child 的 owner
- `404 CHILD_NOT_FOUND` — child 未注册
- `404 PAIRING_CODE_INVALID` — 码不存在 / 过期 / 已用

### 4. TTL 清理

新增 `niki/infrastructure/heartbeat.py:cleanup_expired_pairing_codes()`，作为 `HeartbeatService` 的一个 checker：

```python
heartbeat.register(
    "pairing_cleanup",
    cleanup_expired_pairing_codes,
    interval_seconds=60,
)
```

返回值跟其它 ping 一致（`status / latency_ms / removed`），错误被吞掉进 `error` 字段，不影响其它 checker。

### 5. 文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `niki/auth/pairing.py` | NEW | `PairingCode` + ABC + `InMemoryPairingCodeStore` + 单例 |
| `niki/api/pairing.py` | NEW | 2 个 FastAPI 端点 |
| `niki/api/router.py` | EDIT | 接入 `pairing_router` |
| `niki/infrastructure/heartbeat.py` | EDIT | 新增 `cleanup_expired_pairing_codes` |
| `niki/main.py` | EDIT | lifespan 注册 `pairing_cleanup` |
| `tests/auth/test_pairing.py` | NEW | 13 个测试 |

### 6. 安全考量

| 风险 | 缓解 |
|------|------|
| 6 位码空间小（10⁶） | 5 分钟 TTL × slowapi 限流，爆破不可行 |
| `/consume` 公开 | 跟"扫码"等价：拿到码 = 拥有临时绑定权；TTL 是唯一防线 |
| 多 worker 内存不共享 | 本期接受（BE-SESSION-redis 已铺好）；后续按 Redis 切（同 ABC 模式） |
| `consume` 重复调用 | 锁内原子标记 `consumed_at`，第二次返 None |

## 后续

- **BE-PUSH-002** 真实厂商推送：`consume` 成功后给该 parent 所有 `enabled=True` 的 device 发一条"新设备绑定"推送
- **BE-DEV-004-ext-Redis**：把 `InMemoryPairingCodeStore` 切到 Redis（多 worker 共享；`SETNX` + 5 分钟 TTL 是天然契合）
- **家长主动解绑**：`DELETE /api/v1/pairing/{child_id}`（解除 binding 但不删 child）
- **配对码限流**：`/code` 加 slowapi 限制（每家长 1 分钟 5 次，防误触刷码）

## 测试

```bash
python -m pytest tests/auth/test_pairing.py -q
```

应通过 13 个测试；全套 `1381 passed, 2 skipped` 基线**零回归**。