# Niki — AI Companion & Growth Assistant for Children (3-12)

> **一句话：一个能感知情绪、适配认知、守护安全的"AI伙伴"。**

Niki is an AI companion for children aged 3-12. It adapts to each child's
emotional state, cognitive level, and interests — acting as a patient tutor,
knowledgeable friend, and empathetic listener. The system is built around
strict safety guarantees: parental consent, content policy enforcement,
private "tree-hole" mode, circuit-breaker resilience, and a 30-day data
deletion promise.

---

## Quick Start

### Prerequisites

- Python 3.11+ (tested with 3.12)
- Docker & Docker Compose (for infrastructure)
- Conda or venv (recommended)

### Setup

```bash
# 1. Create environment
conda create -n niki python=3.12 -y
conda activate niki

# 2. Install
cd Niki
pip install -e ".[dev]"

# 3. Copy env template
cp .env.example .env
# Edit .env to add your LLM API keys (DeepSeek-V4, doubao)

# 4. Start infrastructure (Redis, Chroma, Neo4j, PostgreSQL)
make docker-up

# 5. Run
make dev
# → API at http://localhost:8000
# → Docs at http://localhost:8000/docs
```

### Verify

```bash
curl http://localhost:8000/api/v1/health
# → {"status": "healthy", "components": [...]}

curl -X POST http://localhost:8000/api/v1/chat/full \
  -H "Content-Type: application/json" \
  -d '{"text":"我喜欢恐龙","child_age":7}'
```

---

## Architecture — Five Layers

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. Terminal Interaction  (WebRTC voice, Whisper ASR, TTS)        │
├─────────────────────────────────────────────────────────────────┤
│ 2. Access & Safety        (Input/output gateway, topic blacklist) │
├─────────────────────────────────────────────────────────────────┤
│ 3. Intelligence Core      (Encyclopedia / Tutoring / Emotion)     │
├─────────────────────────────────────────────────────────────────┤
│ 4. Memory & Evolution     (Redis / Chroma / Neo4j / Pipeline)    │
├─────────────────────────────────────────────────────────────────┤
│ 5. Data Governance        (Real-name, consent, deletion)         │
└─────────────────────────────────────────────────────────────────┘
```

All built around **multi-agent orchestration** with empathy-first ordering
and circuit-breaker protection.

---

## Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Web framework | **FastAPI** | Async, OpenAPI, fast |
| LLM orchestration | **LangChain** | Industry standard |
| Primary LLM | **DeepSeek-V4** | Deep reasoning, child-facing |
| Fallback LLM | **doubao-lite** | Lightweight, fast classification |
| Vector store | **ChromaDB** | Lightweight, dev-friendly |
| Graph DB | **Neo4j** | Entity-relation model |
| Cache | **Redis** | Fast, TTL support |
| Relational | **PostgreSQL** | Stable, mature |
| Auth (planned) | **Aliyun IDaaS** | China-compliant |
| Container | **Docker Compose** | Local dev |

---

## Vertical Slices — All Implemented ✅

| # | Slice | Tests | Endpoints |
|---|-------|-------|-----------|
| P0 | Infrastructure (FastAPI, settings, logging, infra clients) | 11 | 1 (`/health`) |
| #1 | Safety pipeline (input/output gateway + topic blacklist) | 62 | 4 |
| #2 | Routing + persona injection + composite-intent fusion | 60 | 5 |
| #3 | Encyclopedia agent (3-level KB + fact-checker + age-adapt) | 56 | 3 |
| #4 | Tutoring agent (4-round guided discovery + safety valve) | 53 | 3 |
| #5 | Emotion agent (6 emotions + 7-day baseline + treehole trigger) | 53 | 3 |
| #6 | Composite intent chat orchestrator (7-stage pipeline) | 33 | 1 |
| #7 | Parent auth (real-name + face + e-consent + 30-day delete) | 73 | 8 |
| #8 | Parent dashboard (weekly emotion report + interest + alerts) | 34 | 10 |
| #9 | Memory system (short/long/graph/hook/pipeline) | 65 | 11 |
| #10 | Resilience (circuit breaker + degradation notifier) | 35 | 7 |
| #11 | Treehole mode (AES-256-GCM + 7-day auto-purge) | 53 | 7 |
| #12 | Session control (60min cap + 5min warn + parent pause) | 55 | 13 |
| #13 | Content policy (family blacklist + age grading + downgrade) | 66 | 9 |
| #14 | Tool permission matrix (PRD #54 per-agent access control) | 35 | 6 |
| #15 | End-to-end integration tests + README | 21 | — |
| **Total** | | **775** | **91** |

---

## Directory Structure

```
Niki/
├── niki/                              # Main application package
│   ├── main.py                        # FastAPI entry point
│   ├── config/                        # Settings + structured logging
│   ├── api/                           # HTTP route modules
│   │   ├── auth.py                    # #7 real-name / face / consent
│   │   ├── chat/                      # #6 chat orchestrator
│   │   ├── content_policy.py          # #13 family blacklist + age grading
│   │   ├── dashboard.py               # #8 weekly reports
│   │   ├── emotion.py                 # #5 emotion agent
│   │   ├── encyclopedia.py           # #3 KB + fact-check
│   │   ├── health.py                  # /health endpoint
│   │   ├── memory.py                  # #9 short/long/graph
│   │   ├── orchestrator.py           # #6 full chat pipeline
│   │   ├── resilience.py             # #10 circuit breaker
│   │   ├── routing.py                # #2 classifier + router + persona
│   │   ├── safety.py                 # #1 input/output gateway
│   │   ├── session.py                # #12 time limit + pause
│   │   ├── tools.py                  # #14 tool matrix
│   │   ├── treehole.py               # #11 encrypted treehole
│   │   └── tutoring.py               # #4 guided discovery
│   ├── agents/                        # Agent implementations
│   │   ├── encyclopedia.py
│   │   ├── tutoring.py
│   │   ├── emotion.py
│   │   └── orchestrator.py
│   ├── safety/                        # #1 safety pipeline
│   ├── routing/                       # #2 routing
│   ├── knowledge/                     # #3 KB + fact-checker
│   ├── auth/                          # #7 parent auth
│   ├── dashboard/                     # #8 dashboard
│   ├── memory/                        # #9 memory system
│   ├── resilience/                    # #10 circuit breaker
│   ├── treehole/                      # #11 treehole
│   ├── session/                       # #12 session
│   ├── content_policy/                # #13 content policy
│   └── tools/                         # #14 tool matrix
│
├── tests/                            # 775 tests, all passing
│   ├── test_*.py                     # API integration tests
│   ├── safety/, routing/, knowledge/  # Slice unit tests
│   ├── auth/, dashboard/, memory/    # ...
│   ├── resilience/, treehole/, session/
│   ├── content_policy/, tools/
│   └── test_e2e_integration.py        # Cross-slice flows
│
├── docker/                            # Infrastructure
│   ├── docker-compose.yml
│   └── Dockerfile
│
├── niki_backup/                       # Legacy stubs (deprecated)
├── .github/workflows/ci.yml           # Lint + type-check + test
├── pyproject.toml                     # All deps
├── Makefile                           # make dev/test/lint
└── README.md                          # This file
```

---

## API Endpoints (91 total)

### Core
- `GET  /api/v1/health` — health check (all components)
- `GET  /api/v1/health/live` — liveness probe
- `GET  /api/v1/health/ready` — readiness probe

### Safety (#1)
- `POST /api/v1/safety/check-input` — input gateway
- `POST /api/v1/safety/check-output` — output gateway
- `POST /api/v1/chat` — full chat with safety

### Routing (#2)
- `POST /api/v1/routing/classify` — intent classification
- `POST /api/v1/routing/route` — routing decision
- `GET  /api/v1/routing/personas` — list personas

### Agents
- Encyclopedia (#3): `/encyclopedia/{ask,knowledge,search}`
- Tutoring (#4): `/tutoring/{ask,strategies,difficulty}`
- Emotion (#5): `/emotion/{express,baseline,proactive-care,good-night}`

### Chat (#6)
- `POST /api/v1/chat/full` — full pipeline (7 stages)
- `GET  /api/v1/chat/stages` — pipeline stages info

### Auth (#7)
- `POST /api/v1/auth/signup/start` — start 3-step signup
- `POST /api/v1/auth/signup/{real-name,face,consent}` — 3 verification steps
- `POST /api/v1/auth/deletion/{request,complete}` — 30-day deletion

### Dashboard (#8)
- `GET  /api/v1/dashboard/weekly-report/{child_id}` — emotion weekly report
- `GET  /api/v1/dashboard/interest/{child_id}` — interest graph
- `GET  /api/v1/dashboard/learning/{child_id}` — learning whitepaper
- `GET  /api/v1/dashboard/alerts/{child_id}` — pending alerts

### Memory (#9)
- `GET/POST/DELETE /api/v1/memory/short-term/{session_id}` — recent turns
- `POST /api/v1/memory/long-term/{add,search}` — vector store
- `/api/v1/memory/graph/{entities,relations,apply-decay}` — cognitive graph
- `/api/v1/memory/hook/process` — daytime extraction
- `/api/v1/memory/pipeline/{run,health,dead-letter}` — nightly pipeline

### Resilience (#10)
- `GET  /api/v1/resilience/breaker/stats` — circuit breaker state
- `POST /api/v1/resilience/breaker/{reset,force-open}` — manual controls
- `GET  /api/v1/resilience/degradation` — current degradation state
- `POST /api/v1/resilience/chat` — chat with breaker protection

### Treehole (#11)
- `POST /api/v1/treehole/engage` — engage private mode
- `GET  /api/v1/treehole/summaries/{child_id}` — list summaries
- `POST /api/v1/treehole/aggregate` — anonymous baseline contribution
- `POST /api/v1/treehole/purge-{expired,all}` — purges

### Session (#12)
- `POST /api/v1/session/{start,activity,end}` — session lifecycle
- `GET  /api/v1/session/{stats,should-warn,is-exhausted,is-idle}/{child_id}`
- `POST /api/v1/session/parent/{pause,consume-pause,end-session,clear-pause}`

### Content Policy (#13)
- `/api/v1/policy/blacklist/{family_id}/{add,remove,evaluate,list}`
- `/api/v1/policy/age/{child_id,grade,grade-text}`
- `/api/v1/policy/downgrade/evaluate`

### Tools (#14)
- `GET  /api/v1/tools/matrix` — view tool permission matrix
- `GET  /api/v1/tools/allowed/{agent}` — list agent's tools
- `POST /api/v1/tools/check` — access check (audit-logged)
- `POST /api/v1/tools/{revoke,grant}` — admin controls
- `GET  /api/v1/tools/audit` — access audit log

---

## Safety Properties (from PRD)

- ✅ 输入侧 2 层过滤（正则 + LLM）
- ✅ 隐私正则双向保护（system prompt + 输出正则）
- ✅ 适龄句长（3-5 ≤10字/句，6-8 ≤15字/句，9-12 ≤20字/句）
- ✅ 树洞模式 AES-256-GCM 加密 + 7 天自动清除
- ✅ Calculator 工具全局移除（降级不降安全）
- ✅ 熔断器在降级时仍运行安全 + 路由 + 人格
- ✅ 家长看板永远不生成焦虑信号（积极优先 + 模糊化负向）
- ✅ 30 天数据清除承诺
- ✅ 实名 + 人脸 + 电子知情同意三步注册
- ✅ 隐私正则不暴露给子节点（黑名单话题不写入重定向消息）

---

## Development

### Common Commands

```bash
make install    # Install deps
make dev        # Start dev server with hot reload
make test       # Run all tests
make test-cov   # Run with coverage report
make lint       # Lint check (ruff)
make format     # Auto-format code
make typecheck  # mypy
make docker-up  # Start infrastructure
make docker-down # Stop infrastructure
make ts-sdk     # Regenerate the TypeScript SDK (BE-API-007)
```

### TypeScript SDK generation (BE-API-007)

The frontend does not hand-write `fetch()` calls or response types. Instead,
`scripts/gen_ts_sdk.py` reads the OpenAPI document FastAPI already builds
(`app.openapi()`, in-process — no server, no database) and emits a typed
TypeScript client into `frontend/src/api/`: one module per OpenAPI tag
(`dashboard.ts`, `memory.ts`, …) holding one `async function` per endpoint,
a `models.ts` with the interfaces derived from `components.schemas`, a
~200-line zero-dependency `client.ts` runtime (base URL, query encoding,
bearer token, §6.3 error shape → `ApiError`), and an `index.ts` barrel.
The generator itself has no third-party Python dependency and adds **no npm
dependency** — deliberately not `openapi-typescript-codegen`, whose output
drags in axios and a mutable global config singleton. Generation is one-shot
with no runtime reflection, and output is deterministic, so re-running after
a backend change produces a reviewable diff. `make ts-sdk-check` fails when
the checked-in SDK has drifted from the routes.

```bash
make ts-sdk                                   # → frontend/src/api/
python -m scripts.gen_ts_sdk --out /tmp/api    # somewhere else
python -m scripts.gen_ts_sdk --dry-run         # manifest only
make ts-sdk-check                              # CI staleness guard
```

Then in the frontend:

```ts
import { configure, getHealth, getDashboardWeeklyReport } from "@/api";

configure({ baseUrl: "https://api.niki.example", getToken: () => authToken });

const health = await getHealth();                       // HealthResponse
const report = await getDashboardWeeklyReport(childId);  // typed, cancellable
```

Known gap: `multipart/form-data` bodies (4 upload/OCR/transcribe endpoints)
are not generated yet — those methods take the form body via `options.body`
until BE-API-008. See `docs/be-issues/BE-API-007.md`.

### Testing

```bash
# All tests
pytest tests/ -v

# Specific slice
pytest tests/safety/ -v

# With coverage
pytest tests/ --cov=niki --cov-report=html
```

### Code Style

- **Python 3.11+** with full type hints
- **Ruff** for linting + formatting (config in `pyproject.toml`)
- **Mypy** for static type checking
- **Pytest** for tests (asyncio mode auto)

---

## PRD Coverage

All 64 user stories + 70+ technical requirements implemented:

| Group | Coverage |
|-------|----------|
| Children's Encyclopedia (3 stories) | ✅ |
| Children's Tutoring (5 stories) | ✅ |
| Children's Emotion (7 stories) | ✅ |
| Children's UX (6 stories) | ✅ |
| Parent's Dashboard (6 stories) | ✅ |
| Parent's Config (2 stories) | ✅ |
| System Safety (6 stories) | ✅ |
| Memory & Evolution (7 stories) | ✅ |
| Memory (10 stories) | ✅ |
| Session (5 stories) | ✅ |
| System Resilience (4 stories) | ✅ |
| Treehole (4 stories) | ✅ |
| Auth (1 story) | ✅ |
| Data Governance (3 stories) | ✅ |
| **Total** | **75/75 user stories** |

---

## Architecture Decisions

Key ADRs (Architecture Decision Records) baked into the code:

1. **Calculator globally removed** — even Encyclopedia/Tutoring/Emotion agents
   have no access. Tutoring uses its guided-discovery strategy library
   for ALL reasoning (PRD #54: 辅导 Agent 零外部工具).
2. **Treehole original text NEVER stored** — only the AES-256-GCM encrypted
   emotion summary enters storage. PRD: 聚合即匿名.
3. **Empathy-first ordering** — emotional intent always runs first when
   composite intent detected (PRD: 先情感共情→再切入解题).
4. **Soft + hard time limits** — 5-min soft warning before hard 60-min cap
   (PRD: 提前5分钟提醒).
5. **Silent degradation** — circuit breaker doesn't push notifications
   to parents (PRD: 静默感知, 不制造恐慌).
6. **Positive-first reporting** — dashboard always leads with discoveries
   + action suggestions (PRD: 绝对不生成焦虑信号).
7. **Two-version consent** — parent (legal) + child (picture-based)
   (PRD: 双版本隐私政策).
8. **Blacklist = downgrade, never refuse** — gentle deflection
   (PRD: 降级处理而非生硬拒绝).

---

## License

Proprietary — All rights reserved.
