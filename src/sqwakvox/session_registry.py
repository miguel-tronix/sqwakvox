"""Session and document state registry in Redis.

Allows Sqwakvox (the TUI process) to publish active document metadata,
extracted data stores, and worker queue mappings into Redis, so external
tools and agents (like the Sqwakvox MCP gateway, Hermes-agent, Cursor, etc.)
can discover open documents and query them with full context.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, cast

import redis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("SQWAKVOX_SESSION_REDIS_URL") or os.environ.get(
    "SQWAKVOX_CELERY_BROKER", "redis://localhost:6379/0"
)

REDIS_KEY_ACTIVE = "sqwakvox:session:active"
REDIS_KEY_DOCUMENTS = "sqwakvox:session:documents"
REDIS_KEY_DOC_PREFIX = "sqwakvox:session:doc:"

# 24-hour expiration for cached document payloads
DOC_PAYLOAD_TTL_SEC = 86400


def get_redis_client() -> redis.Redis:
    """Return a Redis client configured for the session registry."""
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def is_redis_available() -> bool:
    """Return True if the Redis session store is reachable."""
    try:
        client = get_redis_client()
        return bool(client.ping())
    except Exception as exc:
        logger.debug("Redis ping failed: %s", exc)
        return False


def publish_active_document(
    doc_name: str,
    source: str,
    domain_id: str,
    queue: str | None,
    doc_context: str,
    data_store: dict[str, str] | None = None,
    table_count: int = 0,
    model_id: str = "openai:gpt-5.5-high",
    thread_id: str | None = None,
) -> bool:
    """Publish the currently active document and its context to Redis.

    Returns True if published successfully, False on failure.
    """
    try:
        client = get_redis_client()
        now = time.time()

        active_meta = {
            "active_document_name": doc_name,
            "source": source,
            "domain_id": domain_id,
            "queue": queue or "sqwakvox",
            "table_count": table_count,
            "model_id": model_id,
            "thread_id": thread_id,
            "updated_at": now,
        }

        # 1. Update the single active-document pointer
        client.set(REDIS_KEY_ACTIVE, json.dumps(active_meta))

        # 2. Store the full payload (context + tables) keyed by document name
        doc_payload = {
            **active_meta,
            "doc_context": doc_context,
            "data_store": data_store or {},
        }
        payload_key = f"{REDIS_KEY_DOC_PREFIX}{doc_name}"
        client.set(payload_key, json.dumps(doc_payload), ex=DOC_PAYLOAD_TTL_SEC)

        # 3. Add to the catalog of loaded documents
        client.hset(
            REDIS_KEY_DOCUMENTS,
            doc_name,
            json.dumps(
                {
                    "file_name": doc_name,
                    "source": source,
                    "domain_id": domain_id,
                    "queue": queue or "sqwakvox",
                    "table_count": table_count,
                    "updated_at": now,
                }
            ),
        )
        logger.debug("Published active document '%s' to Redis registry", doc_name)
        return True
    except Exception as exc:
        logger.debug("Failed to publish document state to Redis: %s", exc)
        return False


def get_active_session() -> dict[str, Any] | None:
    """Retrieve metadata about the currently active document in Sqwakvox."""
    try:
        client = get_redis_client()
        raw = cast(str | None, client.get(REDIS_KEY_ACTIVE))
        if not raw:
            return None
        return cast(dict[str, Any], json.loads(raw))
    except Exception as exc:
        logger.debug("Failed to read active session from Redis: %s", exc)
        return None


def get_document_payload(doc_name: str | None = None) -> dict[str, Any] | None:
    """Retrieve the full payload (context, tables, queue) for a document.

    If *doc_name* is omitted, returns the payload for the currently active document.
    """
    try:
        client = get_redis_client()
        target_name = doc_name
        if not target_name:
            active = get_active_session()
            if not active:
                return None
            target_name = active.get("active_document_name")
            if not target_name:
                return None

        payload_key = f"{REDIS_KEY_DOC_PREFIX}{target_name}"
        raw = cast(str | None, client.get(payload_key))
        if not raw:
            return None
        return cast(dict[str, Any], json.loads(raw))
    except Exception as exc:
        logger.debug("Failed to read document payload for '%s': %s", doc_name, exc)
        return None


def list_active_documents() -> list[dict[str, Any]]:
    """Return a list of all documents currently recorded in the Redis catalog."""
    try:
        client = get_redis_client()
        entries = cast(dict[str, str], client.hgetall(REDIS_KEY_DOCUMENTS))
        if not entries:
            return []
        docs = []
        for raw in entries.values():
            with contextlib_suppress(Exception):
                docs.append(json.loads(raw))
        return docs
    except Exception as exc:
        logger.debug("Failed to list documents from Redis: %s", exc)
        return []


def clear_session() -> bool:
    """Clear session markers from Redis when the TUI cleanly shuts down."""
    try:
        client = get_redis_client()
        client.delete(REDIS_KEY_ACTIVE)
        client.delete(REDIS_KEY_DOCUMENTS)
        return True
    except Exception as exc:
        logger.debug("Failed to clear Redis session keys: %s", exc)
        return False


from contextlib import suppress as contextlib_suppress  # noqa: E402
