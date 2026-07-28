# BE-COMP-001：数据可携权导出

| 字段 | 值 |
|------|-----|
| **优先级** | 🟡 P1 |
| **域** | COMPLIANCE |
| **来源** | `docs/frontend-integration.md` §7.1.8 |
| **工作量** | 1 天 |
| **状态** | 🚧 进行中（2026-07-28） |
| **依赖** | 无 |
| **后续** | BE-COMP-002 数据导入（GDPR import right） |

## 背景

§7.1.8 明确：

> BE-COMP-001 | 数据导出 | 缺失 | PRD #35 数据可携权 | 1 天

PRD #35: 家长可一键导出孩子在 Niki 上的所有数据（合规要求）。当前没有任何导出端点，合规有缺口。

本工单实现 `GET /api/v1/parents/{parent_id}/data-export` 返 ZIP 包含所有相关数据。

## 验收标准（AC）

- [ ] AC-1：`GET /api/v1/parents/{parent_id}/data-export` 返 200 + `application/zip` Content-Type
- [ ] AC-2：ZIP 包含至少 5 个 JSON 文件：`account.json` / `children.json` / `memory.json` / `alerts.json` / `tutoring.json` / `knowledge.json`
- [ ] AC-3：ZIP 包含 `README.txt` 说明文件（导出时间、parent_id、字段说明）
- [ ] AC-4：横向越权防护：path 的 `parent_id` 必须等于 token 的 `sub`（否则 403）
- [ ] AC-5：可选 `?child_id=...` 限定只导出单个 child 的数据
- [ ] AC-6：treehole 数据以**密文**导出（家长拿到 ciphertext bytes base64）
- [ ] AC-7：错误码 `PARENT_MISMATCH`（403） / `EXPORT_UNAVAILABLE`（503）
- [ ] AC-8：单测 + 集成测试 ≥ 6 个
- [ ] AC-9：现有 1083 passed 测试**零回归**

## 设计

### 1. 端点

```
GET /api/v1/parents/{parent_id}/data-export
Query: ?child_id=<uuid>      # optional, scope to one child
Auth: Bearer <access_token>

→ 200 application/zip
  Body: <ZIP bytes>
  
→ 403 { "error": "PARENT_MISMATCH", ... }   if path parent_id != token sub
→ 503 { "error": "EXPORT_UNAVAILABLE", ... } if some service unreachable
```

### 2. ZIP 内容

```
niki_data_export_{parent_id}_{YYYYMMDD}.zip
├── README.txt                  — export metadata, field glossary
├── account.json                — masked parent profile
├── children.json               — child list (no PII outside name)
├── memory.json                 — short/long-term memory rows
├── alerts.json                 — safety_valve + high_risk alerts
├── tutoring.json               — tutoring events
├── knowledge.json              — knowledge queries
├── treehole_engagements.json   — encrypted blobs (base64)
├── treehole_summaries.json     — aggregated baseline (if any)
├── emotion_baseline.json       — baseline records
├── media_uploads.json          — file_id list (NOT bytes; the local
│                                 file lives under ~/.niki/uploads)
├── weekly_reports.json         — generated weekly reports
└── manifest.json               — counts, hashes, expiry policy
```

每个 JSON 文件形如：
```json
{
  "exported_at": "2026-07-28T10:00:00Z",
  "record_count": 12,
  "items": [ ... actual rows ... ]
}
```

### 3. 安全约束

- 树洞 ciphertext **包含**（家长有知情权），但不存明文
- 横向越权：path parent_id 必须 == token sub
- media files 仅列 file_id（**不**打包 byte 内容）—— 它们是音频/图片，本应由后端单独提供导出端点（→ BE-COMP-001-ext-A）
- ZIP 流式写入（不在内存中构造整个 ZIP）

### 4. 风险

| 风险 | 缓解 |
|------|------|
| 大数据量 ZIP OOM | Streaming `ZipFile.write` |
| 跨实例数据不一致 | repo 已在 P1 落地，单请求聚合 |
| TreeHole 明文泄漏 | 仅导出密文（带 schema 说明） |
| 横向越权 | 双重校验：path param + token sub |
| media 文件导出 | 推迟到 follow-up；当前只列 file_id |

### 5. 不在本工单

- ❌ 媒体文件（音频/图片）byte 打包 —— → BE-COMP-001-ext-A
- ❌ 数据导入（GDPR import right）—— → BE-COMP-002
- ❌ PDF 报告 —— → BE-DASH-001-report-pdf

## 后续工单

- **BE-COMP-001-ext-A**：媒体文件 byte 流式打包
- **BE-COMP-002**：导入端点（PIPL import right）
- **BE-DASH-001**：PDF 周报导出
