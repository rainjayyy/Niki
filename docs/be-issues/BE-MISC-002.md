# BE-MISC-002：Feature Flags（功能开关）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MISC / CONFIG |
| **来源** | 前端对接文档 §7.3.8；BE-MISC-001 后续 |
| **工作量** | 0.5 天 |
| **状态** | ✅ 已完成（2026-07-30） |
| **依赖** | BE-MISC-001 |
| **后续** | BE-MISC-002-A（Per-Parent Overrides / A/B） |

## 背景

BE-MISC-001 把前端硬编码的 60min 时长 / 7 天冷静期下发了，但没有解决"运营想临时关掉某个实验性功能"的需求：

- 前端要做 A/B（10% 用户先用新首页）必须后端能按 parent_id 定向
- 故障应急时（告警链路爆炸、Webhook 频次失控）需要秒级关闭告警
- 多套部署（dev / staging / prod）需要不同的默认 flag

工单目标：**全局 flag + per-parent override 的 seam 在 v1 就位**，让后续 A/B 工单（BE-MISC-002-A）能只写 override 表，不动业务代码。

## 验收标准（AC）

- [x] AC-1：`Settings.feature_flags_default: str = '{}'` —— 默认空 JSON 对象
- [x] AC-2：`GET /api/v1/config/client` 响应含 `feature_flags: dict[str, bool]`
- [x] AC-3：`niki/feature_flags.py` 提供 `FeatureFlagService.is_enabled(parent_id, flag)`
- [x] AC-4：`is_enabled` 接受 `parent_id`（v1 全局，等价；签名已留 seam）
- [x] AC-5：`set_override(flag, value)` 支持运行时翻转（admin / 测试用）
- [x] AC-6：`silence_alerts` flag 可短路 `_maybe_alert` 派发（速率追踪不受影响）
- [x] AC-7：现有 1381 passed 测试**零回归**
- [x] AC-8：新测试覆盖 ≥ 13 条（单元 + endpoint + 集成）

## 设计

### 1. 设置（`niki/config/settings.py`）

```python
# BE-MISC-002: feature flags
# JSON-encoded dict of global flag defaults. Empty {} is safe.
# Example: {"new_dashboard": true, "silence_alerts": false}.
# Per-parent overrides (A/B targeting) NOT configured here —
# those arrive via a future ticket.
feature_flags_default: str = "{}"
```

`.env` 中由运营改 `FEATURE_FLAGS_DEFAULT` 即可热加载。

### 2. FeatureFlagService（`niki/feature_flags.py`）

```python
class FeatureFlagService:
    def __init__(
        self,
        defaults: Mapping[str, bool] | None = None,
        *,
        defaults_json: str | None = None,
    ) -> None: ...

    def is_enabled(self, parent_id: str | None, flag: str) -> bool: ...
    def all_flags(self) -> dict[str, bool]: ...
    def set_override(self, flag: str, value: bool) -> None: ...
    def reset(self, defaults: Mapping[str, bool] | None = None) -> None: ...
```

要点：

- **`parent_id` 在 v1 已被接受但不使用**——明确的设计 seam，让所有调用点统一签名，per-parent override 表上线后调用点零改动。
- **线程安全**：内部 `RLock` 保护 `_flags`，可读可写。
- **类型收敛**：JSON 中非 bool 值（`0`/`1`）会被强转为 `bool`——前端拿到的永远是 `true` / `false`。
- **未知 flag → False**："关"是"这个实验性功能开了吗？"的安全答案。
- **空默认 → 空响应**：`{}` 不会 500；返回 `feature_flags: {}`。

### 3. Config 端点（`niki/api/config.py`）

```python
@router.get("/config/client")
async def get_client_config(
    client_type: str | None = Query(None),
    version: str | None = Query(None),
) -> dict:
    payload = dict(DEFAULT_CLIENT_CONFIG)
    payload["feature_flags"] = _resolve_feature_flags()
    payload["_query"] = {"client_type": client_type, "version": version}
    return payload
```

`_resolve_feature_flags()` 从 `get_feature_flag_service().all_flags()` 拉快照。`settings.feature_flags_default` 解析失败时返回 **503 FEATURE_FLAGS_UNAVAILABLE**（避免客户端因静态配置 500 导致启动失败）。

### 4. silence_alerts 短路（`niki/services/auth_audit.py`）

```python
if get_feature_flag_service().is_enabled(
    event.actor_parent_id, "silence_alerts",
):
    logger.info("auth.audit.alert_silenced_by_flag", ...)
    return
```

故意放在 `dispatcher.dispatch()` 之前**、`rate_tracker.record()` 之后**：

- 速率追踪仍然累计 → 翻转 flag 后立即生效（不需"预热"）
- 仅 webhooks / email / SSE broker 被抑制
- 审计 DB 写入不受影响

### 5. Per-Parent Override（v1 不实现，仅留 seam）

```sql
-- BE-MISC-002-A will introduce:
CREATE TABLE feature_flag_overrides (
    parent_id  TEXT NOT NULL,
    flag       TEXT NOT NULL,
    value      BOOL NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (parent_id, flag)
);
```

`FeatureFlagService.is_enabled` 调用点的 `parent_id` 参数就是为这个表准备的——届时在 `_lock` 内查一下 override 表，覆盖 `_flags.get(flag, False)`。

## 测试矩阵

| 测试 | 验证点 |
|------|--------|
| `test_default_construction_is_empty` | 无参数构造 → 空 dict |
| `test_defaults_dict_loaded` / `test_defaults_json_parsed` | 两种入参路径 |
| `test_defaults_and_defaults_json_are_mutually_exclusive` | 互斥校验 |
| `test_invalid_json_raises` / `test_non_object_json_raises` | JSON 校验 |
| `test_unknown_flag_defaults_to_false` | 安全默认（关） |
| `test_set_override_flips_runtime_value` | 运行时翻转 |
| `test_reset_returns_to_initial_defaults` | 测试用 reset |
| `test_parent_id_arg_accepted_but_unused_for_v1` | seam 已留 |
| `test_non_bool_values_are_coerced_to_bool` | int → bool 收敛 |
| `test_get_returns_a_service` / `test_set_replaces_singleton` | singleton 生命周期 |
| `test_returns_sorted_dict_of_bools` | 响应序列化稳定 |
| `test_config_returns_feature_flags_field` | endpoint 集成 |
| `test_config_feature_flags_match_seeded_service` | 配置回写一致 |
| `test_override_takes_effect_after_set` | 实时翻转生效 |
| `test_empty_service_yields_empty_flags` | 空默认不报错 |
| `tests/api/test_config_client.py::test_includes_feature_flags` | BE-MISC-001 老契约保留 |

## 后续

- **BE-MISC-002-A**：per-parent overrides + 管理员 API（`POST /admin/feature_flags/overrides`）
- **BE-MISC-002-B**：audit log 集成——`set_override` 调用记到 `auth_audit`，需要 admin 角色
- **FE-FLAG-001**：把 `feature_flags_default` 移出 `.env`，从 DB `feature_flag_config` 行读取（多环境隔离）

## 变更清单

| 文件 | 行为 |
|------|------|
| `niki/config/settings.py` | 新增 `feature_flags_default: str = '{}'` |
| `niki/feature_flags.py` | **新建**：`FeatureFlagService` + singleton |
| `niki/api/config.py` | `feature_flags` 改为从 service 拉；JSON 解析失败返 503 |
| `niki/services/auth_audit.py` | `_maybe_alert` 检查 `silence_alerts` flag |
| `tests/api/test_feature_flags.py` | **新建**：13 个用例 |
| `tests/api/test_config_client.py` | 加 fixture 注入 flag service；加强 `feature_flags` 断言 |
