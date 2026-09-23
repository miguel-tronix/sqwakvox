"""Tests for the retrieval MCP server tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from sqwakvox import mcp_retrieval_server as retrieval_mcp
from sqwakvox.domains.swe import retrieval


@pytest.fixture
def _index_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SQWAKVOX_SWE_INDEX_DIR", str(tmp_path))


def test_search_document_tool(_index_env: None) -> None:
    retrieval.index_document("book.epub", ["Chapter about factory patterns", "Unrelated text"])
    result = retrieval_mcp.search_document("book.epub", "factory pattern", k=5)
    assert "factory" in result
    assert "book.epub" in result or "chunk" in result


def test_index_info_tool(_index_env: None) -> None:
    retrieval.index_document("book.epub", ["a", "b", "c"])
    info = retrieval_mcp.index_info("book.epub")
    assert '"chunks": 3' in info
    assert '"chunks": 0' in retrieval_mcp.index_info("missing")


def test_list_indexed_documents_tool(_index_env: None) -> None:
    retrieval.index_document("a.md", ["x"])
    result = retrieval_mcp.list_indexed_documents()
    assert "a.md" in result


def test_search_missing_document_returns_empty(_index_env: None) -> None:
    result = retrieval_mcp.search_document("nope", "anything")
    assert "[]" in result


def test_search_k_clamped(_index_env: None) -> None:
    retrieval.index_document("book.epub", [f"chunk {i}" for i in range(100)])
    result = retrieval_mcp.search_document("book.epub", "chunk", k=10_000)
    # k is clamped to 50; json array of at most 50 objects
    assert result.count("chunk_id") <= 50


def test_search_query_clamped(_index_env: None) -> None:
    retrieval.index_document("book.epub", ["factory patterns"])
    long_q = "factory " * 5_000  # > 8000 chars
    result = retrieval_mcp.search_document("book.epub", long_q, k=5)
    assert isinstance(result, str)
