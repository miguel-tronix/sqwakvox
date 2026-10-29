# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:latest AS uv_bin

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:/usr/local/bin:${PATH}" \
    PORT=8080 \
    SQWAKVOX_CELERY_BROKER="redis://127.0.0.1:6379/0" \
    SQWAKVOX_CELERY_BACKEND="redis://127.0.0.1:6379/1" \
    SQWAKVOX_MEMORY_REDIS_URL="redis://127.0.0.1:6379/2"

# System dependencies:
# - redis-server: in-container broker, result backend, and conversation memory
# - libgl1, libglib2.0-0, libgomp1: OpenCV / RapidOCR / PyTorch runtime libs for Docling
# - nodejs & npm: for mcp-ascii-charts MCP server
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    nodejs \
    npm \
    redis-server \
    && npm install -g mcp-ascii-charts \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv_bin /uv /uvx /usr/local/bin/

WORKDIR /app

# 1. Install dependencies first (cached layer unless pyproject.toml/uv.lock change)
#    Skip GPU/CUDA packages in uv.lock and install CPU-only torch + torchvision
#    (~3.5 GB smaller download and image size).
ARG UV_CACHE_DIR=/tmp/uv-cache
ENV UV_HTTP_TIMEOUT=300 \
    UV_CONCURRENT_DOWNLOADS=4
COPY pyproject.toml uv.lock README.md ./
RUN NO_CUDA_ARGS=$(grep -oE '^name = "(nvidia-[^"]+|cuda-[^"]+|triton|torch|torchvision)"' uv.lock \
        | sed 's/^name = "\(.*\)"/--no-install-package \1/') \
    && UV_CACHE_DIR="${UV_CACHE_DIR}" uv sync --frozen --no-dev --no-install-project ${NO_CUDA_ARGS} \
    && UV_CACHE_DIR="${UV_CACHE_DIR}" uv pip install --python /app/.venv/bin/python --no-deps \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.12.0 torchvision==0.27.0 \
    && rm -rf /tmp/uv-cache

# 2. Copy application code, MCP config, and lightweight sample PDFs
COPY src/ ./src/
COPY mcp_servers.json ./mcp_servers.json
COPY samples/ ./samples/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN UV_CACHE_DIR="${UV_CACHE_DIR}" uv pip install --python /app/.venv/bin/python --no-deps . \
    && chmod +x /usr/local/bin/entrypoint.sh \
    && rm -rf /tmp/uv-cache

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
