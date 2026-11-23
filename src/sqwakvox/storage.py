"""Persistent storage path resolution and safe SQLite snapshotting.

Centralizes every writable agent-data path so both local desktop runs and
Cloud Run deployments (with a GCS FUSE bucket mounted at ``SQWAKVOX_DATA_DIR``)
agree on where files live:

- ``SQWAKVOX_SKILLS_DIR``      -> ``<data>/skills``      (fallback: ``./skills``)
- ``SQWAKVOX_CHAT_LOG_DIR``    -> ``<data>/chat_logs``   (fallback: ``~/.sqwakvox_chat_logs``)
- ``SQWAKVOX_UPLOADS_DIR``     -> ``<data>/uploads``     (fallback: ``~/.sqwakvox/uploads``)
- ``SQWAKVOX_CHECKPOINTS_DIR`` -> ``<data>/checkpoints`` (fallback: ``None``, Redis-only)
- ``SQWAKVOX_STATE_DIR``       -> ``<data>/state``       (fallback: ``None``, no snapshots)
- ``SQWAKVOX_SWE_INDEX_DIR``   -> local ``/tmp/sqwakvox/swe`` when ``SQWAKVOX_DATA_DIR``
  is set (so live SQLite never runs over GCS FUSE), otherwise ``~/.sqwakvox/swe``.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DOCLING_CACHE_SCHEMA_VERSION = "v1"
# In-process cache mapping (path, size, mtime_ns) -> sha256 hex digest.
_FILE_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def data_dir() -> Path | None:
    """Return the mounted persistent agent-data root (``SQWAKVOX_DATA_DIR``), if configured."""
    return _env_path("SQWAKVOX_DATA_DIR")


def skills_base_dir() -> Path:
    """Return the base directory holding per-domain skills folders."""
    override = _env_path("SQWAKVOX_SKILLS_DIR")
    if override is not None:
        return override
    root = data_dir()
    if root is not None:
        return root / "skills"
    return Path.cwd() / "skills"


def chat_log_dir() -> Path:
    """Return the directory for persisted per-document JSONL chat logs."""
    override = _env_path("SQWAKVOX_CHAT_LOG_DIR")
    if override is not None:
        return override
    root = data_dir()
    if root is not None:
        return root / "chat_logs"
    return Path.home() / ".sqwakvox_chat_logs"


def uploads_dir() -> Path:
    """Return the directory for browser-uploaded documents."""
    override = _env_path("SQWAKVOX_UPLOADS_DIR")
    if override is not None:
        return override
    root = data_dir()
    if root is not None:
        return root / "uploads"
    return Path.home() / ".sqwakvox" / "uploads"


def checkpoints_dir() -> Path | None:
    """Return the directory for durable LangGraph checkpoint backups, or None."""
    override = _env_path("SQWAKVOX_CHECKPOINTS_DIR")
    if override is not None:
        return override
    root = data_dir()
    if root is not None:
        return root / "checkpoints"
    return None


def state_snapshot_dir() -> Path | None:
    """Return the directory for durable SQLite database snapshots, or None."""
    override = _env_path("SQWAKVOX_STATE_DIR")
    if override is not None:
        return override
    root = data_dir()
    if root is not None:
        return root / "state"
    return None


def swe_index_dir() -> Path:
    """Return the local directory holding the live SWE FTS5 SQLite index.

    When ``SQWAKVOX_DATA_DIR`` is mounted (e.g. Cloud Run GCS FUSE), the live
    SQLite file is placed on fast local ``/tmp`` storage and snapshotted to
    ``<data>/state/index.db`` on writes rather than running live SQLite over FUSE.
    """
    override = _env_path("SQWAKVOX_SWE_INDEX_DIR")
    if override is not None:
        return override
    if data_dir() is not None:
        return Path(tempfile.gettempdir()) / "sqwakvox" / "swe"
    return Path.home() / ".sqwakvox" / "swe"


def docling_cache_dir() -> Path | None:
    """Return the directory for cached converted Docling slices, or None."""
    override = _env_path("SQWAKVOX_DOCLING_CACHE_DIR")
    if override is not None:
        return override
    root = data_dir()
    if root is not None:
        return root / "cache" / "docling"
    return None


def _file_sha256(path: Path) -> str:
    """Return SHA-256 hex digest of *path*, cached by ``(path, size, mtime_ns)``."""
    resolved = str(path.resolve())
    st = path.stat()
    cache_key = (resolved, st.st_size, st.st_mtime_ns)
    cached = _FILE_HASH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    hex_digest = digest.hexdigest()
    # Bound in-process hash cache size.
    if len(_FILE_HASH_CACHE) > 256:
        _FILE_HASH_CACHE.clear()
    _FILE_HASH_CACHE[cache_key] = hex_digest
    return hex_digest


def docling_cache_key(
    source_path: Path,
    *,
    domain_id: str,
    page_range: tuple[int, int] | None,
    skip_ocr: bool,
) -> str:
    """Build a deterministic content-addressed cache key for a Docling slice."""
    file_hash = _file_sha256(source_path)
    range_tag = f"{page_range[0]}-{page_range[1]}" if page_range is not None else "all"
    raw = f"{_DOCLING_CACHE_SCHEMA_VERSION}:{file_hash}:{domain_id}:{range_tag}:{int(skip_ocr)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_cached_document(cache_dir: Path, key: str) -> dict[str, Any] | None:
    """Load a gzipped JSON cached document dict for *key*, or None on miss/error."""
    path = cache_dir / key[:2] / f"{key}.json.gz"
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("Ignoring unreadable Docling cache entry %s: %s", path, exc)
        return None


def save_cached_document(cache_dir: Path, key: str, payload: dict[str, Any]) -> bool:
    """Persist a converted document dict as gzipped JSON under *cache_dir*."""
    path = cache_dir / key[:2] / f"{key}.json.gz"
    tmp_path = path.with_suffix(".gz.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        compressed = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        try:
            tmp_path.write_bytes(compressed)
            tmp_path.replace(path)
        except OSError:
            path.write_bytes(compressed)
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
        return True
    except Exception as exc:
        logger.warning("Failed to write Docling cache entry %s: %s", path, exc)
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        return False


def snapshot_sqlite(src: Path, dest: Path) -> bool:
    """Safely snapshot a live SQLite database at *src* to *dest*.

    Uses ``sqlite3.Connection.backup`` into a local temporary file first so
    *dest* (which may reside on a GCS FUSE mount) receives a single consistent,
    whole-file write without live SQLite locking on FUSE.
    """
    if not src.is_file():
        return False
    tmp_path: Path | None = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{src.name}.", suffix=".bak", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        src_con = sqlite3.connect(str(src))
        try:
            dst_con = sqlite3.connect(str(tmp_path))
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
        finally:
            src_con.close()

        # Copy whole snapshot file to destination (safe across filesystems & FUSE).
        shutil.copyfile(tmp_path, dest)
        return True
    except Exception as exc:
        logger.warning("Failed to snapshot SQLite DB %s -> %s: %s", src, dest, exc)
        return False
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def restore_sqlite(src: Path, dest: Path) -> bool:
    """Restore a snapshotted SQLite database from *src* to local *dest*."""
    if not src.is_file():
        return False
    tmp_path: Path | None = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".restore", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)
        shutil.copyfile(src, tmp_path)
        tmp_path.replace(dest)
        tmp_path = None
        return True
    except Exception as exc:
        logger.warning("Failed to restore SQLite DB %s -> %s: %s", src, dest, exc)
        return False
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
