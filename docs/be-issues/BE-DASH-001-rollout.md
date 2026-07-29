# BE-DASH-001-rollout：多模板 + 自定义 logo

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | DASHBOARD |
| **来源** | 前端对接文档 §4.1（家长端周报） |
| **工作量** | 0.5 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | BE-DASH-001 ✅（基础 PDF 已上） |
| **后续** | 邮件订阅 / 多语言 → BE-DASH-001-rollout-rollout |

## 背景

BE-DASH-001 给家长端发了能下载的 PDF 周报。当前**所有家庭拿到的 PDF 长一样**——同一标题、同一配色、同一布局。

实际问题：
- **学龄前 vs 学龄** 同一份 PDF，措辞不匹配（学龄前重"今天玩得开心吗"，学龄重"今天的情绪稳定性"）
- **机构白标** 用了 Niki 的 logo，机构方（合作幼儿园）想要自己 logo
- **暖色 / 冷色** 不同家庭对颜色偏好不同，**单一**配色无法满足

本工单支持：
1. **多种模板**：`child`（默认）/ `preschool` / `school` —— 切换措辞和重点
2. **自定义 logo**：base64 image + 名称 → PDF header 显示

## 验收标准（AC）

- [ ] AC-1：PDF 端点支持 `?template=preschool` / `?template=school` / `template=child`（默认）
- [ ] AC-2：Settings `dashboard_pdf_logo_b64` / `dashboard_pdf_brand_name` 透传到 PDF header
- [ ] AC-3：`preschool` 模板语言更活泼（emoji + 短句）
- [ ] AC-4：`school` 模板语言更稳重（数据 + 趋势）
- [ ] AC-5：测试覆盖 ≥ 6 个，含：3 模板渲染 / logo 注入 / brand 名称 / 默认行为
- [ ] AC-6：现有 1282 passed 测试**零回归**

## 设计

### 1. ReportTemplate 枚举

```python
class ReportTemplate(StrEnum):
    CHILD = "child"           # 默认 — 适中
    PRESCHOOL = "preschool"   # 3-6岁 — emoji + 短句
    SCHOOL = "school"         # 7-12岁 — 数据 + 趋势
```

### 2. 模板差异

| 字段 | CHILD | PRESCHOOL | SCHOOL |
|------|-------|-----------|--------|
| 标题 emoji | 1-2 个 | 4+ 个 | 0 |
| 句长 | 适中 | < 15 字 | 完整句子 |
| 趋势表 | ✓ | ✓ 简化 | ✓ + Z-score |
| 行动建议 | 2 条 | 1 条 | 3 条 |
| 发现数 | 3-5 | 2-3 | 5-8 |

### 3. Logo / Brand

```python
class ReportBranding:
    logo_b64: str | None = None      # data:image/png;base64,...
    brand_name: str | None = None     # "新东方幼儿园"
    brand_color: str = "#5B6BFF"      # 默认 Niki indigo
```

在 PDF header 渲染：
- 左上角 logo（缩放到 24mm）
- 右上角 brand_name
- 副标题用 brand_color

### 4. API

```
GET /api/v1/dashboard/weekly-report/{child_id}/pdf
  ?template=preschool    # 可选，默认 child
  ?brand=school-id-123   # 可选，覆盖 brand_name
```

### 5. 风险

| 风险 | 缓解 |
|------|------|
| Logo 太大撑爆 PDF | 限 base64 ≤ 100KB；reportlab 自动缩放 |
| Logo 是损坏图片 → PDF 渲染失败 | try/except 包住，无 logo 时降级到纯文字 header |
| Brand 名称超长 | 限 30 字符 + 截断省略号 |
| 模板枚举扩展 | StrEnum，新增模板只加一行 |

### 6. 不在本工单

- ❌ 多语言（i18n → BE-I18N-001）
- ❌ 邮件订阅（→ BE-DASH-001-rollout-rollout）
- ❌ 图表（折线 / 饼图）