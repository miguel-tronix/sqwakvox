"""MCP Gateway Server for Sqwakvox.

Exposes Sqwakvox's document intelligence, domain assistants (Financial, SWE),
retrieval indices, and guardrail pipelines as standard Model Context Protocol (MCP)
tools. External agents (such as Hermes-agent, Claude Code, Cursor, Antigravity)
can call these tools to query documents, inspect tables, and search book chunks.

Launch with:
    python -m sqwakvox.mcp_gateway
"""

from __future__ import annotations

import logging
import os
import time

from fastmcp import FastMCP

from sqwakvox import session_registry
from sqwakvox.backend.celery_app import celery_app
from sqwakvox.models import ModelProvider

logger = logging.getLogger(__name__)

mcp = FastMCP("sqwakvox")

DEFAULT_MODEL_ID = "openai:gpt-5.5-high"


def _trace_tool(tool_name: str, fn: object) -> str:
    """Record tool usage in telemetry, then run *fn*."""
    from sqwakvox.telemetry import get_telemetry, trace_span

    tm = get_telemetry()
    start = time.monotonic()
    with trace_span("sqwakvox.mcp.gateway", {"tool": tool_name}):
        try:
            result = fn()  # type: ignore[operator]
        except Exception as exc:
            logger.error("Gateway tool %s failed: %s", tool_name, exc, exc_info=True)
            if tm.mcp_tool_counter:
                tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "failure"})
            return f"Error: {exc}"
    if tm.mcp_tool_counter:
        tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "success"})
    if tm.mcp_tool_duration:
        tm.mcp_tool_duration.record(time.monotonic() - start, {"tool": tool_name})
    return result  # type: ignore[no-any-return]


@mcp.tool(
    name="sqwakvox_status",
    description=(
        "Check Sqwakvox connection status, active session, and loaded document. "
        "Returns whether Redis is reachable and the active document name and domain."
    ),
)
def sqwakvox_status() -> str:
    """Return status of the Sqwakvox session and backend."""

    def _run() -> str:
        redis_ok = session_registry.is_redis_available()
        if not redis_ok:
            return (
                "Sqwakvox Status: Redis is not reachable at the configured URL.\n"
                "Please ensure redis-server is running."
            )

        active = session_registry.get_active_session()
        docs = session_registry.list_active_documents()

        lines = ["Sqwakvox Status: Connected to Redis bus."]
        if active:
            lines.append(f"• Active Document: {active.get('active_document_name')}")
            lines.append(f"• Domain: {active.get('domain_id')}")
            lines.append(f"• Worker Queue: {active.get('queue')}")
            lines.append(f"• Tables: {active.get('table_count', 0)}")
            lines.append(f"• Open Documents: {len(docs)}")
        else:
            lines.append("• Active Document: None (TUI is not currently displaying a document).")
            if docs:
                lines.append(f"• Open Documents in Background: {len(docs)}")
            else:
                lines.append("• No documents are currently loaded.")
        return "\n".join(lines)

    return _trace_tool("sqwakvox_status", _run)


@mcp.tool(
    name="sqwakvox_list_documents",
    description="List all documents currently open in Sqwakvox, their domains, and table counts.",
)
def sqwakvox_list_documents() -> str:
    """List loaded documents recorded in the session registry."""

    def _run() -> str:
        docs = session_registry.list_active_documents()
        if not docs:
            active = session_registry.get_active_session()
            if active:
                docs = [active]
        if not docs:
            return "No documents are currently loaded in Sqwakvox."

        lines = [f"Found {len(docs)} loaded document(s) in Sqwakvox:"]
        for idx, doc in enumerate(docs, 1):
            name = doc.get("file_name") or doc.get("active_document_name", "unknown")
            domain = doc.get("domain_id", "unknown")
            tables = doc.get("table_count", 0)
            queue = doc.get("queue", "default")
            lines.append(f"{idx}. {name} [Domain: {domain}, Tables: {tables}, Queue: {queue}]")
        return "\n".join(lines)

    return _trace_tool("sqwakvox_list_documents", _run)


def _resolve_model_and_key(preferred_model: str | None = None) -> tuple[str, str]:
    """Resolve the model ID and API key from environment variables."""
    model_id = preferred_model or DEFAULT_MODEL_ID
    env_var = ModelProvider.get_env_var(model_id)
    api_key = os.environ.get(env_var, "").strip()

    if not api_key:
        for alt_var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            val = os.environ.get(alt_var, "").strip()
            if val:
                api_key = val
                if alt_var == "OPENAI_API_KEY":
                    model_id = "openai:gpt-5.5-high"
                elif alt_var == "ANTHROPIC_API_KEY":
                    model_id = "anthropic:claude-4.6"
                elif alt_var == "GEMINI_API_KEY":
                    model_id = "gemini:gemini-3.5-flash"
                break
    return model_id, api_key


@mcp.tool(
    name="sqwakvox_query",
    description=(
        "Query the active document in Sqwakvox using its domain-specific expert "
        "(financial analysis with table cross-validation, or SWE engineering reference). "
        "Args: query (user question), doc_name (optional, specify a particular document), "
        "timeout (seconds, default 120). Guardrails and validation are enforced."
    ),
)
def sqwakvox_query(
    query: str,
    doc_name: str = "",
    timeout: int = 120,
) -> str:
    """Send a query to Sqwakvox's agent and await the response."""

    def _run() -> str:
        target_name = doc_name.strip() if doc_name else None
        payload = session_registry.get_document_payload(target_name)

        if not payload:
            return (
                "Error: No active document payload found in Sqwakvox session registry. "
                "Please ensure Sqwakvox has a document open."
            )

        active_doc_name = payload.get("active_document_name", "unknown")
        doc_context = payload.get("doc_context", "")
        data_store = payload.get("data_store", {})
        domain_id = payload.get("domain_id", "financial")
        queue = payload.get("queue") or "sqwakvox"
        thread_id = payload.get("thread_id") or active_doc_name

        # Resolve API key and model ID
        model_id, api_key = _resolve_model_and_key(payload.get("model_id"))

        if not api_key:
            env_var = ModelProvider.get_env_var(model_id)
            return (
                f"Error: API key for model '{model_id}' ({env_var}) is not set in environment."
            )

        # Dispatch Celery task to the tab's worker queue
        async_result = celery_app.send_task(
            "sqwakvox.backend.tasks.execute_agent",
            args=[
                model_id,
                api_key,
                query,
                doc_context,
                active_doc_name,
                data_store,
                None,  # mcp_servers
                thread_id,
                domain_id,
            ],
            queue=queue,
        )

        try:
            raw_res = async_result.get(timeout=timeout)
        except Exception as exc:
            return f"Error waiting for Sqwakvox agent: {exc}"

        if not raw_res:
            return "Error: Sqwakvox agent returned an empty response."

        res_dict = raw_res if isinstance(raw_res, dict) else getattr(raw_res, "__dict__", {})

        if res_dict.get("is_blocked"):
            reason = res_dict.get("blocked_reason", "Security / guardrail violation")
            return f"[Blocked by Sqwakvox Guardrails]: {reason}"

        if not res_dict.get("success", True) and res_dict.get("error_message"):
            return f"Error from Sqwakvox agent: {res_dict['error_message']}"

        response_text = str(res_dict.get("response") or "")

        # Append math discrepancies if financial validation identified issues
        warnings = res_dict.get("math_discrepancies") or []
        if warnings:
            warn_str = "\n".join(f"⚠️ {w}" for w in warnings)
            response_text = f"{response_text}\n\n[Financial Cross-Validation Warnings]:\n{warn_str}"

        return response_text

    return _trace_tool("sqwakvox_query", _run)


@mcp.tool(
    name="sqwakvox_get_tables",
    description="Retrieve extracted tables and financial metrics for the active document.",
)
def sqwakvox_get_tables(doc_name: str = "") -> str:
    """Return raw data store tables extracted by Docling."""

    def _run() -> str:
        target_name = doc_name.strip() if doc_name else None
        payload = session_registry.get_document_payload(target_name)
        if not payload:
            return "Error: No active document found in Sqwakvox."

        data_store = payload.get("data_store", {})
        active_name = payload.get("active_document_name", "unknown")
        if not data_store:
            return f"No financial table metrics stored for document '{active_name}'."

        lines = [f"Financial data store for '{active_name}':"]
        for key, val in sorted(data_store.items()):
            lines.append(f"• {key}: {val}")
        return "\n".join(lines)

    return _trace_tool("sqwakvox_get_tables", _run)


@mcp.tool(
    name="sqwakvox_search_swe_chunks",
    description=(
        "Search SQLite FTS5 index of ingested software engineering (SWE) books. "
        "Args: query (keywords), doc_id (optional document name), k (max chunks, default 5)."
    ),
)
def sqwakvox_search_swe_chunks(query: str, doc_id: str = "", k: int = 5) -> str:
    """Search chunks in SQLite FTS5 index for SWE documents."""

    def _run() -> str:
        from sqwakvox.domains.swe import retrieval

        target_doc = doc_id.strip()
        if not target_doc:
            active = session_registry.get_active_session()
            if active and active.get("domain_id") == "swe":
                target_doc = active.get("active_document_name", "")

        if not target_doc:
            # Check available indexed documents
            indexed = retrieval.list_documents()
            if not indexed:
                return "No SWE documents have been indexed in SQLite FTS5 yet."
            target_doc = indexed[0][0]

        chunks = retrieval.search_document(target_doc, query, k=k)
        if not chunks:
            return f"No matching chunks found in '{target_doc}' for query: '{query}'."

        results = [f"Found {len(chunks)} chunk(s) in '{target_doc}':"]
        for idx, chunk in enumerate(chunks, 1):
            cid = chunk.get("chunk_id", f"chunk_{idx}")
            content = chunk.get("content", "").strip()
            results.append(f"\n--- [{cid}] ---\n{content}")
        return "\n".join(results)

    return _trace_tool("sqwakvox_search_swe_chunks", _run)


@mcp.tool(
    name="sqwakvox_list_skills",
    description="List reusable engineering skills currently stored in Sqwakvox.",
)
def sqwakvox_list_skills() -> str:
    """List SWE skills available in Sqwakvox."""

    def _run() -> str:
        from sqwakvox.domains.swe import skills as skills_store

        skills = skills_store.list_skills("swe")
        if not skills:
            return "No skills currently stored in Sqwakvox."

        lines = [f"Found {len(skills)} skill(s) in Sqwakvox:"]
        for s in skills:
            lines.append(f"• {s['name']}: {s['description']}")
        return "\n".join(lines)

    return _trace_tool("sqwakvox_list_skills", _run)


def main() -> None:
    """Run the MCP Gateway server over stdio."""
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
