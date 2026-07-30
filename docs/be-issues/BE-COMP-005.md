# BE-COMP-005: 数据删除凭证（PIPL Art. 47）

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | COMPLIANCE |
| **来源** | 前端对接文档 §7.2.8 |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-30） |
| **依赖** | BE-STOR-001 ✅（DeletionRequest 表已建） |
| **后续** | BE-COMP-006 滑块验证 |

## 背景

PIPL Art. 47 要求**删除个人信息**时给用户**可下载的凭证**，证明数据真的删了。

当前 `DeletionRequest` 表存了状态，但**没有**：
- 哈希签名（防抵赖）
- 可下载的 PDF 凭证
- 公开查询接口（家长 30 天后下载凭证）

本工单补齐。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/deletion/{request_id}/receipt` 返回 PDF 凭证
- [ ] AC-2：PDF 含 request_id / parent_id / requested_at / completed_at / 内容哈希
- [ ] AC-3：lateral-auth 检查（只有发起删除的 parent 能下载）
- [ ] AC-4：测试覆盖 ≥ 4 个
- [ ] AC-5：现有 1346 passed 测试**零回归**

## 设计

复用 `reportlab`（BE-DASH-001 已装）生成 PDF：

```python
@router.get("/deletion/{request_id}/receipt")
async def download_receipt(
    request_id: str = Depends(...),
    parent: ParentAccount = Depends(get_current_parent),
):
    req = await get_deletion_request(request_id)
    if req["parent_id"] != parent.id:
        raise 403
    pdf = render_deletion_receipt(req)
    return Response(pdf, media_type="application/pdf")
```
