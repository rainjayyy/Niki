# Niki — AI Companion & Growth Assistant for Children

Niki is an AI companion for children aged 3-12, built with LangChain. It adapts to each child's emotional state, cognitive level, and interests — acting as a patient tutor, knowledgeable friend, and empathetic listener.

> **一句话：一个能感知情绪、适配认知、守护安全的"AI伙伴"。**

## Architecture

Niki follows a five-layer architecture:

| Layer | Responsibility |
|-------|---------------|
| **Terminal Interaction** | WebRTC voice, Whisper ASR, Edge TTS, image understanding, parent dashboard |
| **Access & Security** | Input safety gateway (Llama Guard), intent classifier, agent router, output safety gateway |
| **Intelligence Core** | Encyclopedia Agent (RAG), Tutoring Agent (guided discovery), Emotion Agent (empathetic response), Persona Injector, Memory Hooks |
| **Memory & Evolution** | Redis (short-term), ChromaDB (vectors), Neo4j (cognitive graph), Nightly compression pipeline, Emotion baseline engine |
| **Data Governance** | Graded data storage, encrypted treehole mode, parental consent, data lifecycle management |

See [PRD-v2.0.md](PRD-v2.0.md) for the full product requirements.

## Tech Stack

- **Framework**: FastAPI + LangChain
- **Models**: DeepSeek-V4 (deep reasoning), doubao (lightweight tasks)
- **Databases**: PostgreSQL, Redis, ChromaDB, Neo4j
- **Voice**: Whisper.cpp (ASR), Edge TTS (synthesis)
- **Infrastructure**: Docker, GitHub Actions

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose

### 1. Clone & Install

```bash
git clone <repo-url>
cd Niki
make install
```

### 2. Start Infrastructure Services

```bash
make docker-up
```

This starts Redis, ChromaDB, Neo4j, and PostgreSQL locally.

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API keys (DeepSeek-V4, doubao)
```

### 4. Run the Server

```bash
make dev
```

The API is now running at http://localhost:8000.

- **Health check**: http://localhost:8000/api/v1/health
- **API docs** (debug mode): http://localhost:8000/docs

### 5. Run Tests

```bash
make test        # Unit tests only (fast)
make test-all    # Include integration tests
```

## Project Structure

```
Niki/
├── niki/                    # Application package
│   ├── main.py              # FastAPI entry point
│   ├── config/              # Settings & logging
│   ├── api/                 # Route handlers
│   ├── schemas/             # Pydantic models
│   ├── agents/              # Agent base classes
│   ├── infrastructure/      # DB clients & LLM clients
│   │   └── llm/             # DeepSeek & doubao clients
│   ├── middleware/           # CORS, error handlers
│   └── utils/               # Exceptions, helpers
├── tests/                   # Test suite
├── docker/                  # Docker Compose & Dockerfiles
├── .github/workflows/       # CI pipeline
├── pyproject.toml           # Project metadata & deps
├── Makefile                 # Common commands
└── PRD-v2.0.md              # Product requirements
```

## Development Commands

| Command | Description |
|---------|-------------|
| `make dev` | Start dev server with hot-reload |
| `make test` | Run unit tests |
| `make test-all` | Run all tests including integration |
| `make lint` | Lint and format check |
| `make format` | Auto-format code |
| `make typecheck` | Run mypy type checking |
| `make check` | Run all checks (lint + typecheck + test) |
| `make docker-up` | Start infrastructure services |
| `make docker-down` | Stop infrastructure services |
| `make clean` | Remove build artifacts |

## License

Proprietary — All rights reserved.
