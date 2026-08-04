#!/bin/bash
# Niki 后端一键启动脚本
#
# 用法：
#   MINIMAX_API_KEY=sk-xxx ./start.sh          # 后台启动
#   MINIMAX_API_KEY=sk-xxx ./start.sh --foreground   # 前台启动（看日志）
#
# 前置：先跑 docker compose -f docker/docker-compose.yml up -d postgres redis chromadb neo4j

set -euo pipefail

# 基础设施（Docker 容器）环境变量
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://niki:nikidev123@localhost:5432/niki}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export CHROMA_HOST="${CHROMA_HOST:-localhost}"
export CHROMA_PORT="${CHROMA_PORT:-8001}"
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export NEO4J_USER="${NEO4J_USER:-neo4j}"
export NEO4J_PASSWORD="${NEO4J_PASSWORD:-nikidev123}"

# 应用环境变量
export API_HOST="${API_HOST:-0.0.0.0}"
export API_PORT="${API_PORT:-8000}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export DEBUG="${DEBUG:-true}"
export JWT_SECRET="${JWT_SECRET:-local-dev-secret-not-for-prod}"

# LLM（必填：MINIMAX_API_KEY 启动时传）
export MINIMAX_BASE_URL="${MINIMAX_BASE_URL:-https://api.minimaxi.com/anthropic}"
export MINIMAX_MODEL="${MINIMAX_MODEL:-MiniMax-M3}"
export PRIMARY_LLM_PROVIDER="${PRIMARY_LLM_PROVIDER:-minimax}"

# SMS（mock 模式，dev 会回显验证码）
export SMS_PROVIDER="${SMS_PROVIDER:-mock}"
export ECHO_SMS_CODE_IN_RESPONSE="${ECHO_SMS_CODE_IN_RESPONSE:-true}"

UVICORN="/c/Users/22265/.conda/envs/Nikienv/Scripts/uvicorn.exe"
ALEMBIC="/c/Users/22265/.conda/envs/Nikienv/Scripts/alembic.exe"

echo "[start] 运行数据库迁移..."
"$ALEMBIC" upgrade head

echo "[start] 启动 uvicorn on ${API_HOST}:${API_PORT} ..."
if [[ "${1:-}" == "--foreground" ]]; then
    exec "$UVICORN" niki.main:app --host "$API_HOST" --port "$API_PORT" --no-access-log
else
    nohup "$UVICORN" niki.main:app --host "$API_HOST" --port "$API_PORT" --no-access-log > /tmp/niki_uvicorn.log 2>&1 &
    echo "[start] 已后台启动 PID=$!，日志：/tmp/niki_uvicorn.log"
    echo "[start] Swagger UI: http://localhost:$API_PORT/docs"
fi
