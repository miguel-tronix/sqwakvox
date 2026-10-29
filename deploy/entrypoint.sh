#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] Starting local Redis server on 127.0.0.1:6379..."
redis-server \
    --bind 127.0.0.1 \
    --port 6379 \
    --daemonize yes \
    --save "" \
    --appendonly no \
    --loglevel warning

# Wait briefly for Redis to accept connections
for _ in {1..20}; do
    if redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
        echo "[entrypoint] Redis is ready."
        break
    fi
    sleep 0.2
done

PORT="${PORT:-8080}"
EXTRA_ARGS=("--host" "0.0.0.0" "--port" "${PORT}" "--allow-remote")

# If SQWAKVOX_WEB_TOKEN is not explicitly set, pass --no-token so Cloud Run IAM
# (via `gcloud run services proxy`) or local `podman run` works without needing
# to scrape an auto-generated token from container logs.
if [[ -z "${SQWAKVOX_WEB_TOKEN:-}" ]]; then
    EXTRA_ARGS+=("--no-token")
fi

echo "[entrypoint] Launching sqwakvox-web on 0.0.0.0:${PORT}..."
exec /app/.venv/bin/sqwakvox-web "${EXTRA_ARGS[@]}" "$@"
