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
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, cast

import redis
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


def _enc(value: str) -> str:
    """Encode a thread/namespace id into a Redis-key-safe form."""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _dec(encoded: str) -> str:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode()


class RedisCheckpointer(BaseCheckpointSaver[str]):
    """Minimal LangGraph checkpointer storing checkpoints in Redis hashes.

    Implements the full ``BaseCheckpointSaver`` interface (sync + async) so the
    compiled graph can resume threads across processes and restarts.
    """

    def __init__(self, redis_url: str, *, key_prefix: str = "sqwakvox:ckpt") -> None:
        super().__init__(serde=JsonPlusSerializer())
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = key_prefix

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
        type_, blob = self.serde.dumps_typed(value)
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
    # Sync API
    # ------------------------------------------------------------------ #
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        ckpt_key = self._ckpt_key(thread_id, checkpoint_ns)

        if checkpoint_id := get_checkpoint_id(config):
            raw = self._hget(ckpt_key, checkpoint_id)
        else:
            ids = self._hkeys(ckpt_key)
            if not ids:
                return None
            checkpoint_id = max(ids)  # ULIDs sort lexicographically: max == latest
            raw = self._hget(ckpt_key, checkpoint_id)
        if raw is None:
            return None

        checkpoint, metadata, parent_checkpoint_id = cast(
            tuple[Checkpoint, CheckpointMetadata, str | None],
            self._loads(raw),
        )
        writes = self._load_writes(thread_id, checkpoint_ns, checkpoint_id)

        return CheckpointTuple(
            config=(
                config
                if get_checkpoint_id(config)
                else {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_id,
                    }
                }
            ),
            checkpoint=checkpoint,
            metadata=metadata,
            pending_writes=writes,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_checkpoint_id,
                    }
                }
                if parent_checkpoint_id
                else None
            ),
        )

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
        else:
            prefix = f"{self._prefix}:ckpt:"
            thread_ids = []
            for key in self._redis.scan_iter(f"{prefix}*"):
                tid = _dec(cast(str, key).removeprefix(prefix).rsplit(":", 1)[0])
                if tid not in thread_ids:
                    thread_ids.append(tid)

        config_checkpoint_ns = (
            config["configurable"].get("checkpoint_ns") if config else None
        )
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
                    raw = self._hget(
                        self._ckpt_key(thread_id, checkpoint_ns), checkpoint_id
                    )
                    if raw is None:
                        continue
                    checkpoint, metadata, parent_checkpoint_id = cast(
                        tuple[Checkpoint, CheckpointMetadata, str | None],
                        self._loads(raw),
                    )
                    if filter and not all(
                        query_value == metadata.get(query_key)
                        for query_key, query_value in filter.items()
                    ):
                        continue
                    yield CheckpointTuple(
                        config={
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": checkpoint_ns,
                                "checkpoint_id": checkpoint_id,
                            }
                        },
                        checkpoint=checkpoint,
                        metadata=metadata,
                        pending_writes=self._load_writes(
                            thread_id, checkpoint_ns, checkpoint_id
                        ),
                        parent_config=(
                            {
                                "configurable": {
                                    "thread_id": thread_id,
                                    "checkpoint_ns": checkpoint_ns,
                                    "checkpoint_id": parent_checkpoint_id,
                                }
                            }
                            if parent_checkpoint_id
                            else None
                        ),
                    )
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
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

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
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        return await asyncio.to_thread(
            self.put_writes, config, writes, task_id, task_path
        )

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
