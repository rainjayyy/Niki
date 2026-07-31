# BE-PUSH-004：通知偏好设置（静默时段 / 勿扰 / 严重度过滤）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟠 P1 |
| **域** | PUSH |
| **来源** | 前端对接文档 §7.3.2 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-31） |
| **依赖** | BE-PUSH-001 ✅ / BE-I18N-001 ✅ |
| **后续** | BE-PUSH-002（5 厂商适配）/ BE-PUSH-005（推送测试） |

## 背景

BE-PUSH-001 已落 `quiet_start` / `quiet_end` / `enabled_channels` / `high_risk_only` 四个字段，但**没有真正实现行为**：

- 静默时段没有 wrap-around 语义（22→7 跨天）
- 缺「勿扰」开关（DND / 临时静音 N 小时）
- 缺「按严重度过滤」（只收 warning + critical）
- 缺「按事件类型静音」（屏蔽某个事件）

**业务痛点**：21:00-07:00 时段推送扰民已被投诉（来源：`docs/frontend-integration.md` §7.3.2）。

本工单把 `PushPrefs` 扩展到 8 个字段，并补 2 个 helper（`is_quiet_now` / `should_deliver`），给后续 BE-PUSH-002 / BE-PUSH-005 复用。

## 验收标准（AC）

- [ ] AC-1：`PushPrefs` 共 8 字段：`quiet_start`, `quiet_end`, `enabled_channels`, `high_risk_only`, `dnd_enabled`, `dnd_until`, `severities`, `muted_event_types`
- [ ] AC-2：`PATCH /api/v1/push/prefs` 接受全部 8 字段，返回 200 + 完整 prefs
- [ ] AC-3：验证失败返回 §6.3 结构 + 正确 `error` code：`INVALID_QUIET_HOUR` / `INVALID_ENABLED_CHANNELS` / `INVALID_HIGH_RISK_ONLY` / `INVALID_SEVERITY` / `INVALID_EVENT_TYPE`
- [ ] AC-4：`is_quiet_now(prefs, now)` 正确处理 4 种场景：在静默窗口内 / wrap-around 跨天 / `dnd_until` 未到 / 未设静默时段
- [ ] AC-5：`should_deliver(prefs, severity, event_type, now)` 合并三个判断：severity 不在白名单 / event_type 被静音 / 当前在静默窗口 → 返回 False
- [ ] AC-6：Alembic migration 可逆（downgrade 删除新增列）
- [ ] AC-7：新测试 ≥ 12 个（每 AC ≥ 2），全量测试零回归
- [ ] AC-8：i18n key `media.invalid_severity` + `media.invalid_event_type` 在 `MESSAGES_ZH` + `MESSAGES_EN` 中都存在

## 设计

### 1. ORM model 新增字段

```python
class PushPrefs(Base):
    __tablename__ = "push_prefs"

    # --- BE-PUSH-001 已存在 ---
    parent_id: Mapped[str]                # PK
    quiet_start: Mapped[int | None]       # 0-23
    quiet_end: Mapped[int | None]         # 0-23
    enabled_channels: Mapped[list[str]]   # JSON
    high_risk_only: Mapped[bool]
    updated_at: Mapped[datetime]

    # --- BE-PUSH-004 新增 ---
    dnd_enabled: Mapped[bool]             # server_default=False
    dnd_until: Mapped[datetime | None]    # 临时勿扰结束时刻
    severities: Mapped[list[str]]         # JSON, default ["warning", "critical"]
    muted_event_types: Mapped[list[str]]  # JSON, default []
```

### 2. 端点扩展

```
PATCH /api/v1/push/prefs
Body（全部可选）:
  quiet_start?, quiet_end?,
  enabled_channels?, high_risk_only?,
  dnd_enabled?, dnd_until?,
  severities?, muted_event_types?
→ 200 { ... 全部 8 字段 ... }

GET /api/v1/push/prefs
→ 200 { ... 全部 8 字段 ... }   # 默认值填齐
```

### 3. 验证

| 字段 | 规则 | 错误码 |
|------|------|--------|
| `quiet_start` / `quiet_end` | int 0-23 | `INVALID_QUIET_HOUR` |
| `quiet_start != quiet_end` | 同设时必须不同 | `INVALID_QUIET_HOUR` |
| `enabled_channels` | ⊆ VALID_CHANNELS | `INVALID_ENABLED_CHANNELS` |
| `high_risk_only` | bool | `INVALID_HIGH_RISK_ONLY` |
| `dnd_enabled` | bool | `INVALID_HIGH_RISK_ONLY`（复用 code；语义接近） |
| `dnd_until` | ISO8601 datetime | `INVALID_QUIET_HOUR`（复用 code） |
| `severities` | ⊆ {info, warning, critical} | `INVALID_SEVERITY` |
| `muted_event_types` | list[str]，每项非空字符串 | `INVALID_EVENT_TYPE` |

### 4. Helper 函数

```python
def is_quiet_now(
    prefs: dict,
    now: datetime | None = None,
) -> bool:
    """True iff 'now' falls in quiet window OR DND is active."""
    if now is None:
        now = datetime.now(timezone.utc)
    # DND overrides quiet hours
    if prefs.get("dnd_enabled"):
        dnd_until = prefs.get("dnd_until")
        if dnd_until is not None and dnd_until > now:
            return True
        if dnd_until is None:
            return True   # persistent DND
    qs = prefs.get("quiet_start")
    qe = prefs.get("quiet_end")
    if qs is None or qe is None or qs == qe:
        return False
    h = now.hour
    if qs < qe:
        return qs <= h < qe
    # wrap-around (e.g. 22 → 7)
    return h >= qs or h < qe


def should_deliver(
    prefs: dict,
    severity: str,
    event_type: str,
    now: datetime | None = None,
) -> bool:
    """False iff quiet hours, severity filter, or event-type mute blocks it."""
    if is_quiet_now(prefs, now):
        return False
    allowed = prefs.get("severities") or []
    if allowed and severity not in allowed:
        return False
    muted = prefs.get("muted_event_types") or []
    if event_type in muted:
        return False
    return True
```

### 5. Migration

```python
revision = "2026_07_31_0001"
down_revision = "2026_07_30_0002"

def upgrade():
    op.add_column("push_prefs", sa.Column("dnd_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("push_prefs", sa.Column("dnd_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("push_prefs", sa.Column("severities", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False, server_default='["warning","critical"]'))
    op.add_column("push_prefs", sa.Column("muted_event_types", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False, server_default="[]"))

def downgrade():
    op.drop_column("push_prefs", "muted_event_types")
    op.drop_column("push_prefs", "severities")
    op.drop_column("push_prefs", "dnd_until")
    op.drop_column("push_prefs", "dnd_enabled")
```

### 6. i18n 新增 key

- `media.invalid_severity`
- `media.invalid_event_type`

ZH: `severities 必须是 {info, warning, critical} 的子集` / `muted_event_types 必须是非空字符串列表`
EN: `severities must be a subset of {info, warning, critical}` / `muted_event_types must be a list of non-empty strings`

## 测试

新增 `tests/api/test_push_prefs_quiet.py`（≥ 12 个用例）：

- 默认 prefs 包含全部 8 字段
- PATCH `dnd_enabled` / `dnd_until` 往返
- PATCH `severities` / `muted_event_types` 往返
- 验证失败路径：`invalid_severity` / `invalid_event_type`
- `quiet_start == quiet_end` 拒绝
- `is_quiet_now`：静默窗口内 / 窗口外 / wrap-around / `dnd_until` 在未来 / 未设静默时段
- `should_deliver`：severity 不在白名单 / event_type 被静音 / 在静默窗口 → False

## 后续

- **BE-PUSH-005**：把 `should_deliver` 接入实际推送发送路径（BE-PUSH-002/003 vendor 适配）