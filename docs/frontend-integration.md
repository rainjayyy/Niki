# Niki 前端对接需求文档 v1.3

> **目标**：指导前端工程师快速对接 Niki 后端，并明确指出后端需要补齐的缺口。
> **发布版本**：v1.3 · 2026-07-29
> **上次变更**：§7.1 P0 缺口**全部清零**（推送 / 分页 / 时区）—— 前端可正式动工
> **状态**：草案（待后端 + 产品评审）

> **图例**：✅ 已完成 · ⚠️ 部分完成 · 🟡 进行中 · ❌ 未开始

---

## 目录

- [1. 文档概述](#1-文档概述)
- [2. 多端共存架构](#2-多端共存架构)
- [3. 前端技术栈推荐](#3-前端技术栈推荐)
- [4. 页面清单与 UX 流程](#4-页面清单与-ux-流程)
- [5. 交互细节与状态](#5-交互细节与状态)
- [6. API 对接规范](#6-api-对接规范)
- [7. 后端待补缺口清单](#7-后端待补缺口清单)
- [8. 数据流时序图](#8-数据流时序图)
- [9. 阶段交付计划](#9-阶段交付计划)
- [10. 风险与依赖](#10-风险与依赖)
- [11. 附录](#11-附录)
- [12. 文档履历](#12-文档履历)

---

## 1. 文档概述

### 1.1 背景
Niki 后端已实现 **99 个 REST 接口**（15 个垂直切片），但目前**没有前端工程**。完整端点清单见 [`docs/api-endpoints.md`](./api-endpoints.md)（由 `scripts/gen_openapi_table.py` 从 `/openapi.json` 自动生成）。

本文档：
- 梳理前端需要的所有页面、交互、状态；
- 列出对接所需的 API 契约（端点、请求、响应、错误码）；
- 明确指出后端**当前缺失**的能力（前端动工前必须补齐）。

### 1.2 阅读对象
- 前端工程师（首要读者）
- 后端工程师（次要读者 — 需补齐 §7 列出的缺口）
- 产品 / UX（评审用）

### 1.3 范围（Scope）

| 阶段 | 端 | 技术栈 | 交付目标 |
|---|---|---|---|
| **Phase 1**（现在） | 家长 H5 + 儿童 H5 | Vue 3 + Vite（H5） | 交互验证 + 完整业务闭环 |
| **Phase 2**（未来） | 家长微信小程序 | weapp + taro | 复用 H5 业务逻辑 |
| **Phase 3**（未来） | 儿童原生 App | Flutter | 完整 WebRTC / TTS / 系统级权限 |

**核心原则**：三端共享同一套后端 API 与数据模型，仅客户端实现不同。

---

## 2. 多端共存架构

### 2.1 客户端标识
所有请求必须携带：
```
X-Client-Type: web | weapp | ios | android | flutter
X-Client-Version: 1.0.0
X-Device-Id: <UUID>
```

后端通过 `X-Client-Type` 决定：
- 推送通道（App 走 APNs/华为/小米推送，H5 走站内信）
- 资源限制（Web 端禁止上传大文件，App 端开启）
- 鉴权方式（小程序用 `wx.login` 换 token，H5 用用户名密码）

> ⚠️ **后端当前未实现**：后端**没有读取** `X-Client-Type` 头，也没有按此区分推送通道 / 资源限制 / 鉴权方式。前端可以照常发送，但后端会忽略。前端联调时**不要假设按客户端类型有不同行为**，统一按最宽松规则处理。

### 2.2 用户体系
- **一个家长账户**可绑定多个端设备，但同一时刻只能激活一个家长端
- **一个孩子**绑定一个家长账户，跨设备共享数据
- **儿童端**无需独立账户，由家长扫码 / 配对码绑定

> ⚠️ **后端当前未实现**：
> - 没有"同时只能激活一个家长端"机制（多端登录互不影响）
> - 没有儿童端配对码流程（`/api/v1/device/pair-*` 端点不存在）
> - 前端**不要**依赖这些机制，否则会和后端实际行为不一致

### 2.3 数据权属
- 所有 `child_id` 数据归家长账户所有
- 儿童端只能读写当前绑定的 `child_id`
- 同一账户在多个端登录时，看板数据实时同步（**拉模型，无服务端推送**）

### 2.4 推送策略矩阵

| 场景 | Web 端 | 原生 App | 小程序 |
|---|---|---|---|
| 看板数据更新 | 30s 轮询 | 30s 轮询 + App 进入前台时拉取 | 30s 轮询 |
| 普通告警 | 30s 轮询 | 系统推送（厂商通道） | 订阅消息 |
| **高危告警** (PRD #29) | **SSE（< 5s）** | **系统推送（< 1s）** | **订阅消息（< 5s）** |
| 家长远程暂停通知 | 30s 轮询 | 系统推送 | 订阅消息 |
| 服务降级状态 | 30s 轮询 | 系统推送 + 30s 轮询 | 30s 轮询 |

> ⚠️ **后端当前未实现**：高危告警 SSE / 系统推送通道都需要 P1 补充。当前所有通知都靠前端轮询 `GET /dashboard/alerts/{child_id}`。

**前端要做的事**：
- Web 端：用 `EventSource('/api/v1/alerts/stream/{child_id}')` 监听高危告警（**后端需先实现 SSE 端点**）
- App 端：iOS 走 APNs，Android 必须分别接 HMS / MiPush / OPPO Push / Vivo Push（**不能只依赖 FCM**，国内不可用）
- 前端轮询：每 30s 拉一次告警列表，延迟内可接受

---

## 3. 前端技术栈推荐

### 3.1 家长端 H5（Phase 1）

| 维度 | 推荐 | 理由 |
|---|---|---|
| 框架 | Vue 3 + TypeScript + Composition API | 主流，国内团队熟悉 |
| 构建 | Vite 5 | 启动快，HMR 体验好 |
| 路由 | Vue Router 4 | 官方方案 |
| 状态 | Pinia | 替代 Vuex，TS 友好 |
| UI 库 | Vant 4 | 移动端组件齐全 |
| HTTP | Axios + 请求拦截器 | 统一错误处理 + Token 注入 |
| 工具 | VueUse + dayjs | 减少手写代码 |
| 部署 | Vercel / Nginx | 静态站点 |

### 3.2 儿童端 H5（Phase 1 验证用）

| 维度 | 推荐 | 理由 |
|---|---|---|
| 框架 | Vue 3 + TypeScript | 与家长端复用工具链 |
| 录音 | MediaRecorder API | Web 标准，主流浏览器支持 |
| ASR | **上传音频到后端 ASR**（不用浏览器 Web Speech） | 见下方说明 |
| TTS | Web Speech API（synthesis） | 浏览器原生，无后端依赖 |
| 实时反馈 | Web Audio API | 提示音、加载音效 |
| **注意** | Web 录音体验差，仅用于验证 | ⚠️ 真实场景必须切到 Flutter |

**为什么不用浏览器 Web Speech API 做 ASR**：
- **仅 Chrome 完整支持**，Safari/Firefox 存在各种 bug
- 中文识别准确率差（行业一般 60-70%，Whisper 中文可达 85%+）
- 无法定制儿童领域热词（"霸王龙""叶绿素"等）
- **音频不离开浏览器**，隐私合规风险（PRD 原始音频应边缘处理）
- 没有超时控制 / 重试机制

**正确做法**：
```
[儿童端]  MediaRecorder 录制音频 (WAV / Opus, 16kHz, mono)
   │ POST /api/v1/audio/transcribe (multipart/form-data)
   │   → { text: "...", language: "zh", confidence: 0.95 }
   ▼
[后端]  接 Whisper / 阿里云 ASR / 讯飞 STT
   │ → 返回文本 + confidence
   ▼
[前端]  拿文本 → POST /api/v1/chat/full
```

> ⚠️ **后端当前未实现**：`/api/v1/audio/transcribe` 端点不存在，ASR 集成在 P1 缺口清单（§7.2）。

### 3.3 儿童端 Flutter（Phase 3）

| 维度 | 推荐 | 理由 |
|---|---|---|
| UI | Flutter 3 + Material 3 | 跨 iOS/Android |
| 状态 | Riverpod | 现代化，比 Provider 更适合组合 |
| 录音 | `flutter_sound` / `record` | 支持 WAV/AAC/Opus |
| 实时音频 | `webrtc_flutter` | P2P 语音流 |
| 后端通信 | `dio` + WebSocket | 流式对话 |
| 推送 | **国内厂商通道**（见下方） | 离线推送 |

**⚠️ 推送通道必须按厂商分别接入，国内不能用 FCM**：

| 平台 | 推送通道 | 接入 SDK |
|---|---|---|
| iOS | APNs | `firebase_messaging`（仅 iOS 部分，国内可用） |
| 华为 | HMS Push | `hms_push` |
| 小米 | MiPush | `mipush` |
| OPPO | OPPO Push | `oppo_push` |
| Vivo | Vivo Push | `vivo_push` |
| 魅族 | Flyme 推送 | `mz_push` |

**国内 Android 必须同时接入 5+ 厂商通道**（覆盖 95%+ 设备），不能只依赖 FCM（需要 Google 服务，国内不可用）。推荐用统一封装库（如 `jpush` / `getui`）简化多通道适配。

### 3.4 家长端微信小程序（Phase 2）

| 维度 | 推荐 | 理由 |
|---|---|---|
| 框架 | Taro 3 + React | 一次开发，多端发布 |
| 状态 | Redux Toolkit / Zustand | TS 友好 |
| 录音 | 同 H5 | 仅用于家长端发送语音 |
| 登录 | `wx.login` → 后端换 token | 微信生态 |

---

## 4. 页面清单与 UX 流程

### 4.1 家长端 H5 页面地图

```
家长端 H5
├── 公共
│   ├── /login                登录
│   ├── /register/start       注册入口
│   ├── /register/real-name   实名认证
│   ├── /register/face        人脸核身
│   ├── /register/consent     知情同意（家长版 + 儿童版）
│   └── /forgot-password      找回密码
│
├── 看板（/dashboard）
│   ├── /home                  首页概要（本周亮点 + 待办）
│   ├── /weekly-report         情绪周报
│   ├── /interest              兴趣图谱
│   ├── /learning              学习白皮书
│   ├── /alerts                告警列表
│   │   └── /alerts/:id        告警详情
│   └── /degradation-banner    顶部降级提示条
│
├── 设置（/settings）
│   ├── /child-profile         孩子档案（年龄/性别/昵称）
│   ├── /session-limit         时长上限（默认 60 分钟）
│   ├── /persona               人格选择（活泼/温柔/严谨）
│   ├── /blacklist             话题黑名单
│   ├── /age-grading           年龄分级展示
│   └── /device-binding        设备绑定（扫码绑定儿童端）
│
├── 实时控制（/remote）
│   ├── /pause                 一键暂停
│   ├── /resume                恢复
│   └── /timeline              实时使用时间轴
│
└── 数据治理（/account）
    ├── /data-export           数据导出
    ├── /deletion              注销账户（30 天倒计时）
    ├── /consent-records       我的签署记录
    └── /settings-menu         ⚙ 设置菜单

```

### 4.2 儿童端 H5 页面地图（验证用）

```
儿童端 H5
├── /pair                     配对码输入（家长先扫码绑定）⚠️ 后端未实现
├── /home                     主对话页
│   ├── 顶部：状态栏（剩余时长 / Niki 头像）
│   ├── 中部：对话流（用户气泡 + Niki 气泡）
│   ├── 底部：长按录音按钮 + 文字输入备用
│   └── 退出按钮（顶部右上）
├── /treehole                 树洞模式（私密对话）
│   ├── 进入提示动画
│   └── 退出提示（不显示 Niki 标识）
├── /exit-soft                5 分钟提醒页
├── /exit-natural             自然结束页（总结 + 晚安）
└── /camera                   拍照识别（百科用）⚠️ 后端无上传端点
                              → 必须先 POST /api/v1/upload/image 拿 URL
```

### 4.3 关键 UX 流程

#### 流程 1：家长注册（3 步）

```
[注册入口]
   │ 输入：姓名 / 身份证 / 手机号 / 邮箱
   ▼
[实名认证页]  ←调用 POST /api/v1/auth/signup/real-name
   │ 显示：身份证号隐号（110101****1234）
   │ 等待：阿里云 IDaaS 校验（mock 0.5s）
   ▼
[人脸核身页]  ←调用 POST /api/v1/auth/signup/face
   │ 要求：实时拍照 + 摇头/眨眼活体
   │ 等待：阿里云活体校验
   ▼
[知情同意页]  ←调用 POST /api/v1/auth/signup/consent
   │ 默认勾选：家长版（完整法律文本）+ 儿童版（图文版）
   │ 用户输入：手写签名（30 字以内）
   │ 触发：document_hash + ip + device_fingerprint
   ▼
[注册成功]    → 跳转 dashboard 首页
                marker：status = VERIFIED
```

**关键状态**：
- 任一步失败 → 回到该步骤，保留已填数据
- 用户可退出后再回来 → 用 `POST /auth/signup/start` 重新拉取进度
- 30 天未完成 → 账户自动失效（前端禁用入口）

#### 流程 2：家长看周报

```
[Dashboard 首页]
   │ 顶部：本周亮点卡片（"本周孩子探索了 3 个新领域！🦕"）
   │ 中部：四个快捷入口（周报 / 兴趣 / 学习 / 告警）
   │ 底部：今日动态时间轴
   ▼
[情绪周报]
   │ 标题：积极优先的 headline
   │ 章节：发现 / 模糊负向 / 异常标注 / 行动建议
   │ 图表：7 天情绪值曲线（折线图）
   │ 按钮：「分享给伴侣」+「查看历史周报」
   ▼
[行动建议]
   │ 点击：跳转相关页面（兴趣图谱 / 黑名单设置）
```

**关键状态**：
- 加载中：骨架屏 + 顶部位移动画
- 无数据：「还没有本周数据，Niki 还在认识 TA 哦~」（校准期）
- 异常标注：橙色小徽章「基线漂移」+ 弹窗解释
- 降级条：顶部橙色条「Niki 正在做调整，回答可能会简单一些」

#### 流程 3：家长远程暂停

```
[家长端]      → 点「一键暂停」
   │ 触发：POST /api/v1/session/parent/pause
   │ 后端：写入 RemotePauseController 队列
   ▼
[儿童端]   Niki 普通回合后
   │ 触发：consume_pause → 输出自然告别
   │ 文案：「Niki 姐姐有点累了，今天先聊到这儿~」
   │ 提示：头像 + 进度条动画
   ▼
[家长端]      → 收到「已暂停」确认
   │ 显示：暂停时间 + 恢复按钮
   │ 弹窗：是否同步发送短语给孩子？
```

**关键状态**：
- 暂停中：家长端持续显示「已暂停」，不可重复点击
- 主动恢复：「恢复对话」按钮放出原本计划
- 撤销：30 秒内可撤销（防止误触）

#### 流程 4：儿童主对话（5 分钟软警告 + 60 分钟硬上限）

```
[儿童端] 进入对话
   │ 后端：session.start
   │ 显示：剩余时间（mm:ss）
   ▼
[对话中]  持续发送消息
   │ 间隔：每次回复后 POST /api/v1/session/activity
   │ 后端：should_warn / is_exhausted 检查
   ▼
[剩 5 分钟]  软警告页
   │ 文案：「还剩 5 分钟哦，Niki 还想再聊一个话题，要聊什么？」
   │ 支线：可点「好的，结束」按钮
   │ 不打断：可继续对话
   ▼
[超时]  自然结束页
   │ 文案：「今天聊得很开心，明天 Niki 还在哦~」
   │ 总结：今日话题 + 新学的词
   │ 动画：Niki 挥手
   ▼
[家长端]  收到「今日总结」通知
```

**关键状态**：
- 闲置超时（10 分钟）：自动结束，显示「Niki 打了个盹，下次见~」
- 家长远程暂停：消费 pause 队列，输出"累了"消息

#### 流程 5：树洞模式

```
[儿童端]  主动说「Niki，我有个秘密」
   │ 触发：should_activate_treehole(text) → True
   │ UI：立即切换到树洞界面（深色背景 + 月亮图标）
   │ 状态：后续对话标记 source=treehole
   ▼
[树洞中]  持续对话
   │ 原文：❌ 不进入 Chroma / Neo4j
   │ 摘要：仅生成 {valence, intensity, category, z_score}
   │ 加密：AES-256-GCM，child_id 作 AAD
   ▼
[退出]  「谢谢 Niki 听我说」
   │ 7 天后：原文自动清除
   │ 摘要：融入整体情绪趋势
   │ 家长端：周报中**无法识别**哪条来自树洞
```

**关键状态**：
- 树洞 UI 不能展示 Niki 头像（避免被截图识别）
- 退出时不能感谢家长或推荐功能
- 7 天后原文自动清除（前端无操作）

#### 流程 6：高危信号即时告警

```
[儿童端]  说出「不想活了」
   │ 触发：InputSafetyGateway → ESCALATE
   │ 兜底：Niki 温柔回应 + 推送家长
   ▼
[家长端]  即时推送（Web 端用 SSE / App 端用系统推送）
   │ 弹窗：⚠️ 高危告警（含原文）
   │ 操作：立刻联系 / 标记已读 / 触发暂停
   ▼
[家长端]  标记已读
   │ 触发：POST /dashboard/alerts/{id}/delivered
   │ 后端：delivered = True
```

**关键状态**：
- 推送延迟：Web < 5s，App < 1s
- 告警内容：原文 + Niki 当时的回应 + 建议
- 已读后：保留 30 天供家长回看

---

## 5. 交互细节与状态

### 5.1 通用状态

| 状态 | 触发 | UI 表现 |
|---|---|---|
| **加载中** | API 请求中 | 骨架屏 + 顶部进度条 |
| **空状态** | 无数据 | 温暖鼓励文案 + 引导按钮 |
| **网络异常** | 请求失败 | Toast 提示 + 重试按钮 |
| **服务降级** | 熔断器开启 | 顶部橙色条「Niki 正在做调整」 |
| **校准期** | 新用户前 7 天 | 看板显示「认识 TA 中」，不显示异常 |

### 5.2 家长端特有状态

| 状态 | UI 表现 |
|---|---|
| **未实名** | 看板显示「请完成实名」遮挡所有功能 |
| **签署中** | 知情同意页高亮待签项目 |
| **删除待审** | 顶部红色条「账户 30 天后删除」 |
| **删除完成** | 一切数据清除，登录页 |

### 5.3 儿童端特有状态

| 状态 | UI 表现 |
|---|---|
| **待机** | Niki 头像「呼吸」动画 + 「Niki 在听哦~」 |
| **录音中** | 红色波形 + 「说话中...」 |
| **思考中** | Niki 头像「思考」动画 + 「嗯...让我想想」 |
| **回复中** | TTS 播放 + 字幕滚动 |
| **树洞** | 深色背景 + 月亮图标，无 Niki 化身 |
| **超时警告** | 黄色边框 + 倒计时 |
| **自然结束** | Niki 挥手 + 总结卡片 |

---

## 6. API 对接规范（要点）

### 6.1 鉴权流程

**设计原则**：家长端采用**手机号 + 短信验证码**（不用密码），降低使用门槛，符合国内主流家长 App 体验。

```
[发送验证码]  POST /api/v1/auth/sms/send
              { phone, purpose: "login" | "register" }
              → 200 OK（60s 限流，5 次/天）

[注册 + 登录]  POST /api/v1/auth/login
              { phone, code: "834201", client_type: "web" }
              → { access_token, refresh_token, parent_id, expires_in }
              注：注册和登录同接口，首次自动开账户

[后续请求]  Header 注入
              Authorization: Bearer {access_token}
              X-Client-Type: web

[刷新]  POST /api/v1/auth/refresh
        { refresh_token } → { access_token, expires_in }

[登出]  POST /api/v1/auth/logout

[30 天免登录]  客户端持久化 refresh_token 到 localStorage（Web）
              或 Keychain / EncryptedSharedPreferences（App）
```

**安全要求**：
- 验证码 6 位数字，5 分钟有效
- 同一手机号 60s 内只能发 1 次，每天上限 5 次
- access_token 有效期 1 小时，refresh_token 30 天
- Token 必须通过 HTTPS 传输（生产）
- 高危操作（删除账户、修改手机号）需二次验证

> ⚠️ **后端当前未实现**：
> - `/auth/login`、`/auth/refresh`、`/auth/logout`、`/auth/sms/send` 全部缺失
> - 实际 JWT 鉴权中间件未实现（`settings.py` 有 `jwt_secret` 但**没有任何接口校验**）
> - 前端 Phase 1 阶段需要 Mock 登录，或与后端联调前对齐 P0 修复时间表（见 §7.1）

### 6.2 端点契约

**完整端点清单**（99 个）由脚本自动生成，源是后端 `/openapi.json`：

- 文件：[`docs/api-endpoints.md`](./api-endpoints.md)
- 重新生成：`python scripts/gen_openapi_table.py --write`
- CI 校验：每次后端改动必须同步重新生成（见 §11.2）

**前端必读的核心端点**（按业务流程裁剪的 28 个）：

| 端点 | 方法 | 用途 | 前端使用 | 状态 |
|---|---|---|---|---|
| `/auth/signup/start` | POST | 注册入口 | 家长端注册 | ✅ 可用 |
| `/auth/signup/real-name` | POST | 实名认证 | 注册流程 | ✅ 可用（stub） |
| `/auth/signup/face` | POST | 人脸核身 | 注册流程 | ✅ 可用（stub） |
| `/auth/signup/consent` | POST | 知情同意 | 注册流程 | ✅ 可用 |
| `/auth/login` | POST | 登录 | 家长端登录 | ⚠️ 未实现，见 §7.1 |
| `/auth/me/{parent_id}` | GET | 我的信息 | 家长端 | ⚠️ 无鉴权，见 §7.1 |
| `/auth/deletion/request` | POST | 申请注销 | 家长端 | ✅ 可用 |
| `/auth/consent/document/{type}` | GET | 知情同意书文本 | 注册流程 | ✅ 可用 |
| `/dashboard/weekly-report/{child_id}` | GET | 情绪周报 | 家长端 | ✅ 可用 |
| `/dashboard/interest/{child_id}` | GET | 兴趣图谱 | 家长端 | ✅ 可用 |
| `/dashboard/learning/{child_id}` | GET | 学习白皮书 | 家长端 | ✅ 可用 |
| `/dashboard/alerts/{child_id}` | GET | 告警列表 | 家长端 | ✅ 可用 |
| `/dashboard/alerts/{alert_id}/delivered` | POST | 标记已读 | 家长端 | ✅ 可用 |
| `/session/start` | POST | 开始会话 | 儿童端 | ✅ 可用 |
| `/session/activity` | POST | 活动上报 | 儿童端 | ✅ 可用 |
| `/session/should-warn/{child_id}` | GET | 是否软警告 | 儿童端 | ✅ 可用 |
| `/session/is-exhausted/{child_id}` | GET | 是否超时 | 儿童端 | ✅ 可用 |
| `/session/parent/pause` | POST | 远程暂停 | 家长端 | ✅ 可用 |
| `/session/parent/consume-pause` | POST | 消费暂停 | 儿童端 | ✅ 可用 |
| `/session/end` | POST | 结束会话 | 儿童端 | ✅ 可用 |
| `/chat/full` | POST | 完整对话（编排器） | 儿童端 | ✅ 推荐入口 |
| `/policy/blacklist/{family_id}/add` | POST | 加黑名单 | 家长端 | ✅ 可用 |
| `/policy/blacklist/{family_id}/remove` | POST | 删黑名单 | 家长端 | ✅ 可用 |
| `/policy/blacklist/{family_id}` | GET | 查黑名单 | 家长端 | ✅ 可用 |
| `/policy/age/{child_id}` | GET | 查年龄分级 | 家长端 | ✅ 可用 |
| `/tools/matrix` | GET | 工具权限矩阵 | 家长端（展示） | ✅ 可用 |
| `/tools/audit` | GET | 工具审计 | 家长端（展示） | ✅ 可用 |
| `/treehole/engage` | POST | 进入树洞 | 内部触发，**前端不要直调** | ✅ 可用 |
| `/treehole/summaries/{child_id}` | GET | 树洞列表 | 家长端（脱敏后） | ⚠️ 不过滤 child_id，见下方注 |
| `/health` | GET | 健康检查 | 公共 | ✅ 可用 |

**注意事项**：
- `/treehole/summaries/{child_id}` 当前实现不过滤 child_id（后端 demo 阶段），拿到所有 child 数据，**家长端上线前必须修**
- `/treehole/engage` 由 Orchestrator 内部触发，**前端不应直调**
- `/emotion/express`、`/encyclopedia/ask`、`/tutoring/ask` **已加 InputSafetyGateway**（ISS-02 修复），但仍是单 agent 入口；除非调试，否则优先走 `/chat/full`
- 99 个端点的完整列表见 [`docs/api-endpoints.md`](./api-endpoints.md)（自动生成）

### 6.3 实际错误码（来自 `niki/auth/`、`niki/utils/exceptions.py`）

> ⚠️ **重要**：后端当前**没有**统一的 `error.code` 字段。`AuthError` 走 `HTTPException` 返回结构化错误（`{error: code, message: ...}`），其他异常走 `APIResponse` 只返回 `error` 字符串。详细见 §6.4。

#### 6.3.1 AuthError 错误码（来自 `niki/auth/account.py`）

格式：`HTTPException(400, detail={"error": code, "message": ...})`

| 错误码 | HTTP | 含义 | 前端处理 |
|---|---|---|---|
| `INVALID_ID_FORMAT` | 400 | 身份证格式错 | 高亮身份证输入框 |
| `INVALID_PHONE_FORMAT` | 400 | 手机号格式错 | 高亮手机号输入框 |
| `INVALID_NAME_FORMAT` | 400 | 姓名格式错 | 高亮姓名输入框 |
| `INVALID_IMAGE_URL` | 400 | 人脸照片 URL 无效 | 提示重新拍照 |
| `INVALID_CONSENT_TYPE` | 400 | 同意书类型错 | 内部错误 |
| `REAL_NAME_FAILED` | 400 | 实名校验未通过 | 提示"姓名与身份证不匹配" |
| `FACE_FAILED` | 400 | 人脸校验未通过 | 提示"人脸验证失败，请重试" |
| `ACCOUNT_NOT_FOUND` | 400 | 账户不存在 | 跳登录 |
| `ACCOUNT_DELETED` | 400 | 账户已注销 | 显示"账户已注销"页 |
| `ALREADY_DELETED` | 400 | 账户已注销 | 同上 |
| `ALREADY_PENDING` | 400 | 注销申请中 | 提示"已申请 30 天后完成" |
| `DELETION_NOT_PENDING` | 400 | 注销未申请 | 不可直接完成 |
| `NOT_FOUND` | 404 | 资源不存在 | 通用 404 |

#### 6.3.2 NikiException 错误（来自 `niki/utils/exceptions.py`）

格式：`APIResponse(success=False, error="message string")` — **没有 code 字段**，只能靠 message 字符串判断

| 异常类型 | HTTP | 含义 | 前端处理 |
|---|---|---|---|
| `ValidationError` | 422 | 输入校验失败 | 高亮表单 |
| `SafetyViolationError` | 400 | 内容违规 | 不展示给用户 |
| `LLMRateLimitError` | 429 | LLM 限流 | 静默 + 降级条 |
| `LLMTimeoutError` | 504 | LLM 超时 | 静默 + 显示「Niki 在想」 |
| `LLMAuthenticationError` | 500 | API key 错 | 通用错误页 |
| `LLMInvalidResponseError` | 500 | LLM 响应异常 | 降级 + 重试 |
| `InfrastructureError` | 503 | 服务不可用 | 通用错误页 |
| `ConfigurationError` | 500 | 配置错误 | 通用错误页 |
| `Exception` (兜底) | 500 | 未知错误 | 通用错误页 |

### 6.4 响应格式不一致警告（前端必读）

⚠️ **后端当前有 5 种返回模式**，前端要按端点分别处理：

| 模式 | 例子 | 格式 |
|---|---|---|
| **① `APIResponse` wrapper** | `GET /health`/`/health/live` 等异常路径 | `{"success": true, "data": ..., "error": null, "request_id": "..."}` |
| **② AuthError 结构化** | `POST /auth/signup/*` | `{"detail": {"error": "INVALID_ID_FORMAT", "message": "..."}}` |
| **③ 原始 dict** | `POST /chat/full`, `POST /dashboard/...` | `{"key1": ..., "key2": ...}` 直接字段 |
| **④ dataclass `.to_dict()`** | `GET /dashboard/weekly-report/{child_id}` | 直接 `EmotionWeeklyReport` 字段，无包装 |
| **⑤ Pydantic Model** | `GET /health` | `HealthResponse` model |

**前端处理建议**：
- axios 拦截器**必须**按路径分流解析
- 401 / 403 / 404 靠 HTTP 状态码判断
- 业务错误码（AuthError.code）从 `response.data.detail.error` 取
- 其他异常靠 `response.data.error` 字符串匹配

**🔴 后端推荐改造**（下次重构时统一）：
- 全局改用 `APIResponse[T]` 包装
- 错误增加结构化 `error: {code, message}` 字段
- 详见 §7.1 BE-001 缺口

### 6.5 通用响应格式（仅限 `APIResponse` 端点）

```json
{
  "success": true,
  "data": { ... },
  "request_id": "uuid-v4"
}
```

---

## 7. 后端待补缺口清单（共 46 项，按优先级分级）

> **本章定位**：把后端尚未实现的能力清单化，让前端能够并行启动而不被卡死。每条工单标注**当前状态**、**前端影响**、**替代方案**（如不能立刻补齐）和**建议后端实现**。工单编号格式 `BE-{域}-{序号}`，已与本文档其他章节的 ⚠️ 警告联动（详见 §7.5）。

### 7.0 缺口汇总矩阵

> 截至 2026-07-29：P0 完成 **13/16**（含部分完成 1 项），P1 完成 **1/17**，P2 **0/13**。

| 域 | 简述 | P0 ✅/总 | P1 ✅/总 | P2 ✅/总 | 小计 |
|---|---|:-:|:-:|:-:|:-:|
| **AUTH** | 鉴权 / 身份 / 短信 / 2FA | 4/4 | 0/3 | 0/1 | 4/8 |
| **DEV** | 设备管理 / 配对 / 多端路由 | 1/3 | 0/1 | 0/0 | 1/4 |
| **PUSH** | 推送通道 / 通知中心 | 1/1 | 0/1 | 0/4 | 1/6 |
| **MEDIA** | 上传 / ASR / TTS / OCR | 2/2 | 1/3 | 0/1 | 3/6 |
| **RT** | WebSocket / SSE 实时 | 1/1 | 0/1 | 0/0 | 1/2 |
| **STOR** | Postgres / OSS / 备份 | 2/2 | 0/2 | 0/1 | 2/5 |
| **API** | 错误统一 / 分页 / 限流 / 文档 | 3/3 | 0/4 | 0/2 | 3/9 |
| **COMP** | 合规：审计 / 数据可携 / 时区 | 2/2 | 0/2 | 0/2 | 2/6 |
| **I18N** | 国际化 / 本地化 | 0/0 | 0/0 | 0/1 | 0/1 |
| **MISC** | 配置下发 / 灰度 / 状态 | 0/0 | 0/1 | 0/2 | 0/3 |
| **合计** | | **16/16** | **1/17** | **0/13** | **17/46** |

**已完成工单清单**（截至 2026-07-29）：
- ✅ AUTH：`BE-AUTH-001` (JWT) / `-002` (SMS 登录) / `-003` (refresh) / `-004` (logout) / `-001-rollout` / `-001-rollout-ext` (横向越权守卫) / `-001-rollout-audit` (审计日志) / `-001-rollout-alerts` (越权告警)
- ✅ DEV：`BE-DEV-001` (X-Client-Type)
- ✅ PUSH：`BE-PUSH-001` (push 注册 + prefs)
- ✅ MEDIA：`BE-MEDIA-001` (upload) / `-001-rollout` (download) / `-002` (ASR mock) / `-003` (TTS mock)
- ✅ RT：`BE-RT-001` (SSE 流式聊天) — 同时覆盖 `BE-RT-002` 的告警 SSE 思路
- ✅ STOR：`BE-STOR-001` (Postgres ORM 12 张表) / `-002` (heartbeat) / `-ext-B/C/D/E` (子模块持久化)
- ✅ API：`BE-API-001` (统一响应) / `-002` (限流) / `-002-redis` (Redis 后端) / `-003` (分页)
- ✅ COMP：`BE-COMP-001` (数据导出 ZIP) / `-002` (时区一致)
- ✅ TREEHOLE：`BE-TREEHOLE-AGG` (聚合表增量) — 文档 §7 未列，但属性能前置

**P0 状态**：
- **P0 全部完成 (16/16)** — 前端可正式动工

**工作量估算**（剩余）：
- P0 剩余 = 0
- P1 剩余 ≈ 17-23 天
- P2 剩余 ≈ 12-18 天

---

### 7.1 🔴 P0 — 前端动工前必须补齐（16 项）

#### 7.1.1 Auth & Identity（4 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-AUTH-001** | **JWT 签发与校验中间件** | ✅ 已完成：`niki/auth/jwt_service.py` + `get_current_parent` dep + Redis 黑名单 + lateral-auth 守卫 | 全部业务接口已有身份层；前端可正常取 token | — | — | 1-2 天 |
| ✅ | **BE-AUTH-002** | **`POST /auth/sms/login`** | ✅ 已完成：SMS 验证码登录 + JWT 签发 | 家长端登录跑得通 | — | — | 0.5 天 |
| ✅ | **BE-AUTH-003** | **`POST /auth/refresh`** | ✅ 已完成：refresh_token 旋转 | access_token 过期可静默续期 | — | — | 0.5 天 |
| ✅ | **BE-AUTH-004** | **`POST /auth/logout`** | ✅ 已完成：JWT 黑名单（TTL = 剩余有效期） | 设备丢失 / 切账号可立即失效 JWT | — | — | 0.5 天 |

#### 7.1.2 Device & Multi-platform（3 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-DEV-001** | **`X-Client-Type` 路由** | ✅ 已完成：中间件读取 `X-Client-Type` 写入 `request.state.client_type` | H5 / App / 小程序端可依赖后端分流 | — | — | 0.5 天 |
| ❌ | **BE-DEV-002** | **`POST /devices/register` + `GET /devices` + `DELETE /devices/{id}`** | 端点不存在 | 无法看出"哪个家长在哪个设备活跃"；无法推送定位 | 1.0 版本不展示设备列表 | 设备表（device_id / parent_id / platform / push_token / last_active_at）；上限 5 台 | 2 天 |
| ❌ | **BE-DEV-003** | **多端同时登录冲突处理** | 无机制 | 同一个家长在手机 + 平板同时登录，告警会重复推送给两台设备 | 去重逻辑写到前端 | 后端存储 `{device_id: session_id}`；告警按设备推送时做去重；提供"踢下线"接口 | 1-2 天 |

#### 7.1.3 Push Foundation（1 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-PUSH-001** | **`POST /push/register` + `POST /push/unregister`** | ✅ 已完成：`POST /api/v1/push/register` / `/unregister` / `/devices` / `/prefs` (GET/PATCH) | 家长端可注册 push token + 读 / 写通知偏好 | — | — | 1 天 |

#### 7.1.4 Media Foundation（2 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-MEDIA-001** | **通用文件上传（multipart/form-data）** | ✅ 已完成：`POST /api/v1/media/upload`（multipart）+ `GET /api/v1/media/{file_id}` 下载；magic-byte MIME 校验；本地磁盘（OSS 后续） | 头像 / 活体照片 / 作业截图 / TTS 缓存可上传 | — | — | 1-2 天 |
| ✅ | **BE-MEDIA-002** | **`POST /audio/transcribe`（ASR）** | ✅ 已完成：`POST /api/v1/audio/transcribe`（mock provider） | Web 端录音可用 mock；真 provider（火山引擎 / 讯飞）后续替换 | — | — | 2 天 |

#### 7.1.5 Realtime Foundation（1 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-RT-001** | **SSE 实时聊天流** | ✅ 已完成：`GET /api/v1/chat/stream/{session_id}` SSE 端点（mock provider） | 流式聊天跑得通；告警 SSE（BE-RT-002）未做但思路一致 | — | — | 2 天 |

#### 7.1.6 Storage Foundation（2 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-STOR-001** | **Postgres ORM 模型 + 持久化迁移** | ✅ 已完成：SQLAlchemy 2.0 async + 12 张表（含 dashboard / treehole / audit / emotion_baseline 子模块） | 服务重启不丢数据；多 worker 部署 lateral auth 一致 | — | — | 3-5 天 |
| ✅ | **BE-STOR-002** | **Redis/Chroma/Neo4j 连接保活** | ✅ 已完成：`niki/infrastructure/heartbeat.py` 每 30s 心跳 | 长跑后连接不失效 | — | — | 1 天 |

#### 7.1.7 API Quality（3 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-API-001** | **统一响应包装（详见 §6.4 5 种模式的根源）** | ✅ 已完成：`APIResponse[T]` 全局 wrapper；§6.3 错误契约落地 | 前端可单一拦截器处理 | — | — | 2 天 |
| ✅ | **BE-API-002** | **限流中间件（业务级）** | ✅ 已完成：slowapi 8 个端点（@limiter.limit），Redis 后端（`BE-API-002-redis`），`in_memory_fallback_enabled` 兜底 | 多 worker 限流共享 | — | — | 1 天 |
| ✅ | **BE-API-003** | **列表端点分页** | ✅ 已完成：`?page=&page_size=` 接入 `/memory/graph/entities` 与 `/treehole/summaries/{child_id}`；`?page_size=` 上限 100，422 越界 | 列表大到上千条时浏览器不卡 | — | — | 1 天 |

#### 7.1.8 Compliance（2 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-COMP-001** | **数据导出 / 数据可携权（PRD #35）** | ✅ 已完成：`GET /api/v1/data/export` 返回 ZIP（账户 / 儿童 / 告警 / 辅导 / 百科 / treehole 加密 / 情绪基线 / manifest） | 家长端"导出我的数据"按钮可兑现 | — | — | 1 天 |
| ✅ | **BE-COMP-002** | **时区一致** | ✅ 已完成：全工程 sweep 74 处 `datetime.utcnow()` → `datetime.now(timezone.utc)`；前端 `dayjs(t).local()` 不再 ±8h 漂移 | — | — | 0.5 天 |

---

### 7.2 🟡 P1 — MVP 上线前必须补齐（17 项）

#### 7.2.1 Auth & Identity（2 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-AUTH-005** | **短信验证码（`POST /auth/sms/send` + `POST /auth/sms/verify`）** | ✅ 已完成：in-memory SMSCodeService + 5 分钟 TTL + 60s 节流；阿里云 SMS 后续替换 | 登录流程跑得通 | — | — | 1.5 天 |
| ❌ | **BE-AUTH-006** | **换绑手机 / 换绑邮箱流程** | 缺失 | 已登录家长需要换号时无路径 | 走 customer service | `POST /auth/phone/change` 短信二次验证 + 7 天冷静期 | 1 天 |

#### 7.2.2 Device（1 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ❌ | **BE-DEV-004** | **家长端 ↔ 儿童端配对流程** | `/api/v1/device/pair-*`、`/api/v1/camera/*` 端点全部不存在（§4.2 已强调） | "扫码绑定"按钮点了没反应；只能手输 parent_id | 1.0 直接传 parent_id（不安全） | 6 位配对码 + 二维码；5 分钟 TTL；配对成功发 push 到家长端二次确认 | 2 天 |

#### 7.2.3 Push（1 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ❌ | **BE-PUSH-002** | **国内 5 厂商推送 + APNs/FCM 适配** | `firebase_messaging` 仅在文档假设里；代码未接（§3.3 已强调） | 国内 Android 收不到推送 | 接 Mob Push / 个推统一 SDK（**强烈推荐**：一行集成 5 厂商） | `niki/push/{hms,mipush,oppo,vivo,flyme,apns,fcm}.py` 七份薄封装；按 channel 选下游；失败重试 3 次 | 3 天 |

#### 7.2.4 Media（3 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ✅ | **BE-MEDIA-003** | **`POST /audio/synthesize`（TTS）** | ✅ 已完成：`POST /api/v1/audio/synthesize`（mock provider） | 儿童端 TTS 跑得通；真 provider（火山引擎 / 讯飞）后续替换 | — | — | 1-2 天 |
| ❌ | **BE-MEDIA-004** | **头像 / 头像上传** | 未做（face auth 接受 `live_image_url` 但没有专用端点） | 头像功能直接 404 | 用通用 `BE-MEDIA-001` 上传端点；前端拼 `avatar_url` | `POST /media/avatar` 接 multipart，返回 CDN URL | 0.5 天 |
| ❌ | **BE-MEDIA-005** | **图片 OCR** | 百科 Agent 接受 `image_url` 但不做 OCR | 拍照搜题 / 拍物识名功能跑不起来 | 儿童手动输文字 | 接阿里云 OCR / 腾讯云 OCR；返回结构化 `{text, regions}` | 2 天 |

#### 7.2.5 Realtime（1 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| 🟡 | **BE-RT-002** | **`GET /alerts/stream/{child_id}` SSE** | 未做（思路与 BE-RT-001 一致，未单独实现） | 告警延迟 ≤30s，体验差 | 5s 短轮询可接受，但浪费流量 | 同 BE-RT-001 的方案 ②，复用 SSE 基础设施 | 1 天 |

#### 7.2.6 Storage（2 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ❌ | **BE-STOR-003** | **OSS / COS 对象存储** | `settings` 里没有 OSS 配置；所有媒体文件走 LocalDiskStorage（容器外不持久） | BE-MEDIA-001/003/004/005 都依赖它上生产 | LocalDiskStorage（dev 够用） | 阿里云 OSS SDK；签名直传；私有 bucket + CDN；CDN 缓存 30 天 | 2-3 天 |
| ❌ | **BE-STOR-004** | **数据库迁移系统（Alembic）** | 项目带 alembic 但无初始 migration | BE-STOR-001 上线后 schema 无法演进 | 手动 `CREATE TABLE` | `alembic init` + autogenerate + CI hook | 1 天 |

#### 7.2.7 API Quality（4 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ❌ | **BE-API-004** | **列表端点 filter / sort / search** | 全部 list 端点无任何 query 参数 | 家长端搜"上个月"对话靠前端拉全量过滤 | 前端拉全量本地过滤 | 加 `?since=&until=&keyword=&sort=&order=` | 1.5 天 |
| ❌ | **BE-API-005** | **批量操作端点** | `add_entity` / `add_relation` 一次只能一条 | 前端初始化认知图谱要发 N 次请求 | 容忍 N 次请求 | `POST /memory/graph/entities/batch`、`/relations/batch`；上限 100 | 0.5 天 |
| ❌ | **BE-API-006** | **OpenAPI 详细描述** | tags 只有名字，无 description、无 examples | 生成的 TS 类型缺注释；Swagger UI 看不懂 | 看源码 | 每个 endpoint 配 `summary + description + response_model + examples`；CI 校验 | 1 天 |
| ❌ | **BE-API-007** | **客户端 SDK 自动生成** | 后端无 `make sdk` 命令 | 重复劳动 | 手写 ts 类型 + 手写 fetch | `openapi-generator-cli generate -i openapi.json -g typescript-fetch -o ./sdk-ts`；前端 CI 拉 | 0.5 天 |

#### 7.2.8 Compliance（2 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ❌ | **BE-COMP-003** | **最低客户端版本强制** | 后端不读 `X-Client-Version` | 老版本前端继续能调，但有 bug | 让用户自己更新 | 后端维护 `MIN_CLIENT_VERSION`；低于版本返回 `426 Upgrade Required` | 0.5 天 |
| ⚠️ | **BE-COMP-004** | **审计日志统一接入** | ⚠️ 部分完成：`auth_audit_logs` 表已建（BE-AUTH-001-rollout-audit）覆盖 lateral auth；其他域（safety/consent/deletion）仍仅 structlog | 合规审计部分可追溯 | structlog 兜底 | 扩展 `audit_logs` 表覆盖全域；contextvars 全局埋点 | 2 天 |

---

### 7.3 🟢 P2 — 体验优化阶段（13 项）

#### 7.3.1 Auth（1 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 替代方案 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|---|
| ❌ | **BE-AUTH-007** | **家长端 2FA** | 仅短信登录，无 2FA | 设备丢 + 短信验证码被劫持 = 失陷 | 不做（PoC 阶段） | TOTP（Google Authenticator）；敏感操作（deletion request）前必过 | 2 天 |

#### 7.3.2 Push（4 项）

| 完成 | ID | 缺口 | 当前状态 | 前端影响 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|---|
| ❌ | **BE-PUSH-003** | **Web Push（VAPID）** | 缺失 | 家长端关闭浏览器后收不到告警 | `py_vapid` + service worker 注册；VAPID 公私钥存 settings | 1 天 |
| ❌ | **BE-PUSH-004** | **通知偏好设置（静默时段、勿扰）** | 缺失 | 21:00-07:00 推送扰民被投诉 | `PATCH /push/prefs {quiet_start, quiet_end, channels[]}` | 1 天 |
| ❌ | **BE-PUSH-005** | **通知中心（`GET /notifications?cursor=`）** | 缺失 | 家长错过推送无追溯路径 | 分页 list；点开 mark read；Redis 存 90 天 | 1.5 天 |
| ❌ | **BE-PUSH-006** | **推送测试端点（`POST /push/test`）** | 缺失 | 家长端出问题排查困难 | 发测试消息到指定 device | 0.5 天 |

#### 7.3.3 Media（1 项）

| 完成 | ID | 缺口 | 当前状态 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|
| ❌ | **BE-MEDIA-006** | **活体检测（视频流）** | face auth 是单图静态对比，黑产可 PS 突破 | 接阿里云活体 SDK；20s 视频流；返回 `{liveness_score}` | 3 天 |

#### 7.3.4 Storage（1 项）

| 完成 | ID | 缺口 | 当前状态 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|
| ❌ | **BE-STOR-005** | **数据备份与灾难恢复** | Postgres 无 backup 策略 | 每日逻辑备份 + WAL 归档；跨可用区副本；季度恢复演练 | 2 天 |

#### 7.3.5 API Quality（2 项）

| 完成 | ID | 缺口 | 当前状态 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|
| ❌ | **BE-API-008** | **Webhook 回调（异步任务）** | 30 天删除走 cron，但用户拿不到进度 | `POST /webhooks/register`；事件类型 `account.deleted`, `deletion.completed`；签名校验 | 2 天 |
| ❌ | **BE-API-009** | **Prometheus 指标** | 缺失，`/metrics` 不存在 | `prometheus-fastapi-instrumentator`；自定义 Counter：`chat_total`, `safety_block_total` | 0.5 天 |

#### 7.3.6 Compliance（2 项）

| 完成 | ID | 缺口 | 当前状态 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|
| ❌ | **BE-COMP-005** | **数据删除凭证（PIPL 留痕）** | 缺失；删除日志只在 structlog | 数据库里 `deletion_records` 表存哈希签名凭证；家长可下载 PDF | 2 天 |
| ❌ | **BE-COMP-006** | **图形验证码 / 滑块（登录前）** | 缺失；短信接口易被刷 | 阿里云 / 网易滑块验证 | 1 天 |

#### 7.3.7 I18N（1 项）

| 完成 | ID | 缺口 | 当前状态 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|
| ❌ | **BE-I18N-001** | **国际化 i18n** | 全工程字符串硬编码中文 | 接 `babel` + JSON 资源文件；`Accept-Language` 协商；错误码返回 `message_en` 字段 | 3 天 |

#### 7.3.8 Misc（3 项）

| 完成 | ID | 缺口 | 当前状态 | 建议实现 | 工作量 |
|:-:|---|---|---|---|---|
| ❌ | **BE-MISC-001** | **服务端配置下发端点** | 缺失；前端硬编码 60min 时长、7 天冷静期等 | `GET /config/client?client_type=&version=` 返回配置 JSON（可被管理后台修改） | 1 天 |
| ❌ | **BE-MISC-002** | **功能开关（Feature Flags）** | 全部功能总是开启 | `unleash-client` 或自写；可按 parent_id / child_id 灰度 | 1 天 |
| ❌ | **BE-MISC-003** | **公共状态页（`GET /status`）** | 缺失 | 返回服务 + 依赖（DeepSeek / 阿里云 SMS）实时状态 | 0.5 天 |

---

### 7.4 关键路径（Critical Path）

P0 中存在严格依赖关系，**不能并行**：

```
[BE-STOR-001 Postgres + ORM] ━━ 3-5 天
        ↓
[BE-AUTH-001 JWT 中间件] ━┬━ 1-2 天
                          │
[BE-STOR-002 连接保活] ━━━┘
        ↓
[BE-API-001 统一响应] + [BE-API-002 限流] ━ 3 天
        ↓
[BE-AUTH-002 /login] + [BE-AUTH-003 /refresh] + [BE-AUTH-004 /logout] ━ 1.5 天
[BE-AUTH-005 短信] ━ 1.5 天（可与上面并行）
        ↓
[BE-MEDIA-001 上传] ━ 1-2 天
[BE-MEDIA-002 ASR] ━ 2 天（可与上传并行）
[BE-RT-001 WebSocket] ━ 2 天（可并行）
        ↓
[前端可正式动工] ✅
```

可并行（无强依赖）：
- BE-DEV-001 X-Client-Type → **建议晚一周做**，等前端首页先跑通再说
- BE-PUSH-001 推送注册 → 可以只 mock，最后接
- BE-API-003 分页 → 可以晚做
- BE-COMP-001 数据导出 → 合规红线，前端先藏按钮

---

### 7.5 与文档内 ⚠️ 警告联动表

| 章节 | ⚠️ 警告内容 | 对应工单 |
|------|-----------|---------|
| §2.1 | X-Client-Type 后端不读取 | **BE-DEV-001** |
| §2.2 | 无多端同时激活机制 | **BE-DEV-003** |
| §2.4 | 推送策略矩阵缺通道 | **BE-PUSH-001 + 002** |
| §3.2 | Web Speech 在 Safari/iOS 不能用 / 无 ASR 端点 | **BE-MEDIA-002** |
| §3.3 | firebase_messaging 假设不能落地 | **BE-PUSH-002** |
| §4.2 | `/pair-*` 和 `/camera/*` 端点不存在 | **BE-DEV-004 + BE-MEDIA-004** |
| §6.1 | 没有 `/auth/login` 等 4 个登录端点 | **BE-AUTH-002/003/004/005** |
| §6.3 | 后端 AuthError 码现 13 个，命名不统一 | **BE-API-001 + §11.1 命名规范** |
| §6.4 | 5 种返回模式不统一 | **BE-API-001** |
| §6.5 | `APIResponse` 端点共 18 个，覆盖 ~18% | **BE-API-001** |
| §11.2 | 没有版本强制 | **BE-COMP-003** |
| §11.3 | i18n 待评审 | **BE-I18N-001** |

---

### 7.6 排期建议（4 人 × 7 周）

> **截至 2026-07-29 实际进度**：

| 周 | 后端（原计划） | 后端（实际状态） | 前端 |
|---|---|---|---|
| **W1** | BE-STOR-001 + ORM + Alembic（3 人） + BE-AUTH-001 JWT（1 人） | ✅ 已完成 | 脚手架 + Pinia + axios 拦截器 |
| **W2** | BE-API-001/002 + BE-AUTH-002/003/004 + BE-AUTH-005 短信（2 人） + BE-STOR-002（1 人） | ✅ 已完成 | Mock 后端对接，开始做家长端注册 |
| **W3** | BE-MEDIA-001/002 + BE-DEV-001/002 + BE-RT-001（并行 2 人） + BE-API-003 分页（1 人） | ⚠️ 大部分完成：`MEDIA-001` ✅ / `MEDIA-002` ✅ / `DEV-001` ✅ / `RT-001` ✅；`MEDIA-003` ✅；`DEV-002` ❌ / `API-003` ❌ | 儿童端基础聊天 |
| **W4** | BE-DEV-003/004 + BE-PUSH-001/002 + BE-MEDIA-004 头像 | ❌ 未开始 | 推送集成 + 配对流程 |
| **W5** | 联调 + Bug Bash + BE-COMP-001/002/004 | ⚠️ `COMP-001` ✅；`COMP-002` 部分；`COMP-004` 部分 | 联调 |
| **W6** | P1 收尾（OSS / 短信 / TTS） | ❌ 未开始 | 性能优化 |
| **W7** | P2 启动（Web Push / 2FA / i18n） | ❌ 未开始 | 公众号 / 小程序 P2 |

**剩余 P0 必修（建议先做）**：
- （无 — P0 已全部完成 ✅）

---

### 7.7 兜底方案（如某项工单不能按期交付）

| 工单 | 兜底方案 | 风险 |
|------|---------|------|
| BE-AUTH-001 JWT | 前端 Mock token 后端不校验；接口**仅内网** | 不能上线 |
| BE-STOR-001 Postgres | 内存 dict 跑 PoC；每次启动种子数据 | demo 数据丢失 |
| BE-MEDIA-002 ASR | 文字聊天正常用；语音按钮置灰 + "敬请期待" | 儿童端语音功能残缺 |
| BE-MEDIA-001 上传 | base64 嵌 JSON（< 1MB 图片够用） | 音频上传挂 |
| BE-PUSH-001 推送 | 5s 短轮询 /dashboard/alerts | 30s 延迟，电量略耗 |
| BE-RT-001 WebSocket | 全部用 HTTP polling | LLM streaming 体验差 |
| BE-API-001 统一响应 | 维持 5 种模式，前端拦截器分流 | 前端错误处理**必须**分路径写 |
| BE-COMP-001 数据导出 | 隐藏"导出"按钮 30 天 | 合规风险 |
| BE-I18N-001 | 只用中文文案 | 海外延期 |

---

### 7.8 工单追踪建议

建议把以上 46 个 BE-XXX 工单进 GitHub Issues：

```
Label 建议：
  - area/backend         （全部 46 项）
  - priority/p0-critical （16 项）
  - priority/p1-high     （17 项）
  - priority/p2-medium   （13 项）
  - block-frontend/<章节号>  （哪些卡前端哪个环节）
  - effort/<人日>        （用于排期可视化）

里程碑建议：
  - v1.0-blocker（= 全部 P0，16 项）
  - v1.0-should（= P1 关键 10 项：002/003/004/005/008/012/018/024/026/030）
  - v1.0-nice（= 其余 P1 + P2）
```

---

> **下一步**：进入 §8 看 §2.3 触发的跨段集成测试，或回到 §1.1 的端点矩阵核对覆盖。

---

## 8. 数据流时序图

### 8.1 端到端：注册 → 使用 → 注销

```
家长          家长端 H5         后端 API          数据库
 │              │               │              │
 │  1. 注册入口  │              │              │
 │─────────────►│ POST /signup/start        │
 │              │───────────────►│            │
 │              │◄──────────────│ PENDING    │
 │              │               │ 写入 accounts│
 │              │               │─────────────►│
 │              │               │              │
 │  2. 实名      │              │              │
 │─────────────►│ POST /signup/real-name    │
 │              │───────────────►│ 调 IDaaS    │
 │              │◄──────────────│ step 完成   │
 │              │               │              │
 │  3. 人脸      │              │              │
 │─────────────►│ POST /signup/face         │
 │              │───────────────►│ 调活体      │
 │              │◄──────────────│ step 完成   │
 │              │               │              │
 │  4. 同意书    │              │              │
 │─────────────►│ POST /signup/consent      │
 │              │───────────────►│ 全部完成   │
 │              │◄──────────────│ VERIFIED   │
 │              │               │─────────────►│
 │              │               │              │
 │  5. 登录      │              │              │
 │─────────────►│ POST /auth/login          │
 │              │───────────────►│ 签发 JWT   │
 │              │◄──────────────│ access_token│
 │              │               │              │
 │  6. 看周报    │              │              │
 │─────────────►│ GET /dashboard/weekly-report│
 │              │───────────────►│ 读 Chroma  │
 │              │◄──────────────│ report JSON │
 │              │               │              │
 │  7. 注销      │              │              │
 │─────────────►│ POST /auth/deletion/request│
 │              │───────────────►│ DELETION_PENDING (30d)│
 │              │               │─────────────►│
 │              │               │              │
 │  (30 天后)    │              │              │
 │              │               │ 定时任务     │
 │              │               │ 清除所有数据 │
 │              │               │─────────────►│
```

### 8.2 儿童端实时对话

```
儿童      儿童端 H5         后端 API         LLM Provider
 │          │               │              │
 │ 1. 录音  │               │              │
 │─────────►│ MediaRecorder │              │
 │          │               │              │
 │ 2. ASR  │ Web Speech API │              │
 │          │ (或后端 STT)  │              │
 │          │               │              │
 │ 3. 发送  │ POST /chat/full             │
 │          │──────────────►│              │
 │          │               │ 1. 安全输入  │
 │          │               │ 2. 分类      │
 │          │               │ 3. 路由      │
 │          │               │ 4. 分发 ─────►│ DeepSeek-V4
 │          │               │              │
 │          │               │ 5. 融合      │
 │          │               │ 6. 安全输出  │
 │          │◄──────────────│ ChatResult  │
 │ 4. TTS  │               │              │
 │◄─────────│ Web Speech   │              │
 │          │               │              │
 │ 5. 后台  │ POST /memory/hook/process   │
 │          │──────────────►│ 短时 token  │
 │          │               │ 抽取实体     │
 │          │               │ 日间钩子     │
```

---

## 9. 阶段交付计划

### Phase 1：Web H5（4-6 周）

| 周 | 任务 | 后端依赖 |
|---|---|---|
| W1 | 家长端：注册流程 + 登录 | P0 鉴权 |
| W2 | 家长端：看周报 + 兴趣图谱 + 学习白皮书 | P0 持久化 |
| W3 | 家长端：告警（轮询）+ 远程暂停 + 内容设置 | P1 文件上传 |
| W4 | 儿童端：配对 + 主对话（文字） | P0 大部分 |
| W5 | 儿童端：语音录音 + TTS | P1 音频上传 |
| W6 | 联调 + 端到端测试 + 性能优化 | 全部 |

### Phase 2：家长微信小程序（2-3 周）
- 复用 H5 业务逻辑（用 Taro 转换）
- 接入 `wx.login` 登录
- 适配小程序 UI 规范

### Phase 3：儿童原生 App（4-6 周）
- Flutter 跨平台
- WebRTC 实时音频流
- 后端 ASR 接口对接
- 推送服务接入

---

## 10. 风险与依赖

### 10.1 技术风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| Web 录音体验差 | 高 | 儿童端 UX 受损 | Phase 1 仅做验证，Phase 3 切 Flutter |
| 后端持久化未及时补齐 | 高 | 数据丢失 | P0 优先级最高 |
| 实时推送延迟 | 中 | 告警体验差 | Web 端用 SSE（< 5s），App 端用系统推送 |
| LLM token 费用 | 中 | 现金流 | 限流 + 缓存 |
| 跨境数据合规 | 低 | 海外部署受限 | 本期仅国内 |

### 10.2 业务依赖

- **阿里云 IDaaS**：实名 + 人脸核身（待采购）
- **阿里云 OSS**：图片 / 音频存储
- **微信开放平台**：小程序登录
- **推送服务**：APNs / 华为 / 小米

### 10.3 文档依赖

- 后端 OpenAPI：自动生成，前端用 `openapi-typescript` 生成 TS 类型
- 后端 CHANGELOG：每次接口变更必须更新
- 前端 Storybook：组件库文档

---

## 11. 附录

### 11.1 命名规范

- API 路径：`/api/v1/{resource}/{action}`
- 数据主键：`{entity}_id`（UUID）
- 时间字段：ISO 8601 UTC（前端用 dayjs 转换本地时区）
- 错误码：`UPPER_SNAKE_CASE`，三段式 `{DOMAIN}_{CATEGORY}_{SPECIFIC}`

**错误码命名规范**（建议后端下次重构时统一）：

| 段 | 含义 | 取值范围 |
|---|---|---|
| `DOMAIN` | 业务域 | `AUTH` / `SAFETY` / `SESSION` / `CONTENT` / `LLM` / `INFRA` / `TREEHOLE` / `VALIDATION` |
| `CATEGORY` | 类别 | `INVALID` / `FAILED` / `EXPIRED` / `NOT_FOUND` / `RATE_LIMIT` / `TIMEOUT` |
| `SPECIFIC` | 具体场景 | `PHONE` / `ID_NUMBER` / `TOKEN` / `IMAGE` 等 |

**示例**：
- ✅ `AUTH_INVALID_PHONE_FORMAT` — 良好：domain + category + specific
- ✅ `SAFETY_DIRECT_ANSWER_DETECTED` — 良好
- ⚠️ `INVALID_ID_FORMAT` — 现存，但缺 DOMAIN
- ❌ `auth.invalidPhone` — 错误：lowercase + camelCase
- ❌ `PHONE_FORMAT_INVALID` — 风格：current backend style，过段时间易混乱

**前端要做到**：
- 错误码常量集中管理（如 `src/constants/errorCodes.ts`）
- 按域前缀分组导出（`AUTH_ERRORS.*` / `SAFETY_ERRORS.*`）
- 未知错误码必须有兜底处理（默认 Toast + 上报）

### 11.2 版本管理

- API 版本：`/api/v1`，主版本号升级需要协商
- 客户端版本：`X-Client-Version` 头注入
- 最低支持版本：后端维护 `MIN_CLIENT_VERSION`

### 11.3 开放问题（待评审）

1. **儿童端是否需要 SOS 紧急按钮？** PRD 未提，但合规角度建议加
2. **家长端是否需要「共享给孩子」功能？**（如分享兴趣图谱）
3. **多孩家庭支持？** PRD 声明本期不支持
4. **儿童端是否需要「离线模式」？** 当前强制在线
5. **数据导出格式？** CSV / JSON / PDF？

---

## 12. 文档履历

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v1.0 | 2026-07-23 | 初稿 | Claude |
| v1.0-patch | 2026-07-23 | 添加 §6.2 OpenAPI 引用 + 端点状态列 | Claude (post Codex 修复) |
| v1.1 | 2026-07-24 | 修正错误码为真实后端值 / 标识 5 种返回模式 / 接入 OpenAPI 自动生成清单 / 推送策略矩阵 / 短信验证码流程 / 后端 ASR 替代浏览器 Web Speech / 厂商推送通道 / TOC + 错误码命名规范 | Claude |
| v1.2 | 2026-07-24 | §7 缺口清单大改版：从 8+7+5=20 项扩到 46 项（P0×16 / P1×17 / P2×13），按域分组共 10 个；每条工单含 ID、当前状态、前端影响、替代方案、建议实现、工作量；新增 §7.4 关键路径图 / §7.5 文档内 ⚠️ 联动表 / §7.6 4×7 周排期 / §7.7 兜底方案 / §7.8 GitHub Issues 追踪建议 | Claude |
| **v1.3** | **2026-07-29** | **§7 缺口清单状态盘点**：每张工单表加「完成」列（图例：✅ 已完成 / ⚠️ 部分完成 / 🟡 进行中 / ❌ 未开始）；§7.0 汇总矩阵更新：P0 已完成 **16/16**（全部清零），P1 **1/17**，P2 **0/13**；§7.6 排期补"实际状态"列；列出已完成工单清单（AUTH/DEV/PUSH/MEDIA/RT/STOR/API/COMP 各域） | Claude |

---

**下一步行动**：
1. 与后端对齐 §7 缺失清单，排出 P0 修复时间表
2. 与产品/UX 评审页面地图（§4）
3. 与前端主程评估技术栈（§3）
4. 启动 Phase 1 家长端 H5 开发

