"""ACP Presenter — expose Sqwakvox as an Agent Client Protocol (ACP) agent.

Speaks JSON-RPC 2.0 over stdio using the official ``agent-client-protocol``
Python SDK so ACP-compatible presenters (such as Orca or Zed) can launch and
drive Sqwakvox natively.

Architecture
------------
Sits at Layer L12 alongside :mod:`sqwakvox.mcp_presenter` and
:mod:`sqwakvox.web_presenter`, delegating all document ingestion, incremental
PDF paging/seek, chat persistence, worker management, and live event streaming
to :class:`sqwakvox.doc_session.DocSession`.

Stdio discipline
----------------
Because ACP transports JSON-RPC frames on ``stdout``, all logging is directed
strictly to ``stderr`` and this module never writes directly to ``stdout``.

Launch with::

    sqwakvox-acp
    uv run sqwakvox-acp
    python -m sqwakvox.acp_presenter --domain swe --model openai:gpt-5.5-high
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import logging
import shlex
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlparse

from acp import (
    PROTOCOL_VERSION,
    Agent,
    Client,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    RequestError,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    run_agent,
)
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AudioContentBlock,
    AvailableCommand,
    AvailableCommandInput,
    AvailableCommandsUpdate,
    ClientCapabilities,
    CloseSessionResponse,
    ConfigOptionUpdate,
    ContentToolCallContent,
    CurrentModeUpdate,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    ListSessionsResponse,
    McpServerStdio,
    PromptCapabilities,
    ResourceContentBlock,
    SessionCapabilities,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SseMcpServer,
    TextContentBlock,
    TextResourceContents,
    ToolCallProgress,
    ToolCallStart,
    UnstructuredCommandInput,
    UserMessageChunk,
)

from sqwakvox.doc_session import (
    EVENT_CHAT,
    EVENT_ERROR,
    EVENT_LOG,
    EVENT_PROGRESS,
    DocSession,
    Job,
    SessionEvent,
    get_session,
)

logger = logging.getLogger(__name__)

PromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
McpServerSpec = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio
ConfigOptionItem = SessionConfigOptionSelect | SessionConfigOptionBoolean
ToolKind = Literal[
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
]
ToolStatus = Literal["pending", "in_progress", "completed", "failed"]


def _package_version() -> str:
    try:
        return importlib.metadata.version("sqwakvox")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _available_commands() -> list[AvailableCommand]:
    """Slash commands published to ACP clients via ``available_commands_update``."""

    def _cmd(name: str, description: str, hint: str | None = None) -> AvailableCommand:
        cmd_input = (
            AvailableCommandInput(root=UnstructuredCommandInput(hint=hint))
            if hint is not None
            else None
        )
        return AvailableCommand(name=name, description=description, input=cmd_input)

    return [
        _cmd(
            "open",
            "Ingest and open a document (PDF, EPUB, Markdown, or URL)",
            "<path-or-url> [financial|swe]",
        ),
        _cmd("docs", "List open documents, domains, and PDF page ranges"),
        _cmd("switch", "Switch the active document tab", "<index-or-source>"),
        _cmd("close", "Close the active (or specified) document", "[source]"),
        _cmd("page", "Seek the active PDF to a specific page", "<page-number>"),
        _cmd("more", "Load the next (down) or earlier (up) PDF page slice", "[down|up]"),
        _cmd("validate", "Run financial numerical cross-validation on the active document"),
        _cmd("search", "Search SWE FTS5 indexed chunks", "<query>"),
        _cmd("skills", "List stored SWE skills"),
        _cmd("clear", "Clear chat history for the active document"),
        _cmd("status", "Show active document, model, domain, running jobs, and workers"),
    ]


def _resolve_source(raw: str, cwd: str) -> str:
    """Normalise a file URI, URL, or relative path against the session's *cwd*."""
    value = raw.strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("file://"):
        parsed = urlparse(value)
        value = unquote(parsed.path)
    path = Path(value).expanduser()
    if not path.is_absolute() and cwd:
        candidate = Path(cwd) / path
        if candidate.exists():
            return str(candidate.resolve())
    return str(path.resolve()) if path.exists() else value


def _extract_prompt_parts(blocks: list[PromptBlock], cwd: str) -> tuple[list[str], str]:
    """Split ACP prompt blocks into ``(attached_sources, combined_text)``."""
    sources: list[str] = []
    texts: list[str] = []
    for block in blocks:
        if isinstance(block, TextContentBlock):
            if block.text.strip():
                texts.append(block.text)
        elif isinstance(block, ResourceContentBlock):
            resolved = _resolve_source(block.uri, cwd)
            if resolved and resolved not in sources:
                sources.append(resolved)
        elif isinstance(block, EmbeddedResourceContentBlock):
            resource = block.resource
            uri = getattr(resource, "uri", "")
            resolved = _resolve_source(uri, cwd) if uri else ""
            if resolved and Path(resolved).is_file():
                if resolved not in sources:
                    sources.append(resolved)
            elif isinstance(resource, TextResourceContents) and resource.text.strip():
                label = uri or "attached-resource"
                texts.append(f"[Attached {label}]\n{resource.text}")
    return sources, "\n\n".join(texts).strip()


def _format_page_range(page_info: dict[str, Any]) -> str:
    if not page_info.get("paged"):
        return ""
    start = page_info.get("start_page", 1)
    end = page_info.get("end_page") or page_info.get("rendered_pages", "?")
    total = page_info.get("total_pages")
    return f"pages {start}-{end}/{total}" if total else f"pages {start}-{end}"


def _format_docs(docs: list[dict[str, Any]]) -> str:
    if not docs:
        return "No documents open. Use `/open <path-or-url>` or attach a file to get started."
    lines = ["### Open Documents", ""]
    for idx, doc in enumerate(docs, start=1):
        marker = "**(active)** " if doc.get("active") else ""
        page_str = _format_page_range(doc.get("page_range") or {})
        extra = f", {page_str}" if page_str else ""
        lines.append(
            f"{idx}. {marker}`{doc['file_name']}` — domain: `{doc['domain_id']}`, "
            f"{doc.get('table_count', 0)} table(s), {doc.get('char_count', 0)} chars{extra}\n"
            f"   `{doc['source']}`"
        )
    return "\n".join(lines)


def _format_status(snapshot: dict[str, Any], domain_id: str) -> str:
    active = snapshot.get("active_document_name") or "(none)"
    docs = snapshot.get("documents") or []
    jobs = snapshot.get("jobs") or []
    workers = snapshot.get("workers") or []
    mcp_servers = [s.get("name", "?") for s in (snapshot.get("mcp_servers") or [])]
    lines = [
        "### Sqwakvox Session Status",
        f"- **Active document:** `{active}`",
        f"- **Default expert domain:** `{domain_id}`",
        f"- **Active document domain:** `{snapshot.get('active_domain_id', domain_id)}`",
        f"- **Model:** `{snapshot.get('model_id', '')}`",
        f"- **Open documents:** {len(docs)}",
        f"- **Running jobs:** {len(jobs)}",
        f"- **Managed workers:** {', '.join(f'`{w}`' for w in workers) if workers else 'none'}",
        f"- **MCP servers:** {', '.join(f'`{m}`' for m in mcp_servers) if mcp_servers else 'none'}",
    ]
    return "\n".join(lines)


def _format_validation(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No numeric table columns found to cross-validate in the active document."
    lines = ["### Financial Cross-Validation", ""]
    for row in results:
        icon = "✓" if row.get("ok") else "✗"
        lines.append(
            f"- {icon} **{row.get('label', 'Column')}**: "
            f"computed `{row.get('computed', 0.0):.2f}` vs "
            f"document `{row.get('expected', 0.0):.2f}`"
        )
    return "\n".join(lines)


def _format_chunks(query: str, chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return f"No indexed SWE chunks matched `{query}`."
    lines = [f"### Search Results for `{query}` ({len(chunks)} hit(s))", ""]
    for idx, chunk in enumerate(chunks, start=1):
        heading = chunk.get("heading") or chunk.get("title") or f"Chunk {idx}"
        body = (chunk.get("content") or chunk.get("snippet") or "").strip()
        if len(body) > 500:
            body = body[:500].rstrip() + "…"
        lines.append(f"#### {idx}. {heading}\n{body}")
    return "\n\n".join(lines)


def _format_skills(skills: list[dict[str, Any]]) -> str:
    if not skills:
        return "No stored SWE skills yet."
    lines = ["### Stored SWE Skills", ""]
    for skill in skills:
        name = skill.get("name", "unnamed")
        desc = skill.get("description", "")
        lines.append(f"- **`{name}`**{f': {desc}' if desc else ''}")
    return "\n".join(lines)


@dataclass
class _ACPSessionState:
    session_id: str
    cwd: str
    domain_id: str
    updated_at: str = field(default_factory=_utc_now_iso)
    title: str | None = None
    active_job_id: str | None = None
    active_task: asyncio.Task[PromptResponse] | None = None
    cancelled: bool = False


class SqwakvoxACPAgent:
    """Agent Client Protocol (ACP) adapter backed by :class:`DocSession`."""

    def __init__(
        self,
        session: DocSession | None = None,
        *,
        default_domain: str = "financial",
        default_model: str | None = None,
    ) -> None:
        self._session = session if session is not None else get_session()
        self._default_domain = default_domain
        if default_model:
            self._session.set_credentials(default_model, "")
        self._conn: Client | None = None
        self._sessions: dict[str, _ACPSessionState] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    @property
    def session(self) -> DocSession:
        return self._session

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ #
    # ACP Handshake & Session Lifecycle
    # ------------------------------------------------------------------ #
    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del client_capabilities, client_info, kwargs
        negotiated = min(protocol_version, PROTOCOL_VERSION)
        return InitializeResponse(
            protocol_version=negotiated,
            agent_info=Implementation(
                name="sqwakvox",
                title="Sqwakvox Document Assistant",
                version=_package_version(),
            ),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(embedded_context=True),
                session_capabilities=SessionCapabilities(list=SessionListCapabilities()),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerSpec] | None = None,
        system_prompt: Any = None,
        config_options: Any = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del additional_directories, system_prompt, config_options, kwargs
        session_id = f"sqwakvox-{uuid.uuid4().hex[:12]}"
        state = _ACPSessionState(
            session_id=session_id,
            cwd=cwd,
            domain_id=self._default_domain,
        )
        self._sessions[session_id] = state
        if mcp_servers:
            self._register_client_mcp_servers(mcp_servers)
        self._schedule_available_commands(session_id)
        return NewSessionResponse(
            session_id=session_id,
            modes=self._build_modes(state),
            config_options=self._build_config_options(state),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerSpec] | None = None,
        additional_directories: list[str] | None = None,
        system_prompt: Any = None,
        config_options: Any = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        del additional_directories, system_prompt, config_options, kwargs
        state = self._sessions.get(session_id)
        if state is None:
            state = _ACPSessionState(
                session_id=session_id,
                cwd=cwd,
                domain_id=self._session.domain_of(None) or self._default_domain,
            )
            self._sessions[session_id] = state
        else:
            state.cwd = cwd or state.cwd
            state.updated_at = _utc_now_iso()
        if mcp_servers:
            self._register_client_mcp_servers(mcp_servers)

        # Replay active document chat history so the client restores the transcript.
        if self._session.active_source():
            for entry in self._session.chat():
                role = str(entry.get("role", "system"))
                text = str(entry.get("text", ""))
                if not text:
                    continue
                if role == "user":
                    await self._send_update(
                        session_id, UserMessageChunk(content=TextContentBlock(text=text))
                    )
                elif role == "agent":
                    await self._send_agent_text(session_id, text)
                else:
                    await self._send_thought(session_id, text)

        self._schedule_available_commands(session_id)
        return LoadSessionResponse(
            modes=self._build_modes(state),
            config_options=self._build_config_options(state),
        )

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        archived: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        del cursor, archived, kwargs
        items: list[SessionInfo] = []
        active_name = self._session.active_document_name()
        for state in self._sessions.values():
            if cwd and state.cwd != cwd:
                continue
            items.append(
                SessionInfo(
                    session_id=state.session_id,
                    cwd=state.cwd,
                    title=state.title or active_name or "Sqwakvox Session",
                    updated_at=state.updated_at,
                )
            )
        return ListSessionsResponse(sessions=items)

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse:
        del kwargs
        await self.cancel(session_id)
        self._sessions.pop(session_id, None)
        return CloseSessionResponse()

    # ------------------------------------------------------------------ #
    # Modes & Configuration Options
    # ------------------------------------------------------------------ #
    async def set_session_mode(
        self,
        session_id: str,
        mode_id: str,
        **kwargs: Any,
    ) -> SetSessionModeResponse:
        del kwargs
        state = self._require_session(session_id)
        valid_domains = {d["domain_id"] for d in self._session.list_domains()}
        if mode_id not in valid_domains:
            raise RequestError.invalid_params(
                {"modeId": f"Unknown domain '{mode_id}'. Available: {sorted(valid_domains)}"}
            )
        state.domain_id = mode_id
        state.updated_at = _utc_now_iso()
        await self._send_update(session_id, CurrentModeUpdate(current_mode_id=mode_id))
        await self._send_update(
            session_id, ConfigOptionUpdate(config_options=self._build_config_options(state))
        )
        return SetSessionModeResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> SetSessionConfigOptionResponse:
        del kwargs
        state = self._require_session(session_id)
        str_value = str(value)
        if config_id == "mode":
            valid_domains = {d["domain_id"] for d in self._session.list_domains()}
            if str_value not in valid_domains:
                raise RequestError.invalid_params(
                    {"value": f"Unknown domain '{str_value}'. Available: {sorted(valid_domains)}"}
                )
            state.domain_id = str_value
            await self._send_update(session_id, CurrentModeUpdate(current_mode_id=str_value))
        elif config_id == "model":
            valid_models = {m["model_id"] for m in self._session.list_models()}
            if str_value not in valid_models:
                raise RequestError.invalid_params(
                    {"value": f"Unknown model '{str_value}'. Available: {sorted(valid_models)}"}
                )
            self._session.set_credentials(str_value, "")
        else:
            raise RequestError.invalid_params({"configId": f"Unknown config option '{config_id}'."})

        state.updated_at = _utc_now_iso()
        options = self._build_config_options(state)
        await self._send_update(session_id, ConfigOptionUpdate(config_options=options))
        return SetSessionConfigOptionResponse(config_options=options)

    def _build_modes(self, state: _ACPSessionState) -> SessionModeState:
        modes = [
            SessionMode(
                id=str(d["domain_id"]),
                name=str(d["display_name"]),
                description=str(d.get("description") or ""),
            )
            for d in self._session.list_domains()
        ]
        return SessionModeState(current_mode_id=state.domain_id, available_modes=modes)

    def _build_config_options(self, state: _ACPSessionState) -> list[ConfigOptionItem]:
        domain_opts = [
            SessionConfigSelectOption(
                value=str(d["domain_id"]),
                name=str(d["display_name"]),
                description=str(d.get("description") or ""),
            )
            for d in self._session.list_domains()
        ]
        model_opts = [
            SessionConfigSelectOption(
                value=str(m["model_id"]),
                name=str(m["friendly_name"]),
                description=(
                    f"{m['env_var']} ({'configured' if m.get('configured') else 'not set'})"
                ),
            )
            for m in self._session.list_models()
        ]
        return [
            SessionConfigOptionSelect(
                id="mode",
                name="Expert Domain",
                description="Document assistant profile used for new documents",
                category="mode",
                current_value=state.domain_id,
                options=domain_opts,
            ),
            SessionConfigOptionSelect(
                id="model",
                name="Model",
                description="LLM model used for document chat queries",
                category="model",
                current_value=self._session.model_id,
                options=model_opts,
            ),
        ]

    # ------------------------------------------------------------------ #
    # Prompt & Cancellation
    # ------------------------------------------------------------------ #
    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.cancelled = True
        if state.active_job_id:
            with contextlib.suppress(Exception):
                self._session.cancel(state.active_job_id)
        if state.active_task is not None and not state.active_task.done():
            state.active_task.cancel()

    async def prompt(
        self,
        session_id: str,
        prompt: list[PromptBlock],
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        state = self._require_session(session_id)
        state.cancelled = False
        state.updated_at = _utc_now_iso()
        state.active_task = asyncio.current_task()

        try:
            attachments, text = _extract_prompt_parts(prompt, state.cwd)

            # 1. Auto-ingest any @file / resource blocks attached to the prompt.
            for src in attachments:
                if state.cancelled:
                    return PromptResponse(stop_reason="cancelled")
                ok = await self._open_or_activate(session_id, state, src, state.domain_id)
                if not ok and not text:
                    return PromptResponse(stop_reason="end_turn")

            if not text:
                if not attachments:
                    await self._send_agent_text(
                        session_id,
                        "Send a question about the active document, attach a file, or use "
                        "`/open <path-or-url>` to load a document.",
                    )
                return PromptResponse(stop_reason="end_turn")

            # 2. Dispatch slash commands directly to DocSession.
            if text.startswith("/"):
                return await self._handle_slash_command(session_id, state, text)

            # 3. Natural-language query against the active document.
            if not self._session.active_source():
                await self._send_agent_text(
                    session_id,
                    "No document is currently loaded.\n\n"
                    "Load one first with `/open <path-or-url>` (or attach a file via `@file`), "
                    "then ask your question.",
                )
                return PromptResponse(stop_reason="end_turn")

            return await self._run_ask(session_id, state, text)

        except asyncio.CancelledError:
            if state.cancelled:
                return PromptResponse(stop_reason="cancelled")
            raise
        finally:
            state.active_job_id = None
            state.active_task = None

    # ------------------------------------------------------------------ #
    # Slash Commands
    # ------------------------------------------------------------------ #
    async def _handle_slash_command(
        self, session_id: str, state: _ACPSessionState, raw: str
    ) -> PromptResponse:
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        cmd = parts[0].lstrip("/").lower()
        args = parts[1:]

        if cmd == "open":
            if not args:
                await self._send_agent_text(
                    session_id, "Usage: `/open <path-or-url> [financial|swe]`"
                )
                return PromptResponse(stop_reason="end_turn")
            source = _resolve_source(args[0], state.cwd)
            domain_id = args[1] if len(args) > 1 else state.domain_id
            await self._open_or_activate(session_id, state, source, domain_id, force_open=True)
            return PromptResponse(stop_reason="end_turn")

        if cmd == "docs":
            await self._send_agent_text(session_id, _format_docs(self._session.documents()))
            return PromptResponse(stop_reason="end_turn")

        if cmd == "switch":
            if not args:
                await self._send_agent_text(session_id, "Usage: `/switch <index-or-source>`")
                return PromptResponse(stop_reason="end_turn")
            target = self._resolve_doc_target(args[0])
            if target and self._session.activate(target):
                await self._send_agent_text(
                    session_id,
                    f"Switched active document to `{self._session.active_document_name()}`.",
                )
            else:
                await self._send_agent_text(
                    session_id, f"Could not find open document `{args[0]}`."
                )
            return PromptResponse(stop_reason="end_turn")

        if cmd == "close":
            target = self._resolve_doc_target(args[0]) if args else self._session.active_source()
            if target and self._session.close_document(target):
                await self._send_agent_text(session_id, f"Closed `{target}`.")
            else:
                await self._send_agent_text(session_id, "No matching open document to close.")
            return PromptResponse(stop_reason="end_turn")

        if cmd == "page":
            if not args:
                await self._send_agent_text(session_id, "Usage: `/page <page-number>`")
                return PromptResponse(stop_reason="end_turn")
            try:
                page = int(args[0])
            except ValueError:
                await self._send_agent_text(session_id, f"Invalid page number: `{args[0]}`.")
                return PromptResponse(stop_reason="end_turn")
            return await self._run_seek_page(session_id, state, page)

        if cmd == "more":
            direction = args[0].lower() if args else "down"
            return await self._run_load_more(session_id, state, direction)

        if cmd == "validate":
            return await self._run_cross_validate(session_id, state)

        if cmd == "search":
            if not args:
                await self._send_agent_text(session_id, "Usage: `/search <query>`")
                return PromptResponse(stop_reason="end_turn")
            query = " ".join(args)
            chunks = self._session.search_chunks(query)
            await self._send_agent_text(session_id, _format_chunks(query, chunks))
            return PromptResponse(stop_reason="end_turn")

        if cmd == "skills":
            skills = self._session.list_skills("swe")
            await self._send_agent_text(session_id, _format_skills(skills))
            return PromptResponse(stop_reason="end_turn")

        if cmd == "clear":
            if self._session.clear_chat():
                await self._send_agent_text(session_id, "Cleared chat history for active document.")
            else:
                await self._send_agent_text(session_id, "No active document to clear chat for.")
            return PromptResponse(stop_reason="end_turn")

        if cmd == "status":
            await self._send_agent_text(
                session_id, _format_status(self._session.snapshot(), state.domain_id)
            )
            return PromptResponse(stop_reason="end_turn")

        if cmd == "help":
            lines = ["### Sqwakvox ACP Commands", ""]
            for available in _available_commands():
                hint = (
                    f" `{available.input.root.hint}`"
                    if available.input is not None and available.input.root.hint
                    else ""
                )
                lines.append(f"- `/{available.name}`{hint} — {available.description}")
            await self._send_agent_text(session_id, "\n".join(lines))
            return PromptResponse(stop_reason="end_turn")

        await self._send_agent_text(
            session_id,
            f"Unknown command `/{cmd}`. Use `/help` to list available Sqwakvox commands.",
        )
        return PromptResponse(stop_reason="end_turn")

    def _resolve_doc_target(self, selector: str) -> str | None:
        docs = self._session.documents()
        if selector.isdigit():
            idx = int(selector) - 1
            if 0 <= idx < len(docs):
                return str(docs[idx]["source"])
        for doc in docs:
            if selector in (doc["source"], doc["file_name"]):
                return str(doc["source"])
        return None

    # ------------------------------------------------------------------ #
    # DocSession Job Bridges (with Live ACP Tool-Call & Event Streaming)
    # ------------------------------------------------------------------ #
    async def _open_or_activate(
        self,
        session_id: str,
        state: _ACPSessionState,
        source: str,
        domain_id: str,
        *,
        force_open: bool = False,
    ) -> bool:
        if not force_open and source in self._session.loaded_documents:
            self._session.activate(source)
            state.title = self._session.active_document_name()
            await self._send_agent_text(
                session_id,
                f"Activated already-loaded document `{self._session.active_document_name()}`.",
            )
            return True

        tool_id = f"open-{uuid.uuid4().hex[:8]}"
        await self._send_tool_start(
            session_id, tool_id, f"Open document ({domain_id}): {Path(source).name}", kind="read"
        )
        job = self._session.open_document(source, domain_id=domain_id)
        result, cancelled = await self._await_job_with_events(
            session_id, state, job, forward_chat=False
        )
        if cancelled:
            await self._send_tool_done(session_id, tool_id, "failed", "Cancelled.")
            return False

        if not result.get("ok"):
            err = str(result.get("error") or "Failed to open document.")
            await self._send_tool_done(session_id, tool_id, "failed", err)
            await self._send_agent_text(session_id, f"Failed to open `{source}`: {err}")
            return False

        file_name = str(result.get("file_name") or Path(source).name)
        state.title = file_name
        page_str = _format_page_range(result.get("page_range") or {})
        summary = (
            f"Loaded `{file_name}` (`{domain_id}`): "
            f"{result.get('table_count', 0)} table(s), "
            f"{result.get('char_count', 0)} chars"
            + (f", {page_str}" if page_str else "")
            + "."
        )
        await self._send_tool_done(session_id, tool_id, "completed", summary)
        await self._send_agent_text(session_id, summary)
        return True

    async def _run_seek_page(
        self, session_id: str, state: _ACPSessionState, page: int
    ) -> PromptResponse:
        if not self._session.active_source():
            await self._send_agent_text(session_id, "No active document loaded.")
            return PromptResponse(stop_reason="end_turn")
        tool_id = f"seek-{uuid.uuid4().hex[:8]}"
        await self._send_tool_start(session_id, tool_id, f"Seek to page {page}", kind="read")
        job = self._session.seek_page(page=page)
        result, cancelled = await self._await_job_with_events(
            session_id, state, job, forward_chat=False
        )
        if cancelled:
            await self._send_tool_done(session_id, tool_id, "failed", "Cancelled.")
            return PromptResponse(stop_reason="cancelled")
        if not result.get("ok"):
            err = str(result.get("error") or "Seek failed.")
            await self._send_tool_done(session_id, tool_id, "failed", err)
            await self._send_agent_text(session_id, f"Seek failed: {err}")
            return PromptResponse(stop_reason="end_turn")
        page_str = _format_page_range(result.get("page_range") or {})
        msg = f"Showing `{result.get('file_name', '')}` ({page_str or f'page {page}'})."
        await self._send_tool_done(session_id, tool_id, "completed", msg)
        await self._send_agent_text(session_id, msg)
        return PromptResponse(stop_reason="end_turn")

    async def _run_load_more(
        self, session_id: str, state: _ACPSessionState, direction: str
    ) -> PromptResponse:
        if not self._session.active_source():
            await self._send_agent_text(session_id, "No active document loaded.")
            return PromptResponse(stop_reason="end_turn")
        tool_id = f"more-{uuid.uuid4().hex[:8]}"
        await self._send_tool_start(
            session_id, tool_id, f"Load more pages ({direction})", kind="read"
        )
        job = self._session.load_more(direction=direction)
        result, cancelled = await self._await_job_with_events(
            session_id, state, job, forward_chat=False
        )
        if cancelled:
            await self._send_tool_done(session_id, tool_id, "failed", "Cancelled.")
            return PromptResponse(stop_reason="cancelled")
        if not result.get("ok"):
            err = str(result.get("error") or "Load more failed.")
            await self._send_tool_done(session_id, tool_id, "failed", err)
            await self._send_agent_text(session_id, f"Load more failed: {err}")
            return PromptResponse(stop_reason="end_turn")
        page_str = _format_page_range(result.get("page_range") or {})
        added = result.get("pages_added", 0)
        msg = f"Loaded {added} page(s) ({direction}); now showing {page_str}."
        await self._send_tool_done(session_id, tool_id, "completed", msg)
        await self._send_agent_text(session_id, msg)
        return PromptResponse(stop_reason="end_turn")

    async def _run_cross_validate(
        self, session_id: str, state: _ACPSessionState
    ) -> PromptResponse:
        if not self._session.active_source():
            await self._send_agent_text(session_id, "No active document loaded.")
            return PromptResponse(stop_reason="end_turn")
        tool_id = f"cv-{uuid.uuid4().hex[:8]}"
        await self._send_tool_start(
            session_id, tool_id, "Cross-validate financial tables", kind="execute"
        )
        job = self._session.cross_validate()
        result, cancelled = await self._await_job_with_events(
            session_id, state, job, forward_chat=False
        )
        if cancelled:
            await self._send_tool_done(session_id, tool_id, "failed", "Cancelled.")
            return PromptResponse(stop_reason="cancelled")
        if not result.get("ok"):
            err = str(result.get("error") or "Cross-validation failed.")
            await self._send_tool_done(session_id, tool_id, "failed", err)
            await self._send_agent_text(session_id, f"Cross-validation failed: {err}")
            return PromptResponse(stop_reason="end_turn")
        report = _format_validation(result.get("results") or [])
        await self._send_tool_done(session_id, tool_id, "completed", report)
        await self._send_agent_text(session_id, report)
        return PromptResponse(stop_reason="end_turn")

    async def _run_ask(
        self, session_id: str, state: _ACPSessionState, query: str
    ) -> PromptResponse:
        job = self._session.ask(query)
        result, cancelled = await self._await_job_with_events(
            session_id, state, job, forward_chat=True
        )
        if cancelled:
            return PromptResponse(stop_reason="cancelled")
        if not result.get("ok") and result.get("error"):
            await self._send_agent_text(session_id, f"Error: {result['error']}")
        return PromptResponse(stop_reason="end_turn")

    async def _await_job_with_events(
        self,
        session_id: str,
        state: _ACPSessionState,
        job: Job,
        *,
        forward_chat: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Await *job* while streaming matching :class:`SessionEvent` updates to ACP."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[SessionEvent | None] = asyncio.Queue()
        state.active_job_id = job.job_id

        def _sink(event: SessionEvent) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        unsubscribe = self._session.subscribe(_sink)
        emitted_error = False

        async def _drain() -> None:
            nonlocal emitted_error
            while True:
                event = await queue.get()
                if event is None:
                    break
                if event.kind in (EVENT_PROGRESS, EVENT_LOG):
                    msg = str(event.data.get("message") or "").strip()
                    if msg:
                        await self._send_thought(session_id, msg)
                elif event.kind == EVENT_ERROR:
                    msg = str(event.data.get("message") or "").strip()
                    if msg:
                        emitted_error = True
                        await self._send_agent_text(session_id, f"Error: {msg}")
                elif event.kind == EVENT_CHAT and forward_chat:
                    entry = event.data.get("message") or {}
                    role = entry.get("role")
                    body = str(entry.get("text") or "")
                    if not body:
                        continue
                    if role == "agent":
                        await self._send_agent_text(session_id, body)
                    elif role == "system":
                        level = entry.get("level")
                        if level == "error":
                            emitted_error = True
                            await self._send_agent_text(session_id, f"**System:** {body}")
                        else:
                            await self._send_thought(session_id, body)

        drainer = asyncio.create_task(_drain())
        try:
            if job.future is None:
                raw_result: dict[str, Any] = job.result or {}
            else:
                raw_result = await asyncio.wrap_future(job.future)
        except asyncio.CancelledError:
            self._session.cancel(job.job_id)
            if state.cancelled:
                return {"ok": False, "error": "Cancelled."}, True
            raise
        except Exception as exc:
            raw_result = {"ok": False, "error": str(exc)}
        finally:
            unsubscribe()
            queue.put_nowait(None)
            with contextlib.suppress(asyncio.CancelledError):
                await drainer
            if state.active_job_id == job.job_id:
                state.active_job_id = None

        if emitted_error and not raw_result.get("ok"):
            # Error was already streamed via EVENT_ERROR / system chat entry.
            raw_result = {**raw_result, "error": ""}
        return raw_result, state.cancelled

    # ------------------------------------------------------------------ #
    # Client MCP Registration & ACP Notification Helpers
    # ------------------------------------------------------------------ #
    def _register_client_mcp_servers(self, servers: list[McpServerSpec]) -> None:
        """Merge client-provided ACP MCP servers into the session config list."""
        from sqwakvox.mcp import MCPSse, MCPStdio, MCPStreamableHttp

        existing_names = {name for name, _cfg, _domains in self._session.mcp_configs}
        for srv in servers:
            name = getattr(srv, "name", "")
            if not name or name in existing_names:
                continue
            cfg: Any = None
            if isinstance(srv, McpServerStdio):
                env = {e.name: e.value for e in srv.env} if srv.env else None
                cfg = MCPStdio(command=srv.command, args=list(srv.args), env=env)
            elif isinstance(srv, HttpMcpServer):
                headers = {h.name: h.value for h in srv.headers} if srv.headers else None
                cfg = MCPStreamableHttp(url=srv.url, headers=headers)
            elif isinstance(srv, SseMcpServer):
                headers = {h.name: h.value for h in srv.headers} if srv.headers else None
                cfg = MCPSse(url=srv.url, headers=headers)
            if cfg is not None:
                self._session.mcp_configs.append((name, cfg, None))
                existing_names.add(name)

    def _require_session(self, session_id: str) -> _ACPSessionState:
        state = self._sessions.get(session_id)
        if state is None:
            raise RequestError.invalid_params({"sessionId": f"Unknown session '{session_id}'."})
        return state

    def _schedule_available_commands(self, session_id: str) -> None:
        if self._conn is None:
            return
        task = asyncio.create_task(
            self._send_update(
                session_id,
                AvailableCommandsUpdate(available_commands=_available_commands()),
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _send_update(self, session_id: str, update: Any) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.session_update(session_id=session_id, update=update)
        except Exception as exc:
            logger.debug("ACP session_update failed for %s: %s", session_id, exc)

    async def _send_agent_text(self, session_id: str, text: str) -> None:
        await self._send_update(
            session_id,
            AgentMessageChunk(content=TextContentBlock(text=text)),
        )

    async def _send_thought(self, session_id: str, text: str) -> None:
        await self._send_update(
            session_id,
            AgentThoughtChunk(content=TextContentBlock(text=text)),
        )

    async def _send_tool_start(
        self, session_id: str, tool_call_id: str, title: str, *, kind: ToolKind = "other"
    ) -> None:
        await self._send_update(
            session_id,
            ToolCallStart(
                tool_call_id=tool_call_id,
                title=title,
                kind=kind,
                status="in_progress",
            ),
        )

    async def _send_tool_done(
        self,
        session_id: str,
        tool_call_id: str,
        status: ToolStatus,
        summary: str,
    ) -> None:
        await self._send_update(
            session_id,
            ToolCallProgress(
                tool_call_id=tool_call_id,
                status=status,
                content=[ContentToolCallContent(content=TextContentBlock(text=summary))],
            ),
        )


# --------------------------------------------------------------------------- #
# Entry-point
# --------------------------------------------------------------------------- #


async def _serve(default_domain: str, default_model: str | None) -> None:
    session = get_session()
    agent = SqwakvoxACPAgent(
        session=session,
        default_domain=default_domain,
        default_model=default_model,
    )
    try:
        await run_agent(cast(Agent, agent))
    finally:
        with contextlib.suppress(Exception):
            session.publish_session_clear()
        with contextlib.suppress(Exception):
            session.close()


def main() -> None:
    """Run the Sqwakvox ACP agent server over stdio."""
    parser = argparse.ArgumentParser(description="Sqwakvox ACP presenter (stdio JSON-RPC 2.0)")
    parser.add_argument(
        "--domain",
        default="financial",
        help="Default expert domain for new documents (default: financial)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Default LLM model id (e.g. openai:gpt-5.5-high)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Stderr log verbosity (default: WARNING)",
    )
    args = parser.parse_args()

    # ACP uses stdout exclusively for JSON-RPC frames — route all logs to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    import atexit

    from sqwakvox.telemetry import setup_telemetry, shutdown_telemetry

    tm = setup_telemetry(role="acp")
    atexit.register(shutdown_telemetry)
    logger.info("Sqwakvox ACP telemetry initialized (exporter=%s)", tm.exporter_name)

    try:
        asyncio.run(_serve(default_domain=args.domain, default_model=args.model))
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
