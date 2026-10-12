# Niki — 面向 3-12 岁儿童的多 Agent AI 伙伴

> **一个能感知情绪、适配认知、守护安全的"AI 伙伴"**。
> 自研编排框架 · 多 Provider LLM 工厂 · 严格安全护栏 · LLM-as-Judge 评估体系

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](#)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#)

---

## 1. 项目定位

Niki 是一个面向 3-12 岁儿童的 AI 陪伴系统，**不是**通用聊天机器人。设计上的 3 个根本约束贯穿全代码：

1. **儿童安全是 P0** —— 任何 LLM 输出都必须经过 `OutputSafetyGateway`；自伤 / 暴力 / 色情 / 毒 / 脏话 5 类输入直接拦截；4 级分类（ALLOW / DOWNGRADE / ESCALATE / BLOCK）。
2. **家长必须知情可控** —— 实名 + 人脸 + 知情同意三件套注册、30 天删除冷静期、PDF 删除回执、横向越权 (lateral-auth) 模式、PII 仅以 sha256 入库、JWT 三档 scope + TOTP 2FA。
3. **效果可评测可回归** —— LLM-as-Judge + 4 项算法去偏 + 19 金 + 20 人工标注 + 3 层可独立运行评估架构。

> 这些约束让本项目区别于"调 LangChain + 接 GPT-4 做个聊天 demo"——它是真在生产路径上跑过合规审查的工程。

---

## 2. 核心数字（截至 2026-10）

| 指标 | 数量 | 说明 |
|---|---|---|
| FastAPI 路由文件 | 35+ | 按 vertical slice 组织 |
| HTTP 端点 | **176** | 103 POST + 67 GET + 4 DELETE + 2 PATCH |
| 子模块（package） | 22 | agents / routing / safety / memory / auth / ... |
| Python 源文件 | ~110 | 排除 `__pycache__` / `__init__.py` |
| ORM 模型 | 17 | `db/models/` |
| Repository | 13 | in-memory + Postgres 双实现 |
| LLM 工具选择 (ReAct) | 4-round (KB only) | `agent_tool_use_enabled` flag，默认关闭；`agent_tool_loop_max_rounds` 默认 4 |
| SSE 流式 ReAct | structured events | `agent_stream_use_agent_pipeline` flag，默认关闭；开 → `/chat/stream` 走 `ChatOrchestrator.stream_chat`，emit `phase`/`chunk`/`tool_call`/`tool_result`/`final` 事件 |
| Vertical Slice | 6 | safety → routing → encyclopedia/tutoring/emotion → orchestrator |
| 评估金标准用例 | 19 | 覆盖 3 个核心维度：回复质量 / 硬约束达标 / 安全性 |
| 必过安全用例 | 覆盖 5 类输入 + 输出改写 + 直答检测 | 任何 fail 即阻塞 CI |
| 人工标注校准集 | 20 | judge 与人评一致性（Cohen-kappa ≥ 0.7） |
| LLM-judge 算法去偏 | 4 | 见 [`niki/eval/README.md`](niki/eval/README.md) |
| Prometheus 指标 | 13 | 自建 `CollectorRegistry` 隔离测试 |

> README is verified by make typecheck && make test (see [docs/ci.md](docs/ci.md) for the exact command set).

---

## 3. 快速开始

### 3.1 前置

- Python 3.11+（项目用 `StrEnum`，3.9 不兼容）
- Docker + Docker Compose（启动 Redis / Chroma / Neo4j / PostgreSQL）
- Conda 或 venv

### 3.2 启动

```bash
git clone <this-repo> && cd Niki
conda create -n niki python=3.12 -y && conda activate niki
pip install -e ".[dev]"
cp .env.example .env
# 编辑 .env，填入 LLM 凭证（至少 MINIMAX_API_KEY，或保留空走 stub）

make docker-up     # 启动基础设施
make dev           # → API: http://localhost:8000
                   # → 文档: http://localhost:8000/docs
```

### 3.3 验证

```bash
curl http://localhost:8000/api/v1/health
# → {"status": "healthy", "components": {...}}

curl -X POST http://localhost:8000/api/v1/chat/full \
  -H "Content-Type: application/json" \
  -d '{"text":"我喜欢恐龙","child_age":7,"session_id":"demo"}'
```

### 3.4 运行测试

```bash
pytest tests/ -q                              # 全部
pytest tests/safety/ -q                       # 某个 slice
pytest tests/knowledge/ tests/memory/ -q      # 多个 slice
```

---

## 4. 架构（5 层 + 7 阶段编排）

```
┌────────────────────────────────────────────────────────────────────┐
│ 1. 接入层   浏览器 / App / WebRTC   →   ASR / TTS / OCR              │
├────────────────────────────────────────────────────────────────────┤
│ 2. 鉴权与合规  JWT (3 档 scope) · TOTP 2FA · lateral-auth            │
│               实名 / 人脸 / 同意书 / 30 天删除 / PII sha256          │
├────────────────────────────────────────────────────────────────────┤
│ 3. 安全网关  InputSafetyGateway  →  OutputSafetyGateway             │
│              5 类敏感词 / 主题黑名单 / 自伤升级 / 恐吓词改写 /       │
│              推理模型 think-block 防御性剥离                        │
├────────────────────────────────────────────────────────────────────┤
│ 4. 编排与 Agent  IntentClassifier → AgentRouter → 并行 dispatch →   │
│                  FusionOrchestrator → OutputGateway                 │
│                  (情感优先 + Socratic + 4 轮安全阀)                  │
├────────────────────────────────────────────────────────────────────┤
│ 5. 记忆与知识  Redis 短期 (10 轮/1d)  →  Chroma 长期               │
│                →  Neo4j 认知图谱  →  知识库 (3 级 L1/L2/L3)         │
└────────────────────────────────────────────────────────────────────┘
```

> 编排器（[`niki/agents/orchestrator.py`](niki/agents/orchestrator.py)）的 7 阶段：SAFETY_INPUT → CLASSIFY → ROUTE → DISPATCH → FUSE → SAFETY_OUTPUT → COMPLETE。每条 child message 都跑这 7 阶段，全部可观测（`stages_completed` 字段）。

---

## 5. 多 Agent 编排（核心）

`ChatOrchestrator`（`niki/agents/orchestrator.py`）是项目的灵魂——它把"垂直切片"组装成端到端管道：

| Agent | 设计哲学 | 关键约束 |
|---|---|---|
| **EncyclopediaAgent** | RAG-first，知识库三档 L1/L2/L3 | `FactChecker` 解析失败 **fail-closed**（不答比答错好）|
| **TutoringAgent** | Socratic 风格，第 1-3 轮绝不直答；4 轮安全阀才给答案 | 4 轮引导策略 + 情绪优先 + **零外部工具**（PRD #54）|
| **EmotionAgent** | 7 天滑动基线 + z-score 异常检测 | 异常 → 触发树洞 / 推送家长 / 周报标注 |
| **FusionOrchestrator** | 多 agent 并行 → 文本融合 | 路由层情感永远 first（PRD：先共情再解题）|

**每个 agent 都共享**：

- **3 档人设**：lively（热情）/ gentle（温柔）/ rigorous（严谨），由 `PersonaInjector` 注入 system prompt
- **3 档年龄护栏**：3-5 岁 ≤10 字/句 ≤5 句；6-8 岁 ≤15 字/句 ≤8 句；9-12 岁 ≤20 字/句 ≤12 句
- **think-block 防御性剥离**：专门应对 MiniMax-M3 / DeepSeek-R1 这类 reasoning 模型外漏思考过程

**不会调用 LLM 的场景**（这是真实工程取舍，不是 bug）：

- Tutoring 1-3 轮：直接用静态 Socratic 模板（LLM 输出漂移破坏金标准）
- 情感安抚：模板化回应对儿童一致性更好

---

## 6. LLM 工厂与流式推理

`niki/infrastructure/llm/factory.py` 是单点切换入口：

```
minimax (Minimax-M3)   ← primary
   ↓ (unavailable)
deepseek (DeepSeek-V4)
   ↓ (unavailable)
doubao (doubao-lite)   ← 轻量分类 / 检查
   ↓ (no key)
_StubLLM               ← 确定性 fallback
```

**3 个工程细节**值得面试展开：

1. **凭证占位符检测**（`_PLACEHOLDER_TOKENS`）：`your-` / `changeme` / `xxx` 等占位符字符串会被识别成"假凭证"，强制走 stub。这是被真实事故驱动加的——曾有 staging 带着 `your-key-here` 跑了一周没人发现。
2. **降级而非中断**：prefer 失败 → 试下一个；全失败 → stub + 结构化日志，绝不让 5xx 阻塞业务。
3. **Anthropic 兼容 SSE 流式客户端**（`MinimaxStreamingProvider`）：自研实现 token 级增量返回，配合 `/api/v1/chat/stream` 端点给前端 EventSource 用。

---

## 7. 记忆系统（3 层）

```
ShortTerm  (Redis, 10 轮滑动窗口, 1d TTL)
   │  night pipeline 异步迁移
   ▼
LongTerm   (Postgres + Chroma 向量召回, 内存 fallback)
   │  aligner 处理跨层冲突
   ▼
Graph      (Neo4j 认知图谱：兴趣 / 情绪 / 关系)
   │  hook 实时抽取
   ▼
  编排层下次对话可调用
```

**关键工程取舍**：

- `short_term.clear_all()` 在账户删除时会**真删 Redis**（`SCAN` + `DELETE`），不只是清内存——这是 PIPL 合规要求。
- Chroma 不可用时退化到**关键词匹配**而非抛错——儿童产品不允许"5xx 让孩子干等"。
- Neo4j 不可用时图谱查询降级到 Postgres 关联查询。

---

## 8. 安全与合规（设计哲学）

### 8.1 输入侧 4 级分类

```
self_harm  →  ESCALATE   (立即推家长 + 情感回应)
sexual/drugs/violence/profanity  →  BLOCK  (硬拒)
topic_blacklist  →  DOWNGRADE_TO_EMOTION  (降级到情感 agent)
干净  →  ALLOW
```

### 8.2 输出侧 5 重护栏

1. **恐吓词改写**：death → 离开 / monster → 淘气的小家伙 / ...
2. **年龄护栏**：3 档硬字数 / 硬句数
3. **直答检测正则**：2026-10 打过补丁，捕获"现在有 N 个" / "= N" 这类辅导模式泄漏
4. **隐私反向问询阻断**："你家住哪" 类直接拦截
5. **think-block 剥离**（防御性）：适配 reasoning 模型

### 8.3 合规工程（PIPL / GDPR）

- 实名 + 人脸 + 知情同意（双版本：家长版法律文本 / 儿童版图画版）
- 30 天删除冷静期 + PDF 删除回执
- **lateral-auth 模式**：`parent.id == path_parent_id`，违规 403 `PARENT_MISMATCH`（部分端点用 404 避免 id oracle）
- **PII 永不落库明文**：`parent.id = sha256(name:id_number)[:16]`
- JWT 三档 scope：`partial`（2fa 中） / `full` / `full_reauth`（5 分钟升级令牌，敏感操作强校验）

---

## 9. 评估体系（[`niki/eval/README.md`](niki/eval/README.md)）

> 3 层可独立运行架构：通用 judge / Niki 金标准 / 集成测试

**LLM-as-Judge + 4 项算法去偏**（业内 paper 反复验证过的系统性问题）：

1. **长度归一化**：截断到 200 字前评分（杀 `longer = better` 偏置）
2. **位置偏置检测**：同输入跑两遍换位，分差 ≥ 2 取**低分**（保守）
3. **人评一致性校准**：Cohen-kappa ≥ 0.7 阈值，20 条人工标注集复测
4. **Judge 失效回退硬断言**：JSON malformed 时降级到 must_contain / must_not_contain 硬评分，避免 LLM 抖动掩盖真实安全 bug

**附加推荐**：跨模型 judging（主模型 Minimax，judge 用 doubao）——避免自偏好，是部署层面的选择。

**安全必过套件**任何一条 fail 即阻塞 CI；**金标准 19 条**通过 LLM-judge 软评。

```bash
python scripts/run_eval.py --limit 10 --stub     # 烟囱测试，~1s
python scripts/run_eval.py --child-age 7         # 真实评测
pytest tests/eval/test_safety_must_pass.py -v   # CI 红线
```

---

## 10. 可观测性

- **13 个 Prometheus 指标** + 自建 `CollectorRegistry`（避免污染 `prometheus_client.REGISTRY`）
- **structlog** 结构化日志，全链路 `request_id` 串联
- **双轨审计**：`auth_audit_logs`（横向越权） + `audit_logs`（业务，JSONB `event_metadata`）
- **HeartbeatService**：6 个后台 task（4 个客户端健康 + 配对码清理 + 手机变更扫描）
- **SSE 实时告警**：`/api/v1/alerts/stream` 给父端 H5 推高危事件

---

## 11. 技术栈

| 层 | 选型 | 原因 |
|---|---|---|
| Web 框架 | **FastAPI** | 异步 + OpenAPI + 生态成熟 |
| Agent 框架 | **自研** (`BaseAgent` / `BaseLLMClient`) | 当时 LangChain v0.x 还不稳定；项目要可控可调试 |
| 主用 LLM | **Minimax-M3** | deep reasoning + 儿童语气可控 |
| Fallback LLM | **DeepSeek-V4** / **doubao-lite** | 多 provider 故障切换 |
| 关系库 | **PostgreSQL + SQLAlchemy async** | 17 张表 / 13 repo，事务强 |
| 缓存 | **Redis** | 短期记忆 / 限流 / 配对码 / token 黑名单 |
| 向量库 | **ChromaDB** | 轻量、dev 友好、生产可换 Qdrant |
| 图库 | **Neo4j** | 兴趣 / 记忆关系图 |
| 对象存储 | **本地 / S3 兼容** (OSS / COS / AWS) | `boto3` 抽象 |
| 鉴权 | **JWT (PyJWT)** + **TOTP (pyotp)** | 行业标准 |
| 推送 | **Web Push / VAPID (RFC 8292)** | 无需服务器 |
| 日志 | **structlog** | 结构化 |
| 指标 | **Prometheus client** | 13 个核心指标 |
| 限流 | **slowapi** | Redis 后端可切换 |
| Lint | **ruff** | 快 |
| Test | **pytest** (asyncio mode) | 2090 passed; coverage gaps tracked in [docs/coverage.md](docs/coverage.md) |

---

## 12. 目录结构（精简版）

```
Niki/
├── niki/
│   ├── main.py                  # FastAPI 入口 + lifespan
│   ├── config/                  # settings + structlog
│   ├── infrastructure/          # postgres/redis/chroma/neo4j/llm
│   ├── agents/                  # base + 3 agent + orchestrator
│   ├── routing/                 # classifier + router + fusion + persona
│   ├── safety/                  # input/output gateway + dictionaries
│   ├── knowledge/               # KB (3 级) + fact_checker
│   ├── memory/                  # short_term / long_term / graph / pipeline
│   ├── auth/                    # 13 个鉴权文件
│   ├── dashboard/               # 周报 / 兴趣 / 学习白皮书
│   ├── treehole/                # 树洞（加密 + 7 天清除）
│   ├── session/                 # 时长限制 / 远程暂停
│   ├── tools/                   # 工具权限矩阵
│   ├── realtime/                # SSE 告警 broker（单进程）
│   ├── resilience/              # 断路器 / 降级
│   ├── compliance/              # 删除回执 PDF
│   ├── db/                      # 17 models + 13 repos
│   ├── api/                     # 35+ 路由文件
│   ├── middleware/              # 5 个中间件
│   ├── schemas/                 # Pydantic
│   └── eval/                    # LLM-as-Judge 评估层 ★
├── tests/                       # 136 个测试文件
├── docker/                      # docker-compose
├── docs/                        # 设计文档 / BE-* 工单
├── PRD-v2.0.md                  # 产品需求
├── pyproject.toml
└── Makefile
```

---

## 14. 文档索引

| 文档 | 内容 |
|---|---|
| [`PRD-v2.0.md`](PRD-v2.0.md) | 产品需求 v2.0（75 条 user story）|
| [`niki/eval/README.md`](niki/eval/README.md) | 评估层架构 + 4 项算法去偏详解 |
| [`EVAL_REPORT.md`](EVAL_REPORT.md) | 历次评估结果 |
| `docs/be-issues/BE-*.md` | 50+ 个后端工单的设计 rationale |
| `docs/` | 架构决策记录 (ADR) |

---

## 15. 开发命令

```bash
make install    # 装依赖
make dev        # 启动 dev server（hot reload）
make test       # 跑全部测试
make test-cov   # 覆盖率
make lint       # ruff check
make format     # ruff format
make typecheck  # mypy strict (see [docs/mypy_status.md](docs/mypy_status.md) for known remaining items)
make docker-up  # 启动基础设施
make docker-down
make ts-sdk     # 重新生成 TypeScript SDK（见下）
```

### 15.1 TypeScript SDK 自动生成（BE-API-007）

前端不手写 `fetch()` —— `scripts/gen_ts_sdk.py` 读 FastAPI 内存中的 `app.openapi()`，吐出一个零依赖的 TypeScript 客户端到 `frontend/src/api/`。每次后端路由变更跑一次 `make ts-sdk-check` 验证 SDK 没漂移。

```bash
make ts-sdk         # 生成到 frontend/src/api/
make ts-sdk-check   # CI 检查 SDK 是否过期
```

---

## 16. 许可

Proprietary — All rights reserved.
