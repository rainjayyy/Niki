# BE-DEV-002: 设备管理端点

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | DEVICE |
| **来源** | 前端对接文档 §7.1.2 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-PUSH-001 ✅（注册端点已上） |
| **后续** | BE-DEV-003 多端冲突处理 |

## 背景

家长端要支持多设备（手机 + 平板）。BE-PUSH-001 已有 push/register，但**没有设备总览**：
- 哪些设备绑定了这个家长？
- 最后活跃时间？
- 哪些设备已禁用？

前端 dashboard 需要 `GET /api/v1/devices` 拉设备列表。

本工单给 push_devices 表加**家长维度查询端点**。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/devices` 列出当前 parent 的所有设备
- [ ] AC-2：`DELETE /api/v1/devices/{device_id}` 注销设备（lateral-auth 检查）
- [ ] AC-3：响应不含 push_token（防泄漏）
- [ ] AC-4：测试覆盖 ≥ 4 个
- [ ] AC-5：现有 1341 passed 测试**零回归**

## 设计

复用 `niki/push_device_repo.py`（BE-PUSH-001），加 `list_for_parent` 方法。

```python
@router.get("/devices")
async def list_my_devices(
    parent: ParentAccount = Depends(get_current_parent),
):
    devices = await get_push_device_repository().list_for_parent(parent.id)
    return {"count": len(devices), "devices": [d for d in devices]}

@router.delete("/devices/{device_id}")
async def unregister_device(
    device_id: str,
    parent: ParentAccount = Depends(get_current_parent),
):
    removed = await get_push_device_repository().delete_by_parent_device(
        parent.id, device_id
    )
    return {"unregistered": True, "removed": removed}
```
