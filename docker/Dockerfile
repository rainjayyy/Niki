# Niki — Multi-stage Docker build
#
# Stages:
#   1. builder  — installs dependencies
#   2. dev      — development with hot-reload
#   3. prod     — production-optimized image
#
# Build for development:
#   docker build -f docker/Dockerfile --target dev -t niki:dev .
#
# Build for production:
#   docker build -f docker/Dockerfile --target prod -t niki:latest .

# ============================================================
# Stage 1: Builder
# ============================================================
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ============================================================
# Stage 2: Development
# ============================================================
FROM builder AS dev

WORKDIR /app

# Install dev dependencies
RUN pip install --no-cache-dir -e ".[dev]"

# Copy application code (mounted as volume in dev for hot-reload)
COPY . .

EXPOSE 8000

# Start with hot-reload
CMD ["uvicorn", "niki.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# ============================================================
# Stage 3: Production
# ============================================================
FROM python:3.12-slim AS prod

WORKDIR /app

# Create non-root user
RUN groupadd -r niki && useradd -r -g niki niki

# Copy only installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Install curl for the HEALTHCHECK probe (python:3.12-slim doesn't ship it).
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
USER root

# Set ownership
RUN chown -R niki:niki /app

USER niki

EXPOSE 8000

# Health check — uses the lightweight liveness probe so we don't hit DB
# (the readiness probe in /health/ready does that).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health/live || exit 1

# Entrypoint waits for Postgres, runs alembic upgrade head, then execs uvicorn.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
