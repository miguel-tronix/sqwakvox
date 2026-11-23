"""LangGraph checkpointer backed by plain Redis hashes.

Why this exists: the official ``langgraph-checkpoint-redis`` package requires
the RediSearch module (``FT._LIST``/``FT.SEARCH``), which the stock Ubuntu
``redis-redisearch`` package does not provide (it ships 2018-era RediSearch
1.2.2).  This saver ports the storage contract of LangGraph's reference
``InMemorySaver``/SQLite savers onto vanilla Redis hashes, so conversation
memory works against any plain ``redis-server`` — no modules required.

Storage layout (all keys under a configurable prefix)::

    {prefix}:ckpt:{b64(thread_id)}:{b64(checkpoint_ns)}             hash
        field=checkpoint_id  value=serde([checkpoint, metadata, parent_id])
    {prefix}:wr:{b64(thread_id)}:{b64(checkpoint_ns)}:{checkpoint_id}  hash
        field="{task_id}:{idx:06d}"  value=serde([channel, value])

Checkpoint ids are ULIDs, so lexicographic ``max()`` over hash fields selects
the latest checkpoint, exactly like the in-memory reference implementation.

If you later install Redis Stack, swap this class for the official
``langgraph.checkpoint.redis.RedisSaver`` behind the ``_get_redis_checkpointer``
factory in :mod:`sqwakvox.agent` — nothing else changes.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import json
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import redis
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

logger = logging.getLogger(__name__)


def _enc(value: str) -> str:
    """Encode a thread/namespace id into a Redis-key-safe form."""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _dec(encoded: str) -> str:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode()


def _sanitize_for_serde(obj: Any, depth: int = 0) -> Any:
    """Recursively sanitize objects so JsonPlusSerializer/msgpack can serialize them.

    Replaces non-serializable objects (like raw HTTP responses, un-encodable custom
    types, or orphaned callables inside AIMessage/state) with string representations.
    """
    if depth > 50:
        return str(obj)
    if obj is None or isinstance(obj, (int, float, str, bool, bytes)):
        return obj
    if isinstance(obj, BaseMessage):
        kwargs: dict[str, Any] = {}
        for attr in ("content", "additional_kwargs", "response_metadata", "name", "id"):
            if hasattr(obj, attr):
                val = getattr(obj, attr)
                if val is not None:
                    kwargs[attr] = _sanitize_for_serde(val, depth + 1)
        if hasattr(obj, "tool_calls") and obj.tool_calls:
            kwargs["tool_calls"] = _sanitize_for_serde(obj.tool_calls, depth + 1)
        return obj.__class__(**kwargs)
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_serde(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        res = [_sanitize_for_serde(x, depth + 1) for x in obj]
        return tuple(res) if isinstance(obj, tuple) else res
    if isinstance(obj, set):
        return {_sanitize_for_serde(x, depth + 1) for x in obj}

    try:
        JsonPlusSerializer().dumps_typed(obj)
        return obj
    except Exception:
        if hasattr(obj, "model_dump") and callable(obj.model_dump):
            try:
                dumped = obj.model_dump()
                return _sanitize_for_serde(dumped, depth + 1)
            except Exception:
                pass
        return str(obj)


class RedisCheckpointer(BaseCheckpointSaver[str]):
    """Minimal LangGraph checkpointer storing checkpoints in Redis hashes.

    Implements the full ``BaseCheckpointSaver`` interface (sync + async) so the
    compiled graph can resume threads across processes and restarts.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = "sqwakvox:ckpt",
        backup_dir: Path | str | None = None,
    ) -> None:
        super().__init__(serde=JsonPlusSerializer())
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = key_prefix
        self._backup_dir: Path | None = Path(backup_dir) if backup_dir is not None else None
        self._hydrated_keys: set[tuple[str, str]] = set()
        self._hydrated_all: bool = False
        if self._backup_dir is not None:
            try:
                self._backup_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "Could not create checkpoint backup dir %s (%s); continuing Redis-only",
                    self._backup_dir,
                    exc,
                )
                self._backup_dir = None

    # ------------------------------------------------------------------ #
    # Durable file mirror (optional write-through + lazy restore)
    # ------------------------------------------------------------------ #
    def _backup_file(self, thread_id: str, checkpoint_ns: str) -> Path | None:
        if self._backup_dir is None:
            return None
        ns_part = _enc(checkpoint_ns) if checkpoint_ns else "default"
        return self._backup_dir / f"{_enc(thread_id)}__{ns_part}.json"

    def _hydrate_thread_ns(self, thread_id: str, checkpoint_ns: str) -> None:
        """Load persisted checkpoints from disk into Redis on first miss."""
        if self._backup_dir is None:
            return
        cache_key = (thread_id, checkpoint_ns)
        if cache_key in self._hydrated_keys:
            return
        self._hydrated_keys.add(cache_key)

        path = self._backup_file(thread_id, checkpoint_ns)
        if path is None or not path.is_file():
            return
        # Only hydrate when Redis has no checkpoints for this thread/namespace yet.
        if self._hkeys(self._ckpt_key(thread_id, checkpoint_ns)):
            return
        self._load_backup_file_into_redis(path)

    def _hydrate_all_backups(self) -> None:
        if self._backup_dir is None or self._hydrated_all:
            return
        self._hydrated_all = True
        try:
            files = sorted(self._backup_dir.glob("*.json"))
        except OSError as exc:
            logger.debug("Failed to list checkpoint backups in %s: %s", self._backup_dir, exc)
            return
        for path in files:
            self._load_backup_file_into_redis(path)

    def _load_backup_file_into_redis(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read checkpoint backup %s: %s", path, exc)
            return
        thread_id = payload.get("thread_id")
        checkpoint_ns = payload.get("checkpoint_ns", "")
        if not isinstance(thread_id, str) or not isinstance(checkpoint_ns, str):
            return
        self._hydrated_keys.add((thread_id, checkpoint_ns))
        ckpt_key = self._ckpt_key(thread_id, checkpoint_ns)
        if self._hkeys(ckpt_key):
            return
        checkpoints = payload.get("checkpoints") or {}
        if isinstance(checkpoints, dict):
            for ckpt_id, raw in checkpoints.items():
                if isinstance(ckpt_id, str) and isinstance(raw, str):
                    self._redis.hset(ckpt_key, ckpt_id, raw)
        writes_by_ckpt = payload.get("writes") or {}
        if isinstance(writes_by_ckpt, dict):
            for ckpt_id, fields in writes_by_ckpt.items():
                if not isinstance(ckpt_id, str) or not isinstance(fields, dict):
                    continue
                wr_key = self._wr_key(thread_id, checkpoint_ns, ckpt_id)
                for field, raw in fields.items():
                    if isinstance(field, str) and isinstance(raw, str):
                        self._redis.hset(wr_key, field, raw)

    def _persist_thread_ns(self, thread_id: str, checkpoint_ns: str) -> None:
        """Write-through current Redis state for *(thread_id, checkpoint_ns)* to disk."""
        path = self._backup_file(thread_id, checkpoint_ns)
        if path is None:
            return
        try:
            checkpoints = self._hgetall(self._ckpt_key(thread_id, checkpoint_ns))
            writes: dict[str, dict[str, str]] = {}
            for ckpt_id in checkpoints:
                wr = self._hgetall(self._wr_key(thread_id, checkpoint_ns, ckpt_id))
                if wr:
                    writes[ckpt_id] = wr
            payload = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoints": checkpoints,
                "writes": writes,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(payload, separators=(",", ":"))
            tmp_path = path.with_suffix(".json.tmp")
            try:
                tmp_path.write_text(serialized, encoding="utf-8")
                tmp_path.replace(path)
            except OSError:
                # Fallback for filesystems where rename-over-existing fails.
                path.write_text(serialized, encoding="utf-8")
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to persist checkpoint backup %s: %s", path, exc)

    # ------------------------------------------------------------------ #
    # Key layout
    # ------------------------------------------------------------------ #
    def _ckpt_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._prefix}:ckpt:{_enc(thread_id)}:{_enc(checkpoint_ns)}"

    def _wr_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._prefix}:wr:{_enc(thread_id)}:{_enc(checkpoint_ns)}:{checkpoint_id}"

    # ------------------------------------------------------------------ #
    # Typed serialisation (JsonPlusSerializer exposes dumps_typed/loads_typed)
    # ------------------------------------------------------------------ #
    def _dumps(self, value: Any) -> str:
        try:
            type_, blob = self.serde.dumps_typed(value)
        except Exception:
            sanitized = _sanitize_for_serde(value)
            type_, blob = self.serde.dumps_typed(sanitized)
        encoded_blob = base64.b64encode(blob).decode() if isinstance(blob, bytes) else blob
        return json.dumps([type_, encoded_blob])

    def _loads(self, raw: str) -> Any:
        type_, blob = json.loads(raw)
        # dumps_typed always returns a msgpack/raw bytes blob; we store it
        # base64-encoded, so decode unconditionally before loads_typed.
        return self.serde.loads_typed((type_, base64.b64decode(blob)))

    # redis-py 7.x types its sync commands as ``Awaitable[...] | T`` (the
    # union supports both sync and async call sites); these wrappers pin the
    # concrete sync return types for mypy.
    def _hget(self, key: str, field: str) -> str | None:
        return cast(str | None, self._redis.hget(key, field))

    def _hgetall(self, key: str) -> dict[str, str]:
        return cast(dict[str, str], self._redis.hgetall(key))

    def _hkeys(self, key: str) -> builtins.list[str]:
        return cast(builtins.list[str], self._redis.hkeys(key))

    # ------------------------------------------------------------------ #
    # Tuple assembly (shared by get_tuple / list)
    # ------------------------------------------------------------------ #
    def _config(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
        """Build a RunnableConfig pinned to a specific checkpoint."""
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def _load_parts(self, raw: str) -> tuple[Checkpoint, CheckpointMetadata, str | None]:
        """Deserialise one stored checkpoint hash value."""
        return cast(
            tuple[Checkpoint, CheckpointMetadata, str | None],
            self._loads(raw),
        )

    def _tuple_for(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        parts: tuple[Checkpoint, CheckpointMetadata, str | None],
    ) -> CheckpointTuple:
        """Assemble a CheckpointTuple (with pending writes) for a checkpoint."""
        checkpoint, metadata, parent_checkpoint_id = parts
        return CheckpointTuple(
            config=self._config(thread_id, checkpoint_ns, checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            pending_writes=self._load_writes(thread_id, checkpoint_ns, checkpoint_id),
            parent_config=(
                self._config(thread_id, checkpoint_ns, parent_checkpoint_id)
                if parent_checkpoint_id
                else None
            ),
        )

    # ------------------------------------------------------------------ #
    # Sync API
    # ------------------------------------------------------------------ #
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        self._hydrate_thread_ns(thread_id, checkpoint_ns)
        ckpt_key = self._ckpt_key(thread_id, checkpoint_ns)

        requested_id = get_checkpoint_id(config)
        if requested_id:
            raw = self._hget(ckpt_key, requested_id)
            checkpoint_id = requested_id
        else:
            ids = self._hkeys(ckpt_key)
            if not ids:
                return None
            checkpoint_id = max(ids)  # ULIDs sort lexicographically: max == latest
            raw = self._hget(ckpt_key, checkpoint_id)
        if raw is None:
            return None

        tup = self._tuple_for(thread_id, checkpoint_ns, checkpoint_id, self._load_parts(raw))
        if requested_id:
            # The caller asked for a specific checkpoint: echo their exact
            # config object back instead of our reconstructed one.
            return tup._replace(config=config)
        return tup

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 — interface parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        thread_ids: Sequence[str]
        if config:
            thread_ids = (config["configurable"]["thread_id"],)
            ns_hint = config["configurable"].get("checkpoint_ns")
            if ns_hint is not None:
                self._hydrate_thread_ns(thread_ids[0], ns_hint)
            else:
                self._hydrate_all_backups()
        else:
            self._hydrate_all_backups()
            prefix = f"{self._prefix}:ckpt:"
            thread_ids = []
            for key in self._redis.scan_iter(f"{prefix}*"):
                tid = _dec(cast(str, key).removeprefix(prefix).rsplit(":", 1)[0])
                if tid not in thread_ids:
                    thread_ids.append(tid)

        config_checkpoint_ns = config["configurable"].get("checkpoint_ns") if config else None
        config_checkpoint_id = get_checkpoint_id(config) if config else None

        for thread_id in thread_ids:
            namespaces = (
                (config_checkpoint_ns,)
                if config_checkpoint_ns is not None
                else self._list_namespaces(thread_id)
            )
            for checkpoint_ns in namespaces:
                fields = self._hkeys(self._ckpt_key(thread_id, checkpoint_ns))
                for checkpoint_id in sorted(fields, reverse=True):
                    if config_checkpoint_id and checkpoint_id != config_checkpoint_id:
                        continue
                    if (
                        before
                        and (before_id := get_checkpoint_id(before))
                        and checkpoint_id >= before_id
                    ):
                        continue
                    raw = self._hget(self._ckpt_key(thread_id, checkpoint_ns), checkpoint_id)
                    if raw is None:
                        continue
                    parts = self._load_parts(raw)
                    if filter and not all(
                        query_value == parts[1].get(query_key)
                        for query_key, query_value in filter.items()
                    ):
                        continue
                    yield self._tuple_for(thread_id, checkpoint_ns, checkpoint_id, parts)
                    if limit is not None:
                        limit -= 1
                        if limit <= 0:
                            return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions  # channel values are stored inline; nothing to version
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")
        self._redis.hset(
            self._ckpt_key(thread_id, checkpoint_ns),
            checkpoint_id,
            self._dumps(
                (checkpoint, get_checkpoint_metadata(config, metadata), parent_checkpoint_id)
            ),
        )
        self._hydrated_keys.add((thread_id, checkpoint_ns))
        self._persist_thread_ns(thread_id, checkpoint_ns)
        return self._config(thread_id, checkpoint_ns, checkpoint_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del task_path  # not needed for pending-write reconstruction
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        wr_key = self._wr_key(thread_id, checkpoint_ns, checkpoint_id)
        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            self._redis.hset(
                wr_key,
                f"{task_id}:{write_idx:06d}",
                self._dumps((channel, value)),
            )
        self._persist_thread_ns(thread_id, checkpoint_ns)

    # ------------------------------------------------------------------ #
    # Async API (the compiled graph calls these from ainvoke)
    # ------------------------------------------------------------------ #
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 — interface parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        tuples = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in tuples:
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _load_writes(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> builtins.list[tuple[str, str, Any]]:
        fields = self._hgetall(self._wr_key(thread_id, checkpoint_ns, checkpoint_id))
        writes: builtins.list[tuple[str, str, Any]] = []
        for field in sorted(fields):
            task_id, _idx = field.rsplit(":", 1)
            channel, value = cast(tuple[str, Any], self._loads(fields[field]))
            writes.append((task_id, channel, value))
        return writes

    def _list_namespaces(self, thread_id: str) -> builtins.list[str]:
        prefix = f"{self._prefix}:ckpt:{_enc(thread_id)}:"
        namespaces: list[str] = []
        for key in self._redis.scan_iter(f"{prefix}*"):
            ns = cast(str, key).removeprefix(prefix)
            if ns not in namespaces:
                namespaces.append(ns)
        return namespaces
