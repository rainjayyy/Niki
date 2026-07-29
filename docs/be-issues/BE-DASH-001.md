# BE-DASH-001：PDF 周报

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | DASHBOARD |
| **来源** | 前端对接文档 §4.1（家长端"看周报"流程） |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-29） |
| **依赖** | `DashboardService.generate_weekly_report` ✅（已存在） |
| **后续** | 多模板 / 自定义 logo / 邮件订阅（→ BE-DASH-001-rollout） |

## 背景

家长 dashboard 现在只能拉 JSON 版的 `EmotionWeeklyReport`（`GET /api/v1/dashboard/weekly-report/{child_id}`）。前端拿到 JSON 后还要做：

- 拼装 HTML
- 调用浏览器 `window.print()`
- 让用户手动选"另存为 PDF"

后果：
- 家长端排版不一致（浏览器 / OS / 默认字体都不同）
- iOS 浏览器导出路径诡异（中国家长 60% 在 H5）
- 没有"邮件订阅周报"能力

本工单给后端加 PDF 端点，前端只需 `<a href="/api/v1/dashboard/weekly-report/{child_id}/pdf">下载</a>` 即可。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/dashboard/weekly-report/{child_id}/pdf` 返回 `application/pdf` 字节流
- [ ] AC-2：PDF 含周报 6 大区块：headline / trend / discoveries / fuzzy_negatives / anomalies / actions
- [ ] AC-3：PDF 含 child_id + week_start + week_end + generated_at 元数据
- [ ] AC-4：lateral-auth 检查（`get_owned_child_id`）通过才返回
- [ ] AC-5：中文标题 / 正文不出现 □ / 乱码（用 reportlab 内置 STSong-Light CJK 字体）
- [ ] AC-6：单页 PDF（A4 纵向），超过 1 页时自动分页
- [ ] AC-7：测试覆盖 ≥ 5 个，含：端点返回 PDF / 字节流 magic `%PDF-` / 含中文字符串 / 越权 403
- [ ] AC-8：现有 1226 passed 测试**零回归**

## 设计

### 1. 端点

```
GET /api/v1/dashboard/weekly-report/{child_id}/pdf
Authorization: Bearer <token>

→ 200 application/pdf
   Content-Disposition: attachment; filename="niki-weekly-c-{child_id}-{date}.pdf"
```

### 2. PDF 内容

```
┌─────────────────────────────────────────┐
│  Niki 周报 — 小明的情绪成长              │
│  2026-07-22 ~ 2026-07-29                 │
├─────────────────────────────────────────┤
│  本周亮点                               │
│  "这一周孩子聊了 18 次，整体心情稳定"  │
│                                         │
│  情绪趋势                               │
│  😊 稳定 → 上升 ↗                       │
│  正向 5 天 / 7 天 / 触底反弹 1 次         │
│                                         │
│  本周发现                               │
│  ✦ 探索了 3 个新领域：恐龙、太空、海洋   │
│  ✦ 主动问 Niki 8 个百科问题              │
│                                         │
│  小提示                                  │
│  • 聊完有 2 次明显情绪波动，建议关注     │
│  • 周末可一起读"情绪绘本"               │
│                                         │
│  元数据                                 │
│  生成时间：2026-07-29 14:30 UTC          │
│  数据窗口：2026-07-22 ~ 2026-07-29      │
│  涵盖：7 天                               │
└─────────────────────────────────────────┘
```

### 3. 技术选型

- **reportlab 5.0** —— Python 原生 PDF 生成，无系统依赖
- **CJK 字体** —— `STSong-Light`（reportlab 内置 CID 字体，无需额外字体文件）
- **API 风格** —— 复用 `DashboardService.generate_weekly_report()` 拉数据，PDF service 负责布局

### 4. 风险

| 风险 | 缓解 |
|------|------|
| CJK 字体没注册 → 中文乱码 | `WeeklyReportPDF.__init__` 注册 `STSong-Light`（幂等） |
| PDF 字节流巨大阻塞 event loop | PDF 生成是纯 CPU 操作（无 IO），同步即可；Async 包裹仅做转 IO |
| 模板硬编码 | 后续工单支持多模板（BE-DASH-001-rollout） |
| 横向越权 | 复用 `get_owned_child_id` dep |

### 5. 不在本工单

- ❌ 多模板（学龄前 / 学龄 / 青少年）
- ❌ 自定义 logo / 主题色
- ❌ 邮件订阅 / 定时推送
- ❌ 图表（折线 / 饼图）—— 后续工单