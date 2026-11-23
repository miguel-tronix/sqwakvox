"""Chunked retrieval for large SWE documents.

Long books (Fowler-scale) don't fit a single agent context.  We index each
parsed document as heading-aware chunks in a local SQLite **FTS5** table
(BM25 ranking) and expose search through the retrieval MCP server
(:mod:`sqwakvox.mcp_retrieval_server`).

The index lives at ``~/.sqwakvox/swe/index.db`` (override with
``SQWAKVOX_SWE_INDEX_DIR``).  One SQLite file, no new infra.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from sqwakvox import storage
from sqwakvox.models import StructuredDocument

#: Chunk targets (characters).  ~3k chars ≈ 750 tokens — comfortably under
#: most context windows even with several chunks per query.
MAX_CHUNK_CHARS = 3000
CHUNK_OVERLAP = 120
MIN_CHUNK_CHARS = 200

#: How many chunks (plus the TOC) go into the agent context for large docs.
CONTEXT_CHUNKS = 3


def index_dir() -> Path:
    """Directory holding the SQLite retrieval index (env-overridable)."""
    return storage.swe_index_dir()


def index_path() -> Path:
    """Path to the SQLite retrieval index."""
    return index_dir() / "index.db"


def _snapshot_path() -> Path | None:
    snap_dir = storage.state_snapshot_dir()
    return (snap_dir / "index.db") if snap_dir is not None else None


def _persist_snapshot() -> None:
    dest = _snapshot_path()
    if dest is not None:
        storage.snapshot_sqlite(index_path(), dest)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def chunk_markdown(
    md: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Split markdown into heading-aware chunks.

    Sections are cut at ``#``-heading lines; small sections are greedily
    merged up to *max_chars*; a single oversized section is hard-split with
    *overlap* so no content is lost between chunk boundaries.
    """
    if not md:
        return []
    lines = md.splitlines()

    # Split into (heading, body) sections at heading lines.
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    body: list[str] = []
    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            if heading or body:
                sections.append((heading, body))
            heading = line
            body = []
        else:
            body.append(line)
    if heading or body:
        sections.append((heading, body))

    chunks: list[str] = []
    buf: list[str] = []
    buf_size = 0

    def flush() -> None:
        nonlocal buf, buf_size
        text = "\n".join(buf).strip()
        if text:
            chunks.append(text)
        buf, buf_size = [], 0

    for sec_heading, sec_body in sections:
        section_text = ("\n".join([sec_heading, *sec_body])).strip()
        if not section_text:
            continue
        if buf and buf_size + len(section_text) + 1 > max_chars:
            flush()
        if len(section_text) <= max_chars:
            buf.append(section_text)
            buf_size += len(section_text) + 1
        else:
            flush()
            step = max_chars - overlap
            for start in range(0, len(section_text), step):
                chunk = section_text[start : start + max_chars].strip()
                if len(chunk) >= MIN_CHUNK_CHARS:
                    chunks.append(chunk)
    flush()
    return chunks


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #


def _connect() -> sqlite3.Connection:
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        snap = _snapshot_path()
        if snap is not None and snap.is_file():
            storage.restore_sqlite(snap, path)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks USING fts5("
        "doc_id UNINDEXED, chunk_id UNINDEXED, content)"
    )
    return con


def index_document(doc_id: str, chunks: list[str]) -> int:
    """Replace *doc_id*'s chunks in the index; returns the chunk count."""
    con = _connect()
    try:
        con.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        for idx, chunk in enumerate(chunks):
            con.execute(
                "INSERT INTO doc_chunks (doc_id, chunk_id, content) VALUES (?, ?, ?)",
                (doc_id, f"{idx:06d}", chunk),
            )
        con.commit()
    finally:
        con.close()
    _persist_snapshot()
    return len(chunks)


def delete_document(doc_id: str) -> None:
    con = _connect()
    try:
        con.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        con.commit()
    finally:
        con.close()
    _persist_snapshot()


def count_chunks(doc_id: str) -> int:
    con = _connect()
    try:
        row = con.execute("SELECT COUNT(*) FROM doc_chunks WHERE doc_id = ?", (doc_id,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        con.close()


def list_documents() -> list[tuple[str, int]]:
    """All indexed doc ids with their chunk counts."""
    con = _connect()
    try:
        return [
            (str(doc), int(n))
            for doc, n in con.execute(
                "SELECT doc_id, COUNT(*) AS n FROM doc_chunks GROUP BY doc_id"
            ).fetchall()
        ]
    finally:
        con.close()


def search_document(
    doc_id: str,
    query: str,
    k: int = 5,
) -> list[dict[str, Any]]:
    """BM25 search over *doc_id*'s chunks; returns top-k ``{chunk_id, content}``.

    Terms are ANDed first; if nothing matches (e.g. a pluralised word), the
    search retries with OR so a single matching term still returns results.
    """
    terms = [f'"{t}"' for t in re.findall(r"[\w\-]+", query) if len(t) > 1]
    if not terms:
        return []
    con = _connect()
    try:
        for joiner in (" AND ", " OR "):
            match_query = joiner.join(terms)
            try:
                rows = con.execute(
                    "SELECT chunk_id, content, rank FROM doc_chunks "
                    "WHERE doc_chunks MATCH ? AND doc_id = ? "
                    "ORDER BY rank LIMIT ?",
                    (match_query, doc_id, k),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            if rows:
                break
        else:
            # FTS5 syntax edge cases — fall back to plain LIKE.
            like = "%" + re.sub(r"[\W_]+", "%", query) + "%"
            rows = con.execute(
                "SELECT chunk_id, content, 0.0 FROM doc_chunks "
                "WHERE doc_id = ? AND content LIKE ? LIMIT ?",
                (doc_id, like, k),
            ).fetchall()
    finally:
        con.close()
    return [
        {"chunk_id": str(cid), "content": content, "rank": float(rank)}
        for cid, content, rank in rows
    ]


# --------------------------------------------------------------------------- #
# Context assembly (the ``build_context`` hook for the SWE domain)
# --------------------------------------------------------------------------- #


def build_retrieval_context(doc: StructuredDocument, max_chars: int = MAX_CHUNK_CHARS) -> str:
    """Context for large SWE docs: TOC + first chunks + search instructions.

    Small documents return the full raw markdown (same as the base domain).
    """
    md = doc.raw_markdown or ""
    needs_retrieval = bool(doc.metadata.get("needs_retrieval"))
    if not needs_retrieval or len(md) <= MAX_CHUNK_CHARS:
        return md

    toc = doc.metadata.get("toc") or []
    toc_lines = []
    for item in (toc if isinstance(toc, list) else [])[:60]:
        if isinstance(item, dict):
            level = int(item.get("level", 1))
            title = str(item.get("title", ""))
        else:
            level, title = 1, str(item)
        toc_lines.append(f"{'  ' * (level - 1)}- {title}")

    chunks = chunk_markdown(md, max_chars=max_chars)[:CONTEXT_CHUNKS]

    parts = [
        f"# {doc.file_name}",
        "",
        "This is a LARGE document. Only its table of contents and first",
        "sections are included below. To answer questions, use the",
        "`search_document` MCP tool with",
        f'document_id = "{doc.file_name}"',
        "to retrieve the relevant sections before answering.",
        "",
        "## Table of Contents",
        "\n".join(toc_lines),
        "",
        "## First sections",
        "\n\n".join(chunks),
    ]
    return "\n".join(parts)
