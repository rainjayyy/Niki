# Niki 项目熟悉报告（前端视角）

> **作者**：Claude（基于 [README.md](../README.md) · [PRD-v2.0.md](../PRD-v2.0.md) · [frontend-integration.md](./frontend-integration.md) v1.3 · 全部 `niki/api/*.py` 与 `niki/auth/*.py` 源码）
> **生成日期**：2026-07-29
> **目的**：前端工程师新窗口上手用 — 项目全貌 + 鉴权模型 + 端点契约 + UX 流程 + 缺口现状 + 上手清单

---

## 目录

1. [项目是什么](#1-项目是什么)
2. [后端代码结构](#2-后端代码结构与前端最相关的入口)
3. [鉴权模型](#3-鉴权模型前端必背)
4. [最常用 28 个端点](#4-前端最常用的-28-个端点)
5. [五大关键 UX 流程](#5-五大关键-ux-流程)
6. [返回模式与错误处理](#6-前端必须知道的-5-种返回模式)
7. [全局请求头](#7-前端必装的全局头)
8. [后端缺口现状](#8-后端缺口现状截至-2026-07-29)
9. [关键技术细节](#9-几个关键技术细节)
10. [前端上手清单](#10-前端工程师上手清单)

---

## 1. 项目是什么

**Niki** 是面向 **3-12 岁儿童** 的 AI 伙伴与成长助手（FastAPI + 多智能体编排），强调"情绪感知 + 认知适配 + 隐私安全"。

- 文档：[README.md](../README.md) · [PRD-v2.0.md](../PRD-v2.0.md) · [frontend-integration.md](./frontend-integration.md) v1.3
- 后端：**FastAPI + LangChain + DeepSeek-V4 / doubao-lite**，15 个垂直切片，**99 个 REST 端点**
- 前端：**还没有**（目标 Phase 1 = 家长 H5 + 儿童 H5，Vue 3 + Vite）
- 测试：775+ 全绿，CI 在 `.github/workflows/ci.yml`

---

## 2. 后端代码结构（与前端最相关的入口）

```
niki/
├── main.py                    # FastAPI 入口（lifespan 启动 Postgres/Redis/Chroma/Neo4j + 30s 心跳）
├── api/
│   ├── router.py              # 聚合所有子路由，统一前缀 /api/v1
│   ├── auth.py                # 鉴权全链路（SMS / JWT / refresh / logout）
│   ├── chat_stream.py         # BE-RT-001 SSE 流式聊天
│   ├── orchestrator.py        # POST /chat/full 7 阶段管道
│   ├── dashboard.py           # 家长看板 4 大件（横向鉴权）
│   ├── session.py             # 儿童端 60min 上限 + 5min 警告 + 远程暂停
│   ├── treehole.py            # 树洞模式（前端不要直调 /treehole/engage）
│   ├── emotion.py             # 情绪 agent + 8pm 主动关怀
│   ├── content_policy.py      # 家庭黑名单 + 年龄分级
│   ├── media.py               # 通用上传（multipart, ≤10MB, magic-byte 校验）
│   ├── audio.py               # ASR (mock) + TTS (mock)
│   ├── tools.py               # 工具权限矩阵展示
│   ├── children.py            # 注册/列出儿童（横向鉴权 403 PARENT_MISMATCH）
│   ├── data_export.py         # GET /parents/{id}/data-export → ZIP
│   ├── safety.py              # #1 输入/输出网关
│   ├── routing.py             # #2 分类器 + 路由 + 人格
│   ├── encyclopedia.py        # #3 KB + fact-check
│   ├── tutoring.py            # #4 引导式辅导
│   ├── memory.py              # #9 短/长/图记忆 + pipeline
│   ├── resilience.py          # #10 熔断器 + 降级
│   ├── auth_audit.py          # 横向越权审计查询
│   └── health.py              # /health /health/live /health/ready
├── auth/                      # JWT / SMS / Token 黑名单 / get_current_parent 依赖
├── schemas/common.py          # HealthResponse / APIResponse[T] / PaginatedResponse[T] / ChildInput / AgentOutput
├── dashboard/report.py        # EmotionWeeklyReport.to_dict() 字段就是前端要渲染的周报结构
└── ...
```

---

## 3. 鉴权模型（前端必背）

| 项 | 实现 |
|---|---|
| 算法 | **HS256 JWT**，secret 来自 `settings.jwt_secret` |
| Token 类型 | `access`（默认 1 小时）+ `refresh`（默认 30 天），`typ` claim 强制区分 |
| Header | `Authorization: Bearer <access_token>` |
| 客户端头 | `X-Client-Type`（web/weapp/ios/android/flutter）+ `X-Client-Version` + `X-Device-Id`（BE-DEV-001 已落地，可读取但当前不分流） |
| 黑名单 | Redis 存储被 `logout` 撤销的 `jti`；`refresh` 端点做**旋转**（用过的 refresh 立刻吊销） |
| 家长上下文 | `Depends(get_current_parent)` → `ParentAccount(id, name, ...)` |
| 儿童归属校验 | `Depends(get_owned_child_id)` → 校验 child_id 归属于当前 parent，失败 403/404 并写审计日志 |

**关键端点契约**（`niki/api/auth.py`）：

```text
POST /api/v1/auth/sms/send           { phone, purpose? }   → { phone, status, dev_code? } (3/min)
POST /api/v1/auth/sms/login          { phone, code }       → { access_token, refresh_token, token_type, expires_in, parent_id, account_status } (10/min)
POST /api/v1/auth/refresh            { refresh_token }     → { access_token, refresh_token, ... } (60/min) [旧 refresh 立即吊销]
POST /api/v1/auth/logout                                   → { status: "logged_out", parent_id, revoked_jti }
POST /api/v1/auth/signup/start       { name, id_number, phone, email? }  → { account, progress }
POST /api/v1/auth/signup/real-name   { parent_id, name, id_number }     → { account }
POST /api/v1/auth/signup/face        { parent_id, live_image_url, reference_image_url? }
POST /api/v1/auth/signup/consent     { parent_id, typed_signature, ip_address?, device_fingerprint?, sign_child_consent? }
GET  /api/v1/auth/consent/document/{parent|child}        → { type, version, text }
GET  /api/v1/auth/me/{parent_id}
POST /api/v1/auth/deletion/request   { parent_id }        → { account, deletion_window_remaining_days }
POST /api/v1/auth/deletion/complete  { parent_id }
GET  /api/v1/auth/whoami                                   → { parent_id, name, status } (JWT demo)
POST /api/v1/children/register/{parent_id}   { child_id, name? }  → { child_id, parent_id, name, status }
GET  /api/v1/children/list/{parent_id}                       → { parent_id, count, child_ids }
```

> 📌 **家长首次登录即开户**：`/auth/sms/login` 拿到未注册手机号会自动 `start_signup` 建一个 `PENDING` 账号，并发实名信息 = `家长<后四位>` + 占位身份证号。Phase 1 前端可以先按 `account_status === "pending"` 触发完整注册流程。

---

## 4. 前端最常用的 28 个端点

| 端点 | 用途 |
|---|---|
| `POST /chat/full` | **儿童端主对话入口**（编排器 7 阶段：安全输入→分类→路由→分发→融合→安全输出） |
| `POST /chat/stream` | **SSE 流式聊天**（mock provider，前端用 `EventSource`/`fetch` 读 `data: {chunk:..., token_index:...}` → `data: {is_final: true, total_text, session_id}` → `data: [DONE]`，30/min） |
| `GET /health` / `/health/live` / `/health/ready` | 健康检查（含 redis/postgres/neo4j/chroma/llm + 限流后端） |
| `GET /dashboard/weekly-report/{child_id}` | **情绪周报** — `EmotionWeeklyReport.to_dict()` 字段：headline / discoveries / fuzzy_negatives / anomaly_annotations / action_suggestions / trend / week_start / week_end |
| `GET /dashboard/interest/{child_id}` | 兴趣图谱 |
| `GET /dashboard/learning/{child_id}` | 学习白皮书 |
| `GET /dashboard/alerts/{child_id}` | 告警列表（**已展平**：high_risk[] + safety_valve[]） |
| `POST /dashboard/alerts/{alert_id}/delivered` | 标记告警已读 |
| `POST /dashboard/baseline/add` · `/events/tutoring` · `/events/knowledge` | 内部记录（测试用，前端一般不用） |
| `POST /session/start` · `/activity` · `/end` | 儿童端会话生命周期 |
| `GET /session/stats/{child_id}` · `/should-warn/{child_id}` · `/is-exhausted/{child_id}` · `/is-idle/{child_id}` | 软警告（5min）+ 硬上限（60min）+ 闲置（10min） |
| `POST /session/parent/pause` · `/consume-pause` · `/end-session` · `/clear-pause` + `GET /session/parent/has-pause/{cid}` · `/paused/{cid}` | 家长一键暂停 / 撤销 |
| `POST /policy/blacklist/{family_id}/add\|remove` + `GET /policy/blacklist/{family_id}` + `POST /policy/blacklist/{family_id}/evaluate` | 家庭话题黑名单 |
| `GET /policy/age/{child_id}?child_age=...` · `POST /policy/age/grade\|grade-text` · `POST /policy/downgrade/evaluate` | 年龄分级 |
| `GET /tools/matrix` · `/tools/allowed/{agent}` · `/tools/audit` | 家长展示用 |
| `POST /media/upload`（multipart, ≤10MB）→ `{ file_id, mime_type, size_bytes, sha1, access_url, uploaded_at, category }`；`GET /media/{file_id:path}` 下载 | 通用文件上传 |
| `POST /audio/transcribe`（multipart, ≤5MB）→ `{ text, duration_ms, confidence, language, audio_hash }`；`POST /audio/synthesize` → `{ audio_base64, mime_type, duration_ms, ... }` | ASR/TTS（**当前 mock provider**，前端可以先把链路跑通） |
| `GET /treehole/summaries/{child_id}` | 树洞脱敏摘要列表（⚠️ 当前实现不过滤 child_id，上生产前必须修） |
| `GET /parents/{parent_id}/data-export?child_id=...` | 数据导出 ZIP（PIPL Art. 44） |

> ⚠️ **永远不要直调** `POST /treehole/engage` — 由 `/chat/full` 编排器内部触发。

---

## 5. 五大关键 UX 流程

1. **家长注册**（3 步：real-name → face → consent）— `signup/start` 拿 progress，UI 可中途退出再回来继续
2. **家长看周报** — 积极优先 headline + 4 个章节（发现/模糊负向/异常标注/行动建议），7 天折线图
3. **远程暂停** — 家长 POST `/session/parent/pause` → 儿童端下次回合时 `consume_pause` 拿到自然告别文案
4. **儿童对话** — 进入 → `/session/start`；每回合 `/session/activity`；剩余 5min → 软警告页；60min → 自然结束页
5. **树洞模式** — 输入触发 `should_activate_treehole` 后切深色 UI，**不能展示 Niki 头像 / 不能感谢家长**，原文 AES-256-GCM 加密，7 天后自动清除

---

## 6. 前端必须知道的 5 种返回模式

| # | 模式 | 例子 | 格式 |
|---|---|---|---|
| ① | **`APIResponse` wrapper** | `/health` 异常路径 | `{"success": true, "data": ..., "error": null, "request_id": "..."}` — 错误时 `error: {code, message, detail}` |
| ② | **AuthError 结构化** | `POST /auth/*` | `{"detail": {"error": "INVALID_PHONE_FORMAT", "message": "..."}}` |
| ③ | **原始 dict** | `POST /chat/full`, `/dashboard/*` | 直接字段 |
| ④ | **dataclass `.to_dict()`** | `GET /dashboard/weekly-report/{child_id}` | 周报字段直接展开 |
| ⑤ | **Pydantic Model** | `GET /health` 成功 | `HealthResponse` model |

**axios 拦截器建议**：按路径分流解析；HTTP 状态码处理 401/403/404；业务错误码从 `response.data.detail.error` 取；其他从 `response.data.error` 字符串匹配。

---

## 7. 前端必装的全局头

```
X-Client-Type: web | weapp | ios | android | flutter
X-Client-Version: 1.0.0
X-Device-Id: <UUID>
Authorization: Bearer <access_token>     // 登录后所有受保护端点
```

⚠️ **当前 BE-DEV-001 已读 `X-Client-Type` 写入 `request.state.client_type`**，但**还没真正按类型分流推送/资源限制**，前端可以照发但别假设行为差异。

---

## 8. 后端缺口现状（截至 2026-07-29）

| 级别 | 完成 | 剩余重点 |
|---|---|---|
| **P0 前端动工前必修** | **13⚠️ / 16** | ❌ `BE-PUSH-001` 推送注册 · ❌ `BE-API-003` 列表分页 · ⚠️ `BE-COMP-002` 时区（部分完成） |
| **P1 MVP 上线前必补** | **1 / 17** | 推送厂商通道、家长↔儿童配对、OSS、短信/2FA 等等 |
| **P2 体验优化** | **0 / 13** | Web Push、通知中心、i18n、Prometheus 等 |

**前端可放心依赖的能力**（P0 已落地）：
✅ JWT 中间件 / SMS 登录 / refresh 旋转 / logout · X-Client-Type 路由 · 文件上传（multipart） · ASR mock · TTS mock · SSE 流式聊天 · Postgres ORM · 心跳 · 统一响应 `APIResponse[T]` · slowapi 限流 · 数据导出 ZIP · 横向越权审计日志。

**前端要做的兜底**（后端暂缺的）：
- 推送 → 30s 短轮询 `GET /dashboard/alerts/{child_id}`
- 配对 → 1.0 直接传 `parent_id`（临时）
- 分页 → 前端 slice + virtual scroll（直到 `BE-API-003` 上线）

---

## 9. 几个关键技术细节

- **JWT 黑名单**：登出时把 `jti` 写 Redis（TTL = 剩余有效期），`verify_token` 在签名/exp 检查后再查黑名单（`niki/auth/jwt_service.py` + `niki/auth/token_blacklist.py`）
- **横向鉴权**：`get_owned_child_id` 校验 child 归属于 token parent；`children.py` 同样校验 `parent.id == path parent_id`；不匹配都写 `AuthAuditLogger` 并返回 `PARENT_MISMATCH` / `PARENT_CHILD_MISMATCH`
- **限流**：slowapi 已挂到 8 个端点（`/auth/sms/*`、`/auth/refresh`、`/chat/stream`、`/media/*`、`/audio/*`），Redis 后端（`BE-API-002-redis`）多 worker 共享计数；`/health` 会暴露当前后端 `rate_limit_backend` + `rate_limit_in_fallback`
- **测试模式开关**：`NIKI_TEST_FAKE_AUTH=1` 会注入 `get_current_parent` fake 实现（返回 `test_parent`）并跳过 `get_owned_child_id` 的归属校验，**生产绝不能开**
- **mock 与真 provider**：`ASR` / `TTS` / `LLM streaming` 全部是 mock，Phase 1 足够联通；后续替换 provider 时**前端 API 契约不变**
- **错误码命名建议**：`{DOMAIN}_{CATEGORY}_{SPECIFIC}` 三段式 UPPER_SNAKE_CASE，前端按域分组导出常量

---

## 10. 前端工程师上手清单（推荐路径）

1. 阅读 [frontend-integration.md](./frontend-integration.md) 全文（最权威）
2. 启动后端：`make docker-up` → `make dev` → 浏览器开 [http://localhost:8000/docs](http://localhost:8000/docs) 看交互式文档，下载 [openapi.json](http://localhost:8000/openapi.json)
3. 在 `scripts/` 跑 `gen_openapi_table.py --write` 重新生成 [api-endpoints.md](./api-endpoints.md)
4. 用 `NIKI_TEST_FAKE_AUTH=1 make dev` 跳过鉴权，先把家长端首页/儿童端对话跑通（拿到业务联调数据）
5. 真实联调时用 `POST /auth/sms/send`（debug 模式返回 `dev_code`）→ `/auth/sms/login` 拿 token → 后续业务请求带 Bearer
6. **SSE 流式**前端用 `fetch` + `ReadableStream` 解码（不能用 axios 拦截器接管 response body），或用原生 `EventSource` polyfill
7. 跨域：CORS 在 `niki/middleware/cors.py` 里配，本地开发默认开
8. 错误处理：建 `src/constants/errorCodes.ts` + `src/api/http.ts`（axios 拦截器按路径分流 + token 自动刷新 + 401 跳登录）

---

**下一步行动**：
- 与后端对齐 §8 缺失清单，排出 P0 修复时间表
- 与产品/UX 评审页面地图（[frontend-integration.md §4](./frontend-integration.md)）
- 与前端主程评估技术栈（[frontend-integration.md §3](./frontend-integration.md)）
- 启动 Phase 1 家长端 H5 开发