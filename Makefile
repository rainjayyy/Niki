# Niki — Makefile
# Quick reference for common development commands.

.PHONY: help install dev test lint format typecheck clean docker-up docker-down ts-sdk ts-sdk-check

# Default target
help:
	@echo "Niki — Development Commands"
	@echo "==========================="
	@echo ""
	@echo "Setup:"
	@echo "  make install      Install Python dependencies"
	@echo "  make docker-up    Start infrastructure services (Redis, Chroma, Neo4j, PG)"
	@echo "  make docker-down  Stop infrastructure services"
	@echo ""
	@echo "Development:"
	@echo "  make dev          Start FastAPI dev server with hot-reload"
	@echo "  make test         Run the test suite"
	@echo "  make lint         Run ruff linter and formatter check"
	@echo "  make format       Auto-format code with ruff"
	@echo "  make typecheck    Run mypy type checking"
	@echo "  make check        Run all checks (lint + typecheck + test)"
	@echo ""
	@echo "Codegen:"
	@echo "  make ts-sdk        Generate the TypeScript SDK into frontend/src/api"
	@echo "  make ts-sdk-check  Fail if the checked-in SDK is stale"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        Remove build artifacts and cache files"

# ---- Setup ----

install:
	pip install --upgrade pip
	pip install -e ".[dev]"

docker-up:
	docker compose -f docker/docker-compose.yml up -d
	@echo "Waiting for services to be ready..."
	@sleep 5
	@docker compose -f docker/docker-compose.yml ps

docker-down:
	docker compose -f docker/docker-compose.yml down -v

# ---- Development ----

dev:
	cp -n .env.example .env 2>/dev/null || true
	uvicorn niki.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest tests/ -v --tb=short -m "not integration"

test-all:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ -v --tb=short --cov=niki --cov-report=html --cov-report=term -m "not integration"

lint:
	ruff check niki/ tests/
	ruff format --check niki/ tests/

format:
	ruff check --fix niki/ tests/
	ruff format niki/ tests/

typecheck:
	mypy niki/ --show-error-codes

# ---- Codegen (BE-API-007) ----

# Regenerate the TypeScript client from the FastAPI OpenAPI schema.
# Zero dependencies: stdlib + the already-installed FastAPI, no npm.
ts-sdk:
	python -m scripts.gen_ts_sdk

# CI guard: exits 1 when frontend/src/api/ no longer matches the routes.
ts-sdk-check:
	python -m scripts.gen_ts_sdk --check

# ---- Full check ----

check: lint typecheck test
	@echo ""
	@echo "✅ All checks passed!"

# ---- Cleanup ----

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ *.egg-info/
	@echo "Cleaned."
