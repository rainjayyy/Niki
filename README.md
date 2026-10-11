# Niki — Multi-Agent AI Companion for Children aged 3-12

> **An AI companion that senses emotions, adapts cognition, and safeguards safety.**
>
> Self-developed orchestration framework · Multi-provider LLM factory · Strict safety guardrails · LLM-as-Judge evaluation system

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](#)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#)
[![Eval pass rate](https://img.shields.io/badge/eval-89.5%25_(17%2F19)-yellow)](#)

[简体中文 (README_zh.md)](./README_zh.md)

---

## 1. What is Niki?

Niki is an AI companion system for ages 3-12. It is **not** a generic chatbot.
Three foundational constraints run through the entire codebase:

1. **Child safety is P0** — every LLM output passes through the OutputSafetyGateway. The 5 sensitive categories (self-harm / violence / sexual / drugs / profanity) are intercepted at the input layer with 4-tier classification (ALLOW / DOWNGRADE / ESCALATE / BLOCK).
2. **Parents must be informed and in control** — real-name + face + parental-consent triple registration, 30-day deletion cooling window, PDF deletion receipt, lateral-auth (parent_id == path_parent_id) pattern, PII stored only as sha256, JWT 3-tier scope (partial / full / full_reauth) + TOTP 2FA.
3. **Quality is measurable and regression-proof** — LLM-as-Judge + 4 algorithmic de-biasing methods + 19 golden cases + 20 human-labeled calibration + 3 independently runnable evaluation layers.

> These constraints are what distinguish Niki from "wire up LangChain + connect GPT-4 to build a chat demo" — Niki is engineering that has been through real production-path compliance review.

---

## 2. Headline numbers (as of 2026-10)

| Metric | Count | Notes |
|---|---|---|
| FastAPI router files | 35+ | organized by vertical slice |
| HTTP endpoints | **176** | 103 POST + 67 GET + 4 DELETE + 2 PATCH |
| Sub-modules (packages) | 22 | agents / routing / safety / memory / auth / ... |
| Python source files | ~110 | excluding __pycache__ / __init__.py |
| ORM models | 17 | db/models/ |
| Repositories | 13 | in-memory + Postgres dual implementation |
| LLM tool selection (ReAct) | 4-round (KB only) | agent_tool_use_enabled flag, default off |
| SSE streaming ReAct | structured events | agent_stream_use_agent_pipeline flag, default off |
| Vertical slices | 6 | safety → routing → encyclopedia/tutoring/emotion → orchestrator |
| Evaluation golden cases | 19 | covers 3 core dimensions |
| Required-pass safety cases | covers 5 input categories + output rewriting + direct-answer detection | any fail blocks CI |
| Human-labeled calibration set | 20 | judge-human agreement (Cohen-kappa ~ 0.8) |
| LLM-judge algorithmic de-bias | 4 | see niki/eval/README.md |
| Prometheus metrics | 13 | self-built CollectorRegistry isolated from tests |

---

## 3. Quick start

### 3.1 Prerequisites

- Python 3.11+ (project uses enum.StrEnum, which is unavailable on 3.9)
- Docker + Docker Compose (starts Redis / Chroma / Neo4j / PostgreSQL)
- conda or venv

### 3.2 Boot

```bash
git clone <this-repo> && cd Niki
conda create -n niki python=3.12 -y && conda activate niki
pip install -e ".[dev]"
cp .env.example .env
# edit .env, fill in LLM credentials (at least MINIMAX_API_KEY, or leave empty to use stub)

make docker-up     # start infrastructure
make dev           # API: http://localhost:8000
                  # docs: http://localhost:8000/docs
```

### 3.3 Verify

```bash
curl http://localhost:8000/api/v1/health
# -> {"status": "healthy", "components": {...}}
```

---

## 4. Architecture (5 layers + 7-stage orchestration)

```
┌────────────────────────────────────────────────────────────────────┐
│ 1. Access layer   Browser / App / WebRTC   →   ASR / TTS / OCR              │
├────────────────────────────────────────────────────────────────────┤
│ 2. Auth & compliance  JWT (3-tier scope) · TOTP 2FA · lateral-auth      │
│                    Real-name / face / consent / 30-day deletion /      │
│                    PII sha256 storage                                  │
├────────────────────────────────────────────────────────────────────┤
│ 3. Safety gateway  InputSafetyGateway  →  OutputSafetyGateway         │
│                    4-tier (ALLOW / DOWNGRADE / ESCALATE / BLOCK)        │
│                    Strips <think> blocks from reasoning models         │
├────────────────────────────────────────────────────────────────────┤
│ 4. LLM layer       Provider factory (Minimax / DeepSeek / Doubao)      │
│                    Real client + Fallback for dev / tests              │
├────────────────────────────────────────────────────────────────────┤
│ 5. Agent layer     EncyclopediaAgent · TutoringAgent · EmotionAgent   │
│                    ChatOrchestrator (intent classification + routing) │
│                    Privacy tier classifier (4-tier)                   │
└────────────────────────────────────────────────────────────────────┘
```

