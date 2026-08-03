#!/bin/sh
# Niki backend container entrypoint.
# Waits for Postgres to accept connections, runs Alembic migrations,
# then hands off to uvicorn.

set -e

echo "[entrypoint] Waiting for Postgres at ${DATABASE_HOST}:${DATABASE_PORT}..."
# Plain TCP probe (pg_isready isn't installed in the slim image).
python - <<'PY'
import os, socket, sys, time
host = os.environ.get("DATABASE_HOST", "postgres")
port = int(os.environ.get("DATABASE_PORT", "5432"))
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[entrypoint] postgres {host}:{port} reachable")
            sys.exit(0)
    except OSError:
        time.sleep(1)
print(f"[entrypoint] postgres {host}:{port} not reachable after 60s", file=sys.stderr)
sys.exit(1)
PY

echo "[entrypoint] Running Alembic migrations..."
alembic upgrade head

echo "[entrypoint] Starting uvicorn..."
exec uvicorn niki.main:app \
    --host 0.0.0.0 \
    --port "${API_PORT:-8000}" \
    --no-access-log
