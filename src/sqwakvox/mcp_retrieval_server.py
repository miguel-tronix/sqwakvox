"""MCP Server: chunked retrieval for large SWE documents.

Lets the agent search a parsed document's sections (BM25 over a local SQLite
FTS5 index — see :mod:`sqwakvox.domains.swe.retrieval`) instead of having the
whole book stuffed into its context.

Launch with:
    python -m sqwakvox.mcp_retrieval_server
"""

from __future__ import annotations

import json
import logging
import time

from fastmcp import FastMCP

from sqwakvox.domains.swe import retrieval
from sqwakvox.mcp_http import run_with_http_guard

logger = logging.getLogger(__name__)

mcp = FastMCP("sqwakvox-retrieval")


def _trace_tool(tool_name: str, fn: object) -> str:
    """Record tool usage in telemetry, then run *fn*."""
    from sqwakvox.telemetry import get_telemetry, trace_span

    tm = get_telemetry()
    start = time.monotonic()
    with trace_span("sqwakvox.mcp.retrieval", {"tool": tool_name}):
        try:
            result = fn()  # type: ignore[operator]
        except Exception as exc:  # tools must not crash the server
            logger.error("Retrieval tool %s failed: %s", tool_name, exc, exc_info=True)
            if tm.mcp_tool_counter:
                tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "failure"})
            return f"Error: tool '{tool_name}' failed. Check server logs for details."
    if tm.mcp_tool_counter:
        tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "success"})
    if tm.mcp_tool_duration:
        tm.mcp_tool_duration.record(time.monotonic() - start, {"tool": tool_name})
    return result  # type: ignore[no-any-return]


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


_QUERY_MAX_CHARS = 8_000
_K_MIN, _K_MAX = 1, 50


@mcp.tool(
    name="search_document",
    description=(
        "Search the sections of a loaded SWE document. Args: document_id (the "
        "file name shown in the document context, e.g. 'Refactoring.epub'), "
        "query (free text), k (max results, default 5, clamped 1-50). Returns "
        "matching chunks of the document. Use this for large documents whose "
        "full content is not in the context."
    ),
    annotations={"readOnlyHint": True},
)
def search_document(document_id: str, query: str, k: int = 5) -> str:
    """Return top-k matching chunks of *document_id* for *query*."""

    def _run() -> str:
        safe_query = query[:_QUERY_MAX_CHARS]
        safe_k = _clamp_int(k, _K_MIN, _K_MAX)
        results = retrieval.search_document(document_id, safe_query, k=safe_k)
        return json.dumps(results, indent=2)

    return _trace_tool("search_document", _run)


@mcp.tool(
    name="index_info",
    description=(
        "Report how many chunks of a document are indexed (0 = not indexed / "
        "not found). Args: document_id."
    ),
    annotations={"readOnlyHint": True},
)
def index_info(document_id: str) -> str:
    """Return the chunk count for *document_id*."""

    def _run() -> str:
        count = retrieval.count_chunks(document_id)
        return json.dumps({"document_id": document_id, "chunks": count})

    return _trace_tool("index_info", _run)


@mcp.tool(
    name="list_indexed_documents",
    description="List every indexed document with its chunk count.",
    annotations={"readOnlyHint": True},
)
def list_indexed_documents() -> str:
    """Return the list of indexed documents."""

    def _run() -> str:
        docs = [
            {"document_id": doc_id, "chunks": count} for doc_id, count in retrieval.list_documents()
        ]
        return json.dumps(docs, indent=2)

    return _trace_tool("list_indexed_documents", _run)


# --------------------------------------------------------------------------- #
# Entry-point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the retrieval MCP server (default: stdio; HTTP needs opt-in)."""
    import argparse

    parser = argparse.ArgumentParser(description="Sqwakvox retrieval MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport to use (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for sse/http transport")
    parser.add_argument("--port", type=int, default=8000, help="Port for sse/http transport")
    args = parser.parse_args()

    run_with_http_guard(mcp, args, server_name="retrieval")


if __name__ == "__main__":
    main()
