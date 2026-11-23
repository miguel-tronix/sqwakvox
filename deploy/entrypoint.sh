#!/usr/bin/env bash
set -euo pipefail

# Prepare mounted persistent agent-data storage (e.g. Cloud Run GCS FUSE at /data)
if [[ -n "${SQWAKVOX_DATA_DIR:-}" ]]; then
    echo "[entrypoint] Initializing persistent agent storage at ${SQWAKVOX_DATA_DIR}..."
    mkdir -p \
        "${SQWAKVOX_DATA_DIR}/skills" \
        "${SQWAKVOX_DATA_DIR}/chat_logs" \
        "${SQWAKVOX_DATA_DIR}/checkpoints" \
        "${SQWAKVOX_DATA_DIR}/uploads" \
        "${SQWAKVOX_DATA_DIR}/state" \
        "${SQWAKVOX_DATA_DIR}/episodes" \
        "${SQWAKVOX_DATA_DIR}/cache/docling" || true

    # Seed repo-bundled skills on first boot without overwriting user/agent edits.
    if [[ -d "/app/skills" ]]; then
        cp -rn /app/skills/. "${SQWAKVOX_DATA_DIR}/skills/" 2>/dev/null || true
    fi

    # Restore local SQLite MCP database snapshot onto fast container disk
    # (live SQLite must not run directly on a GCS FUSE mount).
    if [[ -f "${SQWAKVOX_DATA_DIR}/state/sqwakvox_mcp.db" && ! -f "/app/sqwakvox_mcp.db" ]]; then
        cp "${SQWAKVOX_DATA_DIR}/state/sqwakvox_mcp.db" "/app/sqwakvox_mcp.db" || true
    fi
fi

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
