"""Tests for persistent agent storage paths, SQLite snapshotting, and checkpoint backup."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from sqwakvox import storage
from sqwakvox.backend.redis_checkpointer import RedisCheckpointer
from sqwakvox.domains.swe import retrieval, skills


class _FakeRedis:
    """Minimal in-memory Redis hash store for testing RedisCheckpointer."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}

    def hset(self, key: str, field: str, value: str) -> int:
        self._hashes.setdefault(key, {})[field] = value
        return 1

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def hkeys(self, key: str) -> list[str]:
        return list(self._hashes.get(key, {}).keys())

    def scan_iter(self, pattern: str) -> list[str]:
        return [k for k in self._hashes if fnmatch.fnmatch(k, pattern)]


def test_storage_resolves_all_subdirs_under_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for var in (
        "SQWAKVOX_SKILLS_DIR",
        "SQWAKVOX_CHAT_LOG_DIR",
        "SQWAKVOX_UPLOADS_DIR",
        "SQWAKVOX_CHECKPOINTS_DIR",
        "SQWAKVOX_STATE_DIR",
        "SQWAKVOX_SWE_INDEX_DIR",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("SQWAKVOX_DATA_DIR", str(tmp_path))

    assert storage.skills_base_dir() == tmp_path / "skills"
    assert storage.chat_log_dir() == tmp_path / "chat_logs"
    assert storage.uploads_dir() == tmp_path / "uploads"
    assert storage.checkpoints_dir() == tmp_path / "checkpoints"
    assert storage.state_snapshot_dir() == tmp_path / "state"
    # Live SQLite index stays off the FUSE mount on fast local temp storage.
    assert storage.swe_index_dir() != tmp_path / "swe"
    assert skills.skills_root("swe") == tmp_path / "skills" / "swe"


def test_storage_specific_env_vars_override_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SQWAKVOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SQWAKVOX_SKILLS_DIR", str(tmp_path / "custom_skills"))
    monkeypatch.setenv("SQWAKVOX_UPLOADS_DIR", str(tmp_path / "custom_uploads"))

    assert storage.skills_base_dir() == tmp_path / "custom_skills"
    assert storage.uploads_dir() == tmp_path / "custom_uploads"


def test_retrieval_snapshots_and_restores_sqlite_across_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_dir_1 = tmp_path / "local_run_1"
    local_dir_2 = tmp_path / "local_run_2"
    state_dir = tmp_path / "mounted_state"

    monkeypatch.setenv("SQWAKVOX_SWE_INDEX_DIR", str(local_dir_1))
    monkeypatch.setenv("SQWAKVOX_STATE_DIR", str(state_dir))

    assert retrieval.index_document("book.epub", ["strategy pattern decouples algorithms"]) == 1
    snapshot_file = state_dir / "index.db"
    assert snapshot_file.is_file()

    # Simulate container restart with an empty local /tmp directory.
    monkeypatch.setenv("SQWAKVOX_SWE_INDEX_DIR", str(local_dir_2))
    assert not (local_dir_2 / "index.db").exists()

    results = retrieval.search_document("book.epub", "strategy pattern")
    assert len(results) == 1
    assert "decouples algorithms" in results[0]["content"]
    assert (local_dir_2 / "index.db").is_file()


def test_redis_checkpointer_persists_and_hydrates_across_redis_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "checkpoints"
    fake_redis_1 = _FakeRedis()
    fake_redis_2 = _FakeRedis()

    monkeypatch.setattr("redis.Redis.from_url", lambda *_a, **_kw: fake_redis_1)
    saver1 = RedisCheckpointer("redis://localhost:6379/2", backup_dir=backup_dir)

    config: RunnableConfig = {"configurable": {"thread_id": "doc:report.pdf", "checkpoint_ns": ""}}
    checkpoint: Checkpoint = {
        "v": 1,
        "id": "01J00000000000000000000001",
        "ts": "2026-11-22T00:00:00+00:00",
        "channel_values": {"messages": ["hello world"]},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }
    metadata: CheckpointMetadata = {"source": "input", "step": 1, "writes": {}}

    saved_cfg = saver1.put(config, checkpoint, metadata, {})
    saver1.put_writes(saved_cfg, [("messages", "pending")], task_id="task-1")

    assert list(backup_dir.glob("*.json"))

    # Simulate container restart with a brand new empty Redis DB 2.
    monkeypatch.setattr("redis.Redis.from_url", lambda *_a, **_kw: fake_redis_2)
    saver2 = RedisCheckpointer("redis://localhost:6379/2", backup_dir=backup_dir)

    restored = saver2.get_tuple(config)
    assert restored is not None
    assert restored.checkpoint["id"] == "01J00000000000000000000001"
    assert restored.checkpoint["channel_values"] == {"messages": ["hello world"]}
    assert restored.pending_writes == [("task-1", "messages", "pending")]

    # Listing all threads without a config also hydrates from backup_dir.
    fake_redis_3 = _FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda *_a, **_kw: fake_redis_3)
    saver3 = RedisCheckpointer("redis://localhost:6379/2", backup_dir=backup_dir)
    listed: list[Any] = list(saver3.list(None))
    assert len(listed) == 1
    assert listed[0].checkpoint["id"] == "01J00000000000000000000001"


def test_docling_slice_cache_hits_and_invalidates_on_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import MagicMock

    from sqwakvox.controller import AppController

    cache_dir = tmp_path / "cache" / "docling"
    monkeypatch.setenv("SQWAKVOX_DOCLING_CACHE_DIR", str(cache_dir))

    pdf = tmp_path / "cookbook.pdf"
    pdf.write_bytes(b"%PDF-1.4 version 1")

    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeConverter:
        def convert(self, source: str, **kwargs: Any) -> Any:
            calls.append((source, kwargs))
            res = MagicMock()
            pr = kwargs.get("page_range")
            res.document.export_to_markdown.return_value = f"# parsed {pr}"
            res.document.tables = []
            res.pages = [object()] * (pr[1] - pr[0] + 1 if pr else 1)
            return res

    monkeypatch.setattr("sqwakvox.controller.pdf_has_text_layer", lambda _s: True)
    monkeypatch.setattr("sqwakvox.controller.pdf_page_count", lambda _s: 20)

    controller = AppController()
    controller._no_ocr_converter = _FakeConverter()

    # 1. First conversion populates cache.
    doc1 = controller.convert_document(
        str(pdf), lambda: False, domain_id="swe", page_range=(1, 10)
    )
    assert doc1 is not None
    assert doc1.raw_markdown == "# parsed (1, 10)"
    assert len(calls) == 1

    # 2. Second conversion of same file + slice hits cache without calling Docling.
    doc2 = controller.convert_document(
        str(pdf), lambda: False, domain_id="swe", page_range=(1, 10)
    )
    assert doc2 is not None
    assert doc2.raw_markdown == "# parsed (1, 10)"
    assert len(calls) == 1

    # 3. Different page range misses cache.
    doc3 = controller.convert_document(
        str(pdf), lambda: False, domain_id="swe", page_range=(11, 20)
    )
    assert doc3 is not None
    assert doc3.raw_markdown == "# parsed (11, 20)"
    assert len(calls) == 2

    # 4. Modified file content invalidates cache automatically.
    pdf.write_bytes(b"%PDF-1.4 version 2 (updated)")
    doc4 = controller.convert_document(
        str(pdf), lambda: False, domain_id="swe", page_range=(1, 10)
    )
    assert doc4 is not None
    assert len(calls) == 3
