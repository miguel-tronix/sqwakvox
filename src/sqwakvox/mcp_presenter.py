"""MCP Presenter Server — the whole Sqwakvox session as MCP tools.

Every operation the Textual TUI can perform is published here as an MCP tool,
so *any* MCP client can drive Sqwakvox: the web presenter
(:mod:`sqwakvox.web_presenter`) calls these tools in-process, and external
agents (Hermes, Claude Code, Cursor, Antigravity) can drive the same session
over stdio.

Design notes
------------
* Tools are thin wrappers over :class:`sqwakvox.doc_session.DocSession` — the
  shared, view-agnostic session the TUI also uses, so behaviour cannot drift
  between surfaces.
* Long operations return a ``job_id``; pair with
  ``sqwakvox_presenter_wait_job`` / ``sqwakvox_presenter_cancel`` so a client
  never blocks on Docling.  ``*_sync`` variants exist where a single blocking
  call is more convenient.
* Long-running state changes are also available as an event stream
  (``sqwakvox_presenter_events``) for clients without an SSE channel.
* Tools return JSON strings so both language models and browsers can parse
  them; failures are reported in-band (never as an exception) so a client
  always gets a readable message.

Launch with::

    python -m sqwakvox.mcp_presenter                     # stdio
    SQWAKVOX_MCP_ALLOW_HTTP=1 SQWAKVOX_MCP_HTTP_TOKEN=... \
        python -m sqwakvox.mcp_presenter --transport http --port 8765
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any

from fastmcp import FastMCP

from sqwakvox.doc_session import DocSession, get_session
from sqwakvox.mcp_http import run_with_http_guard

logger = logging.getLogger(__name__)

mcp = FastMCP("sqwakvox-presenter")

#: Defaults for the blocking ``*_sync`` tools (seconds), clamped to this range.
DEFAULT_SYNC_TIMEOUT = 1800.0
_TIMEOUT_MIN, _TIMEOUT_MAX = 5.0, 3600.0

#: In-process rate limit for the blocking query tool (``SQWAKVOX_MCP_QUERY_RPM``).
_QUERY_RPM_DEFAULT = 60


def _session() -> DocSession:
    """The session these tools act on (injectable for tests)."""
    return get_session()


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _clamp_timeout(timeout: float) -> float:
    return max(_TIMEOUT_MIN, min(float(timeout), _TIMEOUT_MAX))


class _RateLimiter:
    """Sliding-window RPM guard (``SQWAKVOX_MCP_QUERY_RPM``)."""

    def __init__(self, default_rpm: int) -> None:
        self._default_rpm = default_rpm
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def allows(self) -> bool:
        try:
            rpm = int(os.environ.get("SQWAKVOX_MCP_QUERY_RPM", str(self._default_rpm)))
        except ValueError:
            rpm = self._default_rpm
        if rpm <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            while self._calls and self._calls[0] < now - 60.0:
                self._calls.popleft()
            if len(self._calls) >= rpm:
                return False
            self._calls.append(now)
        return True


_limiter = _RateLimiter(_QUERY_RPM_DEFAULT)


def _trace(tool_name: str, fn: Any) -> str:
    """Run *fn* inside a telemetry span, converting exceptions to text."""
    from sqwakvox.telemetry import get_telemetry, trace_span

    tm = get_telemetry()
    start = time.monotonic()
    with trace_span("sqwakvox.mcp.presenter", {"tool": tool_name}):
        try:
            result = fn()
        except Exception as exc:
            logger.error("Presenter tool %s failed: %s", tool_name, exc, exc_info=True)
            if tm.mcp_tool_counter:
                tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "failure"})
            return _json({"ok": False, "error": f"Tool '{tool_name}' failed: {exc}"})
    if tm.mcp_tool_counter:
        tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "success"})
    if tm.mcp_tool_duration:
        tm.mcp_tool_duration.record(time.monotonic() - start, {"tool": tool_name})
    return str(result)


# --------------------------------------------------------------------------- #
# Status and catalogs
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="sqwakvox_presenter_status",
    description=(
        "Snapshot of the Sqwakvox presenter: active document, its domain, "
        "loaded documents, running jobs, MCP servers and managed workers. "
        "Call this first to learn what is loaded."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_status() -> str:
    """Return the full presenter state."""

    def _run() -> str:
        from sqwakvox import session_registry

        session = _session()
        return _json(
            {
                "ok": True,
                "redis_available": session_registry.is_redis_available(),
                "session": session.snapshot(),
            }
        )

    return _trace("sqwakvox_presenter_status", _run)


@mcp.tool(
    name="sqwakvox_presenter_list_domains",
    description=(
        "List the available document assistants (agent expert types): "
        "'financial' and 'swe', with their display names and capabilities."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_list_domains() -> str:
    """List registered document domains."""

    def _run() -> str:
        return _json({"ok": True, "domains": _session().list_domains()})

    return _trace("sqwakvox_presenter_list_domains", _run)


@mcp.tool(
    name="sqwakvox_presenter_list_models",
    description=(
        "List selectable LLM model ids ('provider:model'), their friendly "
        "names, the environment variable holding their key, and whether that "
        "key is configured."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_list_models() -> str:
    """List selectable models."""

    def _run() -> str:
        return _json({"ok": True, "models": _session().list_models()})

    return _trace("sqwakvox_presenter_list_models", _run)


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="sqwakvox_presenter_list_documents",
    description=(
        "List every loaded document (one entry per tab) with its source, "
        "domain, table count, worker queue and page-slicing state."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_list_documents() -> str:
    """List loaded documents."""

    def _run() -> str:
        return _json({"ok": True, "documents": _session().documents()})

    return _trace("sqwakvox_presenter_list_documents", _run)


@mcp.tool(
    name="sqwakvox_presenter_open_document",
    description=(
        "Ingest a document (local PDF/EPUB/Markdown path, URL, or crawlable "
        "docs site) with Docling and make it the active document. Args: "
        "source (path or URL), domain_id ('financial' or 'swe', default "
        "'financial'), crawl (bool, SWE docs sites only). Returns a job_id "
        "immediately — poll with sqwakvox_presenter_wait_job."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
def sqwakvox_presenter_open_document(
    source: str,
    domain_id: str = "financial",
    crawl: bool = False,
) -> str:
    """Start ingesting a document (async)."""

    def _run() -> str:
        options = {"crawl": True} if crawl else None
        job = _session().open_document(source, domain_id, options)
        return _json({"ok": True, "job_id": job.job_id, "kind": job.kind, "source": source})

    return _trace("sqwakvox_presenter_open_document", _run)


@mcp.tool(
    name="sqwakvox_presenter_open_document_sync",
    description=(
        "Same as sqwakvox_presenter_open_document but blocks until the parse "
        "finishes (or timeout seconds elapse). Returns the document summary."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
def sqwakvox_presenter_open_document_sync(
    source: str,
    domain_id: str = "financial",
    crawl: bool = False,
    timeout: float = DEFAULT_SYNC_TIMEOUT,
) -> str:
    """Ingest a document and wait for it."""

    def _run() -> str:
        options = {"crawl": True} if crawl else None
        try:
            result = _session().open_document_and_wait(
                source, domain_id, options, timeout=_clamp_timeout(timeout)
            )
        except TimeoutError:
            return _json({"ok": False, "error": "Timed out waiting for the document to parse."})
        return _json(result)

    return _trace("sqwakvox_presenter_open_document_sync", _run)


@mcp.tool(
    name="sqwakvox_presenter_activate_document",
    description=(
        "Make a loaded document active (switch tabs). Args: source — the "
        "source string exactly as returned by "
        "sqwakvox_presenter_list_documents."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_activate_document(source: str) -> str:
    """Switch the active document."""

    def _run() -> str:
        ok = _session().activate(source)
        return _json(
            {
                "ok": ok,
                "error": None if ok else f"Document '{source}' is not loaded.",
                "active_document_name": _session().active_document_name(),
            }
        )

    return _trace("sqwakvox_presenter_activate_document", _run)


@mcp.tool(
    name="sqwakvox_presenter_close_document",
    description=(
        "Close a loaded document and cancel its running jobs. Args: source "
        "(optional; defaults to the active document)."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True},
)
def sqwakvox_presenter_close_document(source: str = "") -> str:
    """Unload a document."""

    def _run() -> str:
        ok = _session().close_document(source.strip() or None)
        return _json({"ok": ok, "error": None if ok else "No such loaded document."})

    return _trace("sqwakvox_presenter_close_document", _run)


@mcp.tool(
    name="sqwakvox_presenter_document",
    description=(
        "Full view model of a document: rendered text, structured tables, "
        "metadata and page-slicing state. Args: source (optional, defaults to "
        "the active document), include_text (bool, default true) — set false "
        "for a metadata-only reply."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_document(source: str = "", include_text: bool = True) -> str:
    """Return a document's content."""

    def _run() -> str:
        try:
            detail = _session().document_detail(source.strip() or None, include_text=include_text)
        except (KeyError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        return _json({"ok": True, "document": detail})

    return _trace("sqwakvox_presenter_document", _run)


@mcp.tool(
    name="sqwakvox_presenter_load_more",
    description=(
        "Append the next PDF page slice to a document loaded incrementally "
        "(large PDFs render slice by slice). Args: source (optional, defaults "
        "to the active document), timeout (seconds, default 600)."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
def sqwakvox_presenter_load_more(source: str = "", timeout: float = 600.0) -> str:
    """Load the next page slice (blocking)."""

    def _run() -> str:
        try:
            result = _session().load_more_and_wait(
                source.strip() or None, timeout=_clamp_timeout(timeout)
            )
        except (KeyError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        except TimeoutError:
            return _json({"ok": False, "error": "Timed out loading more pages."})
        return _json(result)

    return _trace("sqwakvox_presenter_load_more", _run)


@mcp.tool(
    name="sqwakvox_presenter_cross_validate",
    description=(
        "Run the financial cross-validation rule engine over a document's "
        "tables: recomputes labelled sums and reports mismatches. Args: "
        "source (optional, defaults to the active document), timeout."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_cross_validate(source: str = "", timeout: float = 300.0) -> str:
    """Cross-validate table numbers."""

    def _run() -> str:
        try:
            result = _session().cross_validate_and_wait(
                source.strip() or None, timeout=_clamp_timeout(timeout)
            )
        except (KeyError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        except TimeoutError:
            return _json({"ok": False, "error": "Timed out running cross-validation."})
        return _json(result)

    return _trace("sqwakvox_presenter_cross_validate", _run)


@mcp.tool(
    name="sqwakvox_presenter_cross_validate_async",
    description=(
        "Same as sqwakvox_presenter_cross_validate but returns a job_id "
        "immediately; collect the results with sqwakvox_presenter_wait_job. "
        "Args: source (optional, defaults to the active document)."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_cross_validate_async(source: str = "") -> str:
    """Cross-validate table numbers (async)."""

    def _run() -> str:
        try:
            job = _session().cross_validate(source.strip() or None)
        except (KeyError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        return _json({"ok": True, "job_id": job.job_id, "kind": job.kind})

    return _trace("sqwakvox_presenter_cross_validate_async", _run)


@mcp.tool(
    name="sqwakvox_presenter_data_store",
    description=(
        "Return the financial data store (extracted table metrics) recorded "
        "for a document. Args: source (optional, defaults to active)."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_data_store(source: str = "") -> str:
    """Return the financial data store."""

    def _run() -> str:
        store = _session().data_store(source.strip() or None)
        return _json({"ok": True, "data_store": store, "count": len(store)})

    return _trace("sqwakvox_presenter_data_store", _run)


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="sqwakvox_presenter_ask",
    description=(
        "Ask the active document's domain expert a question. Runs the "
        "domain post-parse step (data store / TOC index) then the agent with "
        "guardrails, PII redaction and the document's MCP tools attached. "
        "Args: query, model_id (optional, e.g. 'openai:gpt-5.5-high'), "
        "source (optional, defaults to the active document), api_key "
        "(optional; when omitted the worker resolves its own key from the "
        "environment). Returns a job_id immediately; collect the answer with "
        "sqwakvox_presenter_chat."
    ),
    annotations={"readOnlyHint": True, "idempotentHint": False},
)
def sqwakvox_presenter_ask(
    query: str,
    model_id: str = "",
    source: str = "",
    api_key: str = "",
) -> str:
    """Ask a question (async)."""

    def _run() -> str:
        if not _limiter.allows():
            return _json({"ok": False, "error": "Rate limit exceeded; retry shortly."})
        try:
            job = _session().ask(
                query,
                model_id or None,
                api_key or None,
                source.strip() or None,
            )
        except (KeyError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        return _json({"ok": True, "job_id": job.job_id, "kind": job.kind})

    return _trace("sqwakvox_presenter_ask", _run)


@mcp.tool(
    name="sqwakvox_presenter_ask_sync",
    description=(
        "Ask a question and block until the agent answers (or timeout "
        "seconds elapse). Returns the last chat messages including the "
        "agent's reply. Args: query, model_id (optional), source "
        "(optional), api_key (optional), timeout (default 1200, clamped "
        "5-3600)."
    ),
    annotations={"readOnlyHint": True, "idempotentHint": False},
)
def sqwakvox_presenter_ask_sync(
    query: str,
    model_id: str = "",
    source: str = "",
    api_key: str = "",
    timeout: float = 1200.0,
) -> str:
    """Ask a question and wait for the answer."""

    def _run() -> str:
        if not _limiter.allows():
            return _json({"ok": False, "error": "Rate limit exceeded; retry shortly."})
        target = source.strip() or None
        session = _session()
        before = len(session.chat(target))
        try:
            session.wait_job(
                session.ask(query, model_id or None, api_key or None, target).job_id,
                timeout=_clamp_timeout(timeout),
            )
        except (KeyError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        except TimeoutError:
            return _json(
                {
                    "ok": False,
                    "error": "Timed out waiting for the agent.",
                    "messages": session.chat(target)[before:],
                }
            )
        messages = session.chat(target)[before:]
        return _json({"ok": True, "messages": messages, "answer": _answer_of(messages)})

    return _trace("sqwakvox_presenter_ask_sync", _run)


def _answer_of(messages: list[dict[str, Any]]) -> str:
    """The last agent message in *messages*, or an empty string."""
    for message in reversed(messages):
        if message.get("role") == "agent":
            return str(message.get("text", ""))
    return ""


@mcp.tool(
    name="sqwakvox_presenter_chat",
    description=(
        "Return the chat transcript of a document as [{role, text, ts}] "
        "entries. Args: source (optional, defaults to the active document)."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_chat(source: str = "") -> str:
    """Read the chat transcript."""

    def _run() -> str:
        messages = _session().chat(source.strip() or None)
        return _json({"ok": True, "messages": messages, "answer": _answer_of(messages)})

    return _trace("sqwakvox_presenter_chat", _run)


@mcp.tool(
    name="sqwakvox_presenter_clear_chat",
    description="Clear the chat transcript of a document. Args: source (optional).",
    annotations={"readOnlyHint": False, "destructiveHint": True},
)
def sqwakvox_presenter_clear_chat(source: str = "") -> str:
    """Clear a chat transcript."""

    def _run() -> str:
        ok = _session().clear_chat(source.strip() or None)
        return _json({"ok": ok, "error": None if ok else "No such loaded document."})

    return _trace("sqwakvox_presenter_clear_chat", _run)


# --------------------------------------------------------------------------- #
# Jobs and events
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="sqwakvox_presenter_jobs",
    description="List background jobs (open_document, ask, load_more, ...) and their states.",
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_jobs() -> str:
    """List background jobs."""

    def _run() -> str:
        return _json({"ok": True, "jobs": _session().jobs()})

    return _trace("sqwakvox_presenter_jobs", _run)


@mcp.tool(
    name="sqwakvox_presenter_wait_job",
    description=(
        "Block until a job reaches a terminal state and return its result. "
        "Args: job_id, timeout (seconds, default 1800, clamped 5-3600)."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_wait_job(job_id: str, timeout: float = DEFAULT_SYNC_TIMEOUT) -> str:
    """Wait for a background job."""

    def _run() -> str:
        try:
            return _json(_session().wait_job(job_id, timeout=_clamp_timeout(timeout)))
        except (KeyError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        except TimeoutError:
            job = _session().job(job_id)
            return _json(
                {
                    "ok": False,
                    "error": f"Timed out waiting for job {job_id}.",
                    "job": job.as_dict() if job else None,
                }
            )
        except RuntimeError as exc:
            return _json({"ok": False, "error": str(exc)})

    return _trace("sqwakvox_presenter_wait_job", _run)


@mcp.tool(
    name="sqwakvox_presenter_cancel",
    description="Cancel a running background job (parse, agent query, ...). Args: job_id.",
    annotations={"readOnlyHint": False, "destructiveHint": True},
)
def sqwakvox_presenter_cancel(job_id: str) -> str:
    """Cancel a background job."""

    def _run() -> str:
        ok = _session().cancel(job_id)
        return _json({"ok": ok, "error": None if ok else f"Unknown job id: {job_id}"})

    return _trace("sqwakvox_presenter_cancel", _run)


@mcp.tool(
    name="sqwakvox_presenter_events",
    description=(
        "Poll the presenter event stream — progress, document, chat, job and "
        "error events. Args: since_seq (return events after this sequence "
        "number; 0 replays the buffer), limit (default 200). Use "
        "sqwakvox_presenter_status's 'seq' as a resume cursor."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_events(since_seq: int = 0, limit: int = 200) -> str:
    """Replay session events."""

    def _run() -> str:
        events = _session().events_since(int(since_seq), limit=min(int(limit), 1000))
        return _json(
            {
                "ok": True,
                "events": [e.as_dict() for e in events],
                "seq": events[-1].seq if events else int(since_seq),
            }
        )

    return _trace("sqwakvox_presenter_events", _run)


# --------------------------------------------------------------------------- #
# Skills and retrieval
# --------------------------------------------------------------------------- #


@mcp.tool(
    name="sqwakvox_presenter_list_skills",
    description=(
        "List reusable skills stored for a document domain (the SWE assistant "
        "authors these during chat). Args: domain_id (optional, defaults to "
        "the active document's domain)."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_list_skills(domain_id: str = "") -> str:
    """List stored skills."""

    def _run() -> str:
        return _json({"ok": True, "skills": _session().list_skills(domain_id.strip() or None)})

    return _trace("sqwakvox_presenter_list_skills", _run)


@mcp.tool(
    name="sqwakvox_presenter_search_chunks",
    description=(
        "Search the SQLite FTS5 index of ingested SWE documents (book-scale "
        "retrieval instead of reading the whole document). Args: query, "
        "doc_id (optional), k (default 5, clamped 1-50)."
    ),
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_search_chunks(query: str, doc_id: str = "", k: int = 5) -> str:
    """Search indexed document chunks."""

    def _run() -> str:
        chunks = _session().search_chunks(query, doc_id, k)
        return _json({"ok": True, "chunks": chunks, "count": len(chunks)})

    return _trace("sqwakvox_presenter_search_chunks", _run)


@mcp.tool(
    name="sqwakvox_presenter_indexed_documents",
    description="List documents available in the SWE retrieval index with chunk counts.",
    annotations={"readOnlyHint": True},
)
def sqwakvox_presenter_indexed_documents() -> str:
    """List indexed documents."""

    def _run() -> str:
        return _json({"ok": True, "documents": _session().indexed_documents()})

    return _trace("sqwakvox_presenter_indexed_documents", _run)


# --------------------------------------------------------------------------- #
# Entry-point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the presenter MCP server (stdio by default; HTTP needs opt-in)."""
    import argparse

    parser = argparse.ArgumentParser(description="Sqwakvox presenter MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport to use (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for sse/http transport")
    parser.add_argument("--port", type=int, default=8765, help="Port for sse/http transport")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    run_with_http_guard(mcp, args, server_name="presenter")


if __name__ == "__main__":
    main()
