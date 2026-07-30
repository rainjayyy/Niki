# BE-MISC-001：服务端配置下发

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | MISC / CONFIG |
| **来源** | 前端对接文档 §7.3.8 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | 无 |
| **后续** | BE-MISC-002（feature flags） |

## 背景

前端硬编码 60min 时长 / 7 天冷静期 / 默认通道列表 / API 域名。导致：

- 运营调参（如 30min 限制）需要前端发版
- 多套部署（dev / staging / prod）需要不同的默认
- A/B 测试不同配置必须后端控制

本工单加 `GET /api/v1/config/client?client_type=&version=` 端点。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/config/client` 返回 JSON 配置
- [ ] AC-2：含 `daily_limit_minutes` / `cooldown_days` / `enabled_channels` / `api_version`
- [ ] AC-3：未来按 `client_type` / `version` 过滤（v1.0 默认全部相同）
- [ ] AC-4：测试覆盖 ≥ 3 个
- [ ] AC-5：现有 1362 passed 测试**零回归**

## 设计

```python
# niki/api/config.py
DEFAULT_CONFIG = {
    "daily_limit_minutes": 60,
    "cooldown_days": 7,
    "enabled_channels": ["hms", "mipush", "oppo", "vivo", "flyme", "apns", "fcm", "web"],
    "api_version": "v1.0.0",
    "min_supported_version": "1.0.0",
    "feature_flags": {"new_dashboard": True, "ocr_enabled": False},
}

@router.get("/config/client")
async def get_client_config(
    client_type: str | None = None,
    version: str | None = None,
):
    return DEFAULT_CONFIG
```

## 后续

- **BE-MISC-002**：feature flag 真正接入（unleash / 自写）
- **BE-MISC-003**：公共 status 页面
