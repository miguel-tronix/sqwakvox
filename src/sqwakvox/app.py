from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, ClassVar

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    RichLog,
    Select,
    Tab,
    Tabs,
)
from textual.worker import Worker

from sqwakvox.controller import AgentResult, extract_message
from sqwakvox.domains import get_domain, list_domains
from sqwakvox.domains.base import LoadedDocument
from sqwakvox.domains.swe import skills as swe_skills
from sqwakvox.guardrails import AuditLogger
from sqwakvox.models import ModelProvider, StructuredDocument
from sqwakvox.presenter import Presenter, TaskStatus
from sqwakvox.renderer import DocumentRenderPane
from sqwakvox.telemetry import get_telemetry
from sqwakvox.worker_manager import DOCLING_QUEUE, WorkerManager, managed_workers_enabled

logger = logging.getLogger(__name__)
chat_logger = logging.getLogger("sqwakvox.chat")

#: Pages fetched per slice when a PDF is loaded incrementally.  Override with
#: ``SQWAKVOX_PDF_BATCH_SIZE`` (the first slice renders immediately; further
#: slices are prefetched in the background and shown via "Load more").
PDF_BATCH_SIZE = int(os.environ.get("SQWAKVOX_PDF_BATCH_SIZE", "10"))

CSS = """
Screen {
    layout: grid;
    grid-size: 3;
    grid-columns: 1fr 2.2fr 1fr;
    grid-rows: 1fr;
}

#sidebar {
    border: solid $primary;
    padding: 1;
    background: $surface;
    overflow-y: auto;
}

#sidebar Label {
    margin-bottom: 1;
}

#doc-source-row {
    height: auto;
    margin-bottom: 1;
}

#doc-source {
    width: 1fr;
    margin-bottom: 0;
}

#btn-browse {
    min-width: 5;
    width: 5;
    margin-left: 1;
}

#ingest-history {
    height: auto;
    max-height: 8;
    margin-top: 1;
}

#mcp-servers-list {
    height: auto;
    max-height: 8;
    margin-top: 1;
    margin-bottom: 1;
}

#mcp-servers-label {
    margin-top: 1;
}

#center-column {
    layout: vertical;
}

#document-tabs {
    min-height: 3;
    height: 3;
    background: $panel;
    border-bottom: solid $secondary;
}

#doc-view-container {
    height: 1fr;
}

#render-pane {
    border: solid $secondary;
    padding: 1;
    background: $surface;
    overflow-y: auto;
    height: 1fr;
}

#render-pane:focus {
    border: double $secondary;
}

#doc-pager-row {
    height: auto;
    align: center middle;
}

#page-indicator {
    height: 3;
    content-align: left middle;
    margin-left: 1;
}

#view-tabs {
    min-height: 3;
    height: 3;
    background: $panel;
    border-bottom: solid $secondary;
}

#agent-response-pane {
    border: solid $secondary;
    padding: 1;
    background: $surface;
    overflow-y: auto;
    height: 1fr;
    display: none;
}

#agent-response-pane:focus {
    border: double $secondary;
}

#chat-column {
    layout: vertical;
}

#chat-log {
    border: solid $accent;
    padding: 1;
    background: $surface;
    height: 1fr;
}

#chat-input-row {
    height: auto;
}

#chat-input {
    height: 3;
    width: 85%;
}

#status-bar {
    dock: bottom;
    height: 1;
    background: $accent;
    color: white;
    padding: 0 1;
}

#loading-spinner {
    height: 1;
    dock: bottom;
}

#error-banner {
    dock: bottom;
    height: auto;
    visibility: hidden;
}

Select {
    background: $panel;
    color: $text;
}

SelectCurrent {
    background: $panel;
    color: $text;
}

SelectOverlay {
    background: $panel;
    color: $text;
    border: solid $primary;
}

FileSelectModal {
    align: center middle;
}

#modal-container {
    width: 70%;
    height: 80%;
    border: thick $primary;
    background: $surface;
    padding: 1;
}

#file-tree {
    height: 1fr;
    border: solid $secondary;
    margin: 1 0;
}

#modal-buttons {
    height: auto;
    align: right middle;
}

#modal-buttons Button {
    margin-left: 1;
}
"""


class FileSelectModal(ModalScreen[Path]):
    def compose(self) -> ComposeResult:
        with Vertical(id="modal-container"):
            yield Label("[bold]Select a Document File[/bold]")
            yield DirectoryTree("./", id="file-tree")
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", variant="error", id="btn-cancel")
                yield Button("Select", variant="success", id="btn-select")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(event.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-select":
            tree = self.query_one("#file-tree", DirectoryTree)
            if tree.cursor_node and tree.cursor_node.data:
                path = tree.cursor_node.data.path
                if path.is_file():
                    self.dismiss(path)


class SqwakvoxApp(App[None]):
    CSS = CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("ctrl+l", "focus_doc_source", "Load Document", priority=True),
        Binding("ctrl+f", "focus_chat_input", "Focus Chat", priority=True),
        Binding("ctrl+r", "clear_chat", "Clear Chat", priority=True),
        Binding("tab", "focus_next_pane", "Next Pane", priority=True),
        Binding("up", "scroll_up", "Scroll Up", priority=True),
        Binding("down", "scroll_down", "Scroll Down", priority=True),
        Binding("ctrl+x", "cross_validate", "Cross-Validate", priority=True),
    ]

    is_parsing = reactive(False)
    active_document_name = reactive("")
    active_error: reactive[str | None] = reactive(None)

    def __init__(self, presenter: Presenter | None = None) -> None:
        super().__init__()
        self.presenter = presenter or Presenter()
        self._active_parse_handles: dict[str, Worker[None]] = {}
        self._active_agent_handles: dict[str, Worker[None]] = {}
        self.doc_context: str = ""
        self.structured_doc: StructuredDocument | None = None
        self.ingestion_history: list[str] = []
        self.loaded_documents: dict[str, LoadedDocument] = {}
        #: Source → domain_id chosen at load time (the "Agent Expert Type").
        self._doc_domains: dict[str, str] = {}
        self.chat_histories: dict[str, list[str]] = {}
        self._chat_log_dir = Path.home() / ".sqwakvox_chat_logs"
        self._chat_log_dir.mkdir(parents=True, exist_ok=True)
        #: MCP servers as ``(name, config, domains | None)`` (see _load_mcp_servers).
        self.mcp_configs: list[tuple[str, Any, list[str] | None]] = []
        # Per-document-tab Celery queues and their managed workers.  Each
        # loaded document gets its own queue (``sqwakvox.doc<N>``) served by
        # a dedicated worker subprocess, so a slow agent query on one tab
        # never blocks chat on another.  Document *conversion* (Docling)
        # is shared: every tab's parse task routes to the single
        # ``sqwakvox.docling`` worker (see :mod:`sqwakvox.worker_manager`).
        self._doc_queues: dict[str, str] = {}
        self.worker_manager = WorkerManager()

    def _queue_for_source(self, source: str) -> str | None:
        """Return the dedicated queue for *source*, assigning one on first use.

        Returns ``None`` when managed workers are disabled — tasks then fall
        through to the default ``sqwakvox`` queue consumed by an externally
        started worker.
        """
        if not managed_workers_enabled():
            return None
        if source not in self._doc_queues:
            self._doc_queues[source] = f"sqwakvox.doc{len(self._doc_queues)}"
        return self._doc_queues[source]

    def _docling_queue(self) -> str | None:
        """The shared Docling conversion queue, or None to use the default.

        Mirrors :meth:`_queue_for_source`: when managed workers are disabled
        the parse task falls through to the default ``sqwakvox`` queue so an
        externally started worker still picks it up.
        """
        if not managed_workers_enabled():
            return None
        return DOCLING_QUEUE

    def _active_source(self) -> str | None:
        """Return the ingestion source of the currently active document."""
        if self.structured_doc is None:
            return None
        for src, doc in self.loaded_documents.items():
            if doc.file_name == self.active_document_name:
                return src
        return None

    def _active_queue(self) -> str | None:
        """Queue of the active document's worker, or None to use the default."""
        source = self._active_source()
        if source is None:
            return None
        return self._doc_queues.get(source)

    def _active_domain_id(self) -> str:
        """Domain of the currently active document (financial fallback)."""
        source = self._active_source()
        if source is None:
            return "financial"
        return self._doc_domains.get(source, "financial")

    def _selected_domain(self) -> str:
        """Domain chosen in the sidebar's Agent Expert Type selector."""
        try:
            value = self.query_one("#domain-selector", Select).value
        except Exception:
            return "financial"
        return str(value) if value else "financial"

    def on_unmount(self) -> None:
        """Stop all managed worker subprocesses when the TUI shuts down."""
        self.worker_manager.stop_all()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="sidebar"):
            yield Label("[bold]Load Document[/bold]")
            with Horizontal(id="doc-source-row"):
                yield Input(
                    placeholder="File path or URL...",
                    id="doc-source",
                )
                yield Button("📁", id="btn-browse", variant="default")
            yield Label("Agent Expert Type:")
            yield Select(
                options=[(d.display_name, d.domain_id) for d in list_domains()],
                value="financial",
                id="domain-selector",
            )
            yield Checkbox(
                "Crawl docs site (SWE URLs)",
                id="crawl-checkbox",
            )
            yield Button("Load & Parse", variant="primary", id="btn-parse")

            yield Label("[bold]Model Configuration[/bold]", id="model-config-label")
            yield Label("Select Model:")
            yield Select(
                options=[
                    (info["friendly_name"], model_id)
                    for model_id, info in ModelProvider.MAP.items()
                ],
                value="openai:gpt-5.5-high",
                id="model-selector",
            )
            yield Label("Enter Provider API Key:")
            yield Input(
                placeholder="sk-...",
                password=True,
                id="api-key-input",
            )

            yield Label("[bold]MCP Servers[/bold]", id="mcp-servers-label")
            yield ListView(id="mcp-servers-list")

            yield Label("[bold]Skills[/bold]", id="skills-label")
            yield ListView(id="skills-list")

            yield Label("[bold]Ingest History[/bold]", id="history-label")
            yield ListView(id="ingest-history")

        with Vertical(id="center-column"):
            yield Tabs(id="document-tabs")
            yield Tabs(
                Tab("Document", id="view-doc"),
                Tab("Agent Response", id="view-agent"),
                id="view-tabs",
            )
            with Vertical(id="doc-view-container"):
                yield DocumentRenderPane(id="render-pane")
                with Horizontal(id="doc-pager-row"):
                    yield Button(
                        "Load more pages ↓",
                        id="btn-load-more",
                        variant="default",
                        disabled=True,
                    )
                    yield Label("", id="page-indicator")
            yield RichLog(id="agent-response-pane", highlight=True, markup=True, wrap=True)

        with Vertical(id="chat-column"):
            yield RichLog(id="chat-log", highlight=True, markup=True, wrap=True)
            with Horizontal(id="chat-input-row"):
                yield Input(
                    placeholder="Ask a question about the document...",
                    id="chat-input",
                    disabled=True,
                )
                yield Button("Send", variant="success", id="btn-send", disabled=True)

        yield LoadingIndicator(id="loading-spinner")

        yield Label("Status: Idle | Ready", id="status-bar")

        yield Label("", id="error-banner")

        yield Footer()

    def on_mount(self) -> None:
        self._update_ui_state()
        self._load_mcp_servers()
        self._refresh_skills_list()

    def _load_mcp_servers(self) -> None:
        """Load MCP servers config from standard locations.

        Supports both stdio servers (``command``/``args``) and HTTP-based
        servers (``url`` with ``transport`` of ``sse`` or ``http``). The latter
        lets the calc-stats server run as a long-lived process and sidesteps the
        async->sync stdio threading issues (see mcp_fixes.md, Priority 1).
        """
        from any_agent.config import MCPStdio

        try:
            from any_agent.config import MCPSse, MCPStreamableHttp
        except ImportError:  # pragma: no cover - older any-agent
            sse_class: object | None = None
            http_class: object | None = None
        else:
            sse_class = MCPSse
            http_class = MCPStreamableHttp

        self.mcp_configs = []

        paths = [
            Path("mcp_servers.json"),
            Path.home() / ".sqwakvox" / "mcp_servers.json",
            Path.home() / ".config" / "sqwakvox" / "mcp_servers.json",
        ]

        for path in paths:
            if path.exists():
                try:
                    with path.open(encoding="utf-8") as f:
                        config = json.load(f)

                    servers_dict = config.get("mcpServers", config)
                    if not isinstance(servers_dict, dict):
                        continue

                    for name, srv in servers_dict.items():
                        if not isinstance(srv, dict):
                            continue

                        timeout_seconds = srv.get("client_session_timeout_seconds", 300.0)

                        if "url" in srv:
                            mcp_opt = self._build_http_mcp(
                                srv, timeout_seconds, sse_class, http_class
                            )
                        elif "command" in srv:
                            cmd = srv["command"]
                            args = srv.get("args", [])
                            env = srv.get("env", None)
                            if env:
                                env = {str(k): str(v) for k, v in env.items()}
                            mcp_opt = MCPStdio(
                                command=cmd,
                                args=args,
                                env=env,
                                tools=srv.get("tools", None),
                                client_session_timeout_seconds=timeout_seconds,
                            )
                        else:
                            continue

                        if mcp_opt is not None:
                            domains_tag = srv.get("domains")
                            self.mcp_configs.append((name, mcp_opt, domains_tag))
                    break
                except Exception as e:
                    logger.error("Error loading MCP servers from %s: %s", path, e)

        list_view = self.query_one("#mcp-servers-list", ListView)
        list_view.clear()

        if not self.mcp_configs:
            list_view.append(
                ListItem(
                    Label(
                        "[dim]No MCP servers active.\nCreate mcp_servers.json to add tools.[/dim]"
                    ),
                    disabled=True,
                )
            )
        else:
            for name, config, _domains in self.mcp_configs:
                list_view.append(
                    ListItem(
                        Label(f"🟢 [bold]{name}[/bold] ({config.command})"),
                        id=f"mcp-item-{name}",
                    )
                )

    def _mcp_configs_for(self, domain_id: str) -> list[Any]:
        """MCP configs available to *domain_id* (untagged servers are global)."""
        return [
            config
            for _name, config, domains in self.mcp_configs
            if domains is None or domain_id in domains
        ]

    def _refresh_skills_list(self, domain_id: str | None = None) -> None:
        """List stored skills for the active domain in the sidebar Skills pane."""
        domain_id = domain_id or self._active_domain_id()
        list_view = self.query_one("#skills-list", ListView)
        list_view.clear()
        domain = get_domain(domain_id)
        if not domain.skills_enabled:
            list_view.append(
                ListItem(
                    Label("[dim]Skills not used by this expert type.[/dim]"),
                    disabled=True,
                )
            )
            return
        skills = swe_skills.list_skills(domain_id)
        if not skills:
            list_view.append(
                ListItem(
                    Label("[dim]No skills stored yet.[/dim]"),
                    disabled=True,
                )
            )
            return
        for skill in skills:
            name = skill["name"]
            description = skill.get("description", "")
            label = f"🧠 [bold]{name}[/bold]"
            if description:
                label += f"\n[dim]{description[:60]}[/dim]"
            list_view.append(ListItem(Label(label), id=f"skill-item-{name}"))

    @staticmethod
    def _build_http_mcp(
        srv: dict[str, Any],
        timeout_seconds: float,
        mcp_sse: Any | None,
        mcp_http: Any | None,
    ) -> Any | None:
        transport = srv.get("transport", "sse")
        url = srv["url"]
        headers = srv.get("headers")
        if transport == "http":
            if mcp_http is None:
                logger.error("MCPStreamableHttp unavailable; skipping %s", url)
                return None
            return mcp_http(
                url=url,
                headers=headers,
                client_session_timeout_seconds=timeout_seconds,
            )
        if mcp_sse is None:
            logger.error("MCPSse unavailable; skipping %s", url)
            return None
        return mcp_sse(
            url=url,
            headers=headers,
            client_session_timeout_seconds=timeout_seconds,
        )

    def _update_ui_state(self) -> None:
        status_bar = self.query_one("#status-bar", Label)
        spinner = self.query_one("#loading-spinner", LoadingIndicator)
        chat_input = self.query_one("#chat-input", Input)
        btn_send = self.query_one("#btn-send", Button)
        btn_parse = self.query_one("#btn-parse", Button)

        # Chat stays available whenever any document is ready, even while
        # another document is still parsing on its own worker.
        is_ready = bool(self.doc_context)
        chat_input.disabled = not is_ready
        btn_send.disabled = not is_ready

        if self.is_parsing:
            status_bar.update("Status: Processing Layout via Docling...")
            spinner.visible = True
        else:
            status_bar.update("Status: Idle | Ready" if is_ready else "Status: Idle")
            spinner.visible = False
        btn_parse.disabled = False

    def watch_is_parsing(self, _new_value: bool) -> None:
        self._update_ui_state()

    def watch_active_error(self, error: str | None) -> None:
        error_pane = self.query_one("#error-banner", Label)
        if error:
            escaped_error = escape(extract_message(error))
            error_pane.update(f"[bold white on red]Error: {escaped_error}[/]")
            error_pane.styles.visibility = "visible"
        else:
            error_pane.styles.visibility = "hidden"

    def action_focus_doc_source(self) -> None:
        self.query_one("#doc-source", Input).focus()

    def action_focus_chat_input(self) -> None:
        self.query_one("#chat-input", Input).focus()

    def action_clear_chat(self) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.clear()
        if self.active_document_name:
            self.chat_histories[self.active_document_name] = []
            self._save_chat_log(self.active_document_name)
        self.write_chat_message("[italic]Chat cleared.[/italic]", persist=True)
        chat_logger.info("Chat log cleared by user")

    def _chat_log_path(self, doc_name: str) -> Path:
        """Return the on-disk JSON chat-log path for *doc_name*."""
        safe = re.sub(r"[^\w.\-]", "_", doc_name)
        return self._chat_log_dir / f"{safe}.jsonl"

    def _save_chat_log(self, doc_name: str) -> None:
        """Flush the in-memory chat history for *doc_name* to disk."""
        path = self._chat_log_path(doc_name)
        try:
            with path.open("w", encoding="utf-8") as fh:
                for line in self.chat_histories.get(doc_name, []):
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("Could not write chat log to %s", path)

    def _load_chat_log(self, doc_name: str) -> list[str]:
        """Load a previously saved chat log from disk, if it exists."""
        path = self._chat_log_path(doc_name)
        if not path.exists():
            return []
        entries: list[str] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if raw_line:
                        entries.append(json.loads(raw_line))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read chat log from %s", path)
        return entries

    def write_chat_message(self, markup: str, persist: bool = True) -> None:
        def _write() -> None:
            chat_log = self.query_one("#chat-log", RichLog)
            chat_log.write(markup)
            if persist and self.active_document_name:
                if self.active_document_name not in self.chat_histories:
                    self.chat_histories[self.active_document_name] = []
                self.chat_histories[self.active_document_name].append(markup)
                self._save_chat_log(self.active_document_name)

        try:
            self.call_from_thread(_write)
        except RuntimeError:
            _write()

    def write_agent_response(self, markup: str) -> None:
        def _write() -> None:
            try:
                agent_pane = self.query_one("#agent-response-pane", RichLog)
                agent_pane.write(markup)
            except Exception:
                pass

        try:
            self.call_from_thread(_write)
        except RuntimeError:
            _write()

    def write_agent_message(self, label_markup: str, body: str, persist: bool = True) -> None:
        """Write a labelled agent message with the body rendered verbatim.

        The body is rendered as literal text (no Rich markup parsing, ANSI
        escapes honoured) so ASCII charts and JSON/Python-object payloads
        display exactly as the backend produced them instead of being
        mangled by the Rich markup parser.
        """
        from rich.text import Text

        try:
            label_text = Text.from_markup(label_markup)
        except Exception:
            label_text = Text(label_markup)

        try:
            body_text = Text.from_ansi(body)
        except Exception:
            body_text = Text(body)

        assembled = Text.assemble(label_text, body_text)

        def _write() -> None:
            chat_log = self.query_one("#chat-log", RichLog)
            chat_log.write(assembled)
            try:
                agent_pane = self.query_one("#agent-response-pane", RichLog)
                agent_pane.write(assembled)
            except Exception:
                pass
            if persist and self.active_document_name:
                if self.active_document_name not in self.chat_histories:
                    self.chat_histories[self.active_document_name] = []
                self.chat_histories[self.active_document_name].append(label_markup + body)
                self._save_chat_log(self.active_document_name)

        try:
            self.call_from_thread(_write)
        except RuntimeError:
            _write()

    def action_scroll_up(self) -> None:
        focused = self.focused
        if focused and hasattr(focused, "scroll_up"):
            focused.scroll_up()

    def action_scroll_down(self) -> None:
        focused = self.focused
        if focused and hasattr(focused, "scroll_down"):
            focused.scroll_down()

    def action_focus_next_pane(self) -> None:
        panes = [
            self.query_one("#doc-source", Input),
            self.query_one("#agent-response-pane", RichLog),
            self.query_one("#render-pane", DocumentRenderPane),
            self.query_one("#chat-input", Input),
        ]
        focused = self.focused
        if focused in panes:
            idx = panes.index(focused)
            next_idx = (idx + 1) % len(panes)
            panes[next_idx].focus()
        else:
            panes[0].focus()

    def action_cross_validate(self) -> None:
        if not self.structured_doc or not self.structured_doc.tables:
            self.write_chat_message(
                "[bold yellow]No document loaded to cross-validate.[/bold yellow]",
                persist=True,
            )
            return

        cv_header = "\n[bold underline]Numerical Cross-Validation[/bold underline]"
        self.write_chat_message(cv_header, persist=True)
        self.write_agent_response(cv_header)

        # Dispatch cross-validation as a Celery task via the presenter.
        self.run_worker(
            self._dispatch_cross_validate(self.structured_doc),
            name="cross_validate_worker",
        )

    async def _dispatch_cross_validate(self, doc: StructuredDocument) -> None:
        """Async Textual worker: cross-validate the parsed document tables."""

        def on_complete(status: TaskStatus, results: Any) -> None:
            if status == TaskStatus.SUCCESS:
                for col_name, expected, actual, is_valid in results:
                    if is_valid:
                        msg = (
                            f"  [green]✓[/green] Column '{col_name}' "
                            f"sums to {expected} (actual: {actual:.2f})"
                        )
                    else:
                        msg = (
                            f"  [red]✗[/red] Column '{col_name}' "
                            f"expected {expected} but got {actual:.2f}"
                        )
                    self.write_chat_message(msg, persist=True)
                    self.write_agent_response(msg)
            elif status == TaskStatus.FAILURE:
                err = results if isinstance(results, str) else str(results)
                self.write_chat_message(
                    f"[bold red]Cross-validation failed:[/bold red] {escape(extract_message(err))}",
                    persist=True,
                )

        try:
            await self.presenter.cross_validate(
                document=doc,
                queue=self._active_queue(),
                on_complete=on_complete,
                on_error=lambda err: self.write_chat_message(
                    f"[bold red]Cross-validation error:[/bold red] {escape(extract_message(err))}",
                    persist=True,
                ),
            )
        except Exception as exc:
            self.write_chat_message(
                f"[bold red]Cross-validation error:[/bold red] {escape(extract_message(str(exc)))}",
                persist=True,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-browse":
            self.push_screen(FileSelectModal(), callback=self._on_file_selected)
        elif event.button.id == "btn-parse":
            self._handle_parse()
        elif event.button.id == "btn-send":
            self._handle_chat()
        elif event.button.id == "btn-load-more":
            source = self._active_source()
            if source:
                self.run_worker(self._load_more(source), name=f"load_more_{source}")

    def _on_file_selected(self, path: Path | None) -> None:
        if path:
            self.query_one("#doc-source", Input).value = str(path.absolute())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-input" and not event.input.disabled:
            self._handle_chat()

    def _handle_parse(self) -> None:
        source = self.query_one("#doc-source", Input).value.strip()
        if not source:
            return

        # Only block a duplicate parse of the *same* source; different
        # sources parse concurrently, each on its own document worker.
        if source in self._active_parse_handles:
            self.write_chat_message(
                f"[bold yellow]A parse of {source} is already in progress.[/bold yellow]",
                persist=False,
            )
            return

        # The user picks the expert type per document (no auto-detection).
        domain_id = self._selected_domain()
        self._doc_domains[source] = domain_id

        # Optional docs-site crawling for SWE URLs.
        options: dict[str, Any] | None = None
        try:
            if self.query_one("#crawl-checkbox", Checkbox).value:
                options = {"crawl": True}
        except Exception:
            options = None

        self.is_parsing = True
        self.active_error = None

        domain = get_domain(domain_id)
        self.write_chat_message(
            f"\n[italic dim]Starting {domain.display_name} ingestion for: {source}...[/italic dim]",
            persist=False,
        )
        self.write_chat_message(
            "[italic dim]Initializing Docling Parser (this may take a few seconds)...[/italic dim]",
            persist=False,
        )
        logger.info("Initiated parsing for document source: %s (domain=%s)", source, domain_id)

        # Textual workers are asyncio Tasks on the same event loop as the
        # presenter, so we can await presenter calls directly.
        self._active_parse_handles[source] = self.run_worker(
            self._dispatch_parse(source, domain_id, options),
            name=f"docling_parser_{source}",
        )

    async def _dispatch_parse(
        self,
        source: str,
        domain_id: str = "financial",
        options: dict[str, Any] | None = None,
    ) -> None:
        """Async Textual worker that delegates document parsing to the
        Presenter (which talks to Celery in a background thread).

        Conversion runs on the *shared* Docling worker (``sqwakvox.docling``)
        so its heavyweight OCR models load once, while the document still
        gets its own agent queue (``sqwakvox.doc<N>``) for later chat and
        cross-validation — each tab is served in isolation, and a slow parse
        on one document never blocks agent queries on another.

        After conversion, the domain's post-parse step runs on the document's
        agent worker (data store / TOC+code index / retrieval indexing) and
        its payload is merged into the document's metadata *before* the tab is
        switched to, so rendering and agent context see the full picture.
        """
        # The user picks the expert type per document; record it even when the
        # dispatch is invoked directly (tests/tooling) rather than via the
        # sidebar flow in _handle_parse.
        self._doc_domains[source] = domain_id

        # Make sure the shared Docling worker is running before the parse
        # task is submitted (idempotent — only one process ever consumes the
        # docling queue).  Also ensure this document's dedicated agent worker
        # so it is ready for chat/cross-validation.  Tasks may land in a
        # queue a moment before its worker finishes booting; Celery holds
        # them until the worker starts consuming.
        doc_queue = self._queue_for_source(source)
        if doc_queue is not None:
            self.worker_manager.ensure_worker(doc_queue)
        docling_queue = self._docling_queue()
        if docling_queue is not None:
            self.worker_manager.ensure_docling_worker()
            self.write_chat_message(
                f"[italic dim]Docling ingest worker ready (queue: {docling_queue})[/italic dim]",
                persist=False,
            )

        parsed: StructuredDocument | None = None
        parse_failed: str | None = None

        def on_progress(status: TaskStatus, _payload: Any) -> None:
            if status == TaskStatus.STARTED:
                self.write_chat_message(
                    "[italic dim]Docling parser is running...[/italic dim]",
                    persist=False,
                )

        def on_complete(status: TaskStatus, payload: Any) -> None:
            nonlocal parsed, parse_failed
            if status == TaskStatus.SUCCESS and isinstance(payload, StructuredDocument):
                parsed = payload
            elif status == TaskStatus.FAILURE:
                parse_failed = payload if isinstance(payload, str) else str(payload)
            elif status in (TaskStatus.REVOKED, TaskStatus.CANCELLED):
                parse_failed = "Parse was cancelled."

        try:
            # Local PDFs load incrementally: convert only the first slice so a
            # 100+ page document renders instead of timing out, then the TUI
            # prefetches the rest in the background (see ``_prefetch_batch``).
            page_range = None
            if Path(source).suffix.lower() == ".pdf" and Path(source).is_file():
                page_range = (1, PDF_BATCH_SIZE)

            parse_handle = await self.presenter.parse_document(
                source=source,
                domain_id=domain_id,
                options=options,
                page_range=page_range,
                queue=docling_queue,
                on_progress=on_progress,
                on_complete=on_complete,
            )
            # parse_document returns a handle immediately — on_complete fires
            # asynchronously when the Celery task finishes (Docling can take
            # minutes), so we must wait for it before reading `parsed`.
            await parse_handle.wait()
        except Exception as exc:
            self._finish_parse(source)
            self._on_parse_failure(str(exc))
            return

        if parsed is None:
            self._finish_parse(source)
            self._on_parse_failure(parse_failed or "Parse failed.")
            return

        # --- Domain post-parse: cheap analysis on the doc's agent worker ---
        payload: dict[str, Any] = {}
        try:
            pp_handle = await self.presenter.postprocess_document(
                domain_id=domain_id,
                document=parsed,
                queue=doc_queue,
            )
            await pp_handle.wait()
            if pp_handle.status == TaskStatus.SUCCESS:
                payload = pp_handle.result or {}
        except Exception:
            logger.exception("Post-parse step failed for %s", source)

        if payload:
            parsed.metadata.update(payload)
            self._surface_postprocess_info(domain_id, payload)

        self._on_parse_success(parsed, source, domain_id)
        self._finish_parse(source)

    def _surface_postprocess_info(self, domain_id: str, payload: dict[str, Any]) -> None:
        """Write parse-time analysis info (SWE: TOC/code/injection/retrieval)."""
        if domain_id != "swe":
            return
        toc = payload.get("toc") or []
        code_blocks = payload.get("code_blocks") or []
        source_type = payload.get("source_type", "file")
        pages = payload.get("pages_converted")
        chunks = payload.get("chunk_count") or 0

        details = [
            f"[italic dim]Parsed as {source_type}: {len(toc)} section(s), "
            f"{len(code_blocks)} code block(s), {chunks} search chunk(s).[/italic dim]"
        ]
        if pages:
            details.append(f"[italic dim]{pages} docs-site pages converted.[/italic dim]")
        for flag in payload.get("injection_flags") or []:
            details.append(
                f"[bold yellow]⚠ Potential prompt-injection text in document: "
                f"{escape(flag)}[/bold yellow]"
            )
        if payload.get("needs_retrieval"):
            details.append(
                "[bold yellow]Large document: agent will search sections with the "
                "retrieval tool instead of reading it whole.[/bold yellow]"
            )
        for line in details:
            self.write_chat_message(line, persist=False)

    def _finish_parse(self, source: str) -> None:
        """Drop *source*'s parse handle; clear the parsing flag when done."""
        with contextlib.suppress(KeyError):
            del self._active_parse_handles[source]
        if not self._active_parse_handles:
            self.is_parsing = False

    def _on_parse_success(
        self, structured: StructuredDocument, source: str, domain_id: str = "financial"
    ) -> None:
        # Note: is_parsing is cleared by _finish_parse once *all* in-flight
        # parses (possibly concurrent, one per document tab) have settled.
        self.loaded_documents[source] = LoadedDocument(
            domain_id=domain_id,
            structured=structured,
            source=source,
        )
        # Initialise incremental-paging bookkeeping when the document arrived
        # as a PDF page slice (controller stamps ``page_range`` on the result).
        if structured.metadata.get("page_range") is not None:
            loaded = self.loaded_documents[source]
            loaded.batch_size = int(structured.metadata.get("pages_in_batch") or PDF_BATCH_SIZE)
            loaded.total_pages = structured.metadata.get("total_pages")
            loaded.rendered_pages = loaded.batch_size
            loaded.next_batch = 1
            loaded.batch_cache = {}
            loaded.pending = {}

        if structured.file_name not in self.chat_histories:
            saved = self._load_chat_log(structured.file_name)
            self.chat_histories[structured.file_name] = saved

        if source not in self.ingestion_history:
            self.ingestion_history.append(source)
            history_list = self.query_one("#ingest-history", ListView)
            history_list.append(ListItem(Label(f"• {structured.file_name} (Ready)")))
            tm = get_telemetry()
            if tm.active_documents_counter:
                tm.active_documents_counter.add(1)

        self._rebuild_tabs()
        self._switch_to_document(structured, source="ingest")

        # Prefetch the next slice in the background so "Load more" is instant.
        if self.loaded_documents[source].is_paged:
            self._update_pager(source)
            self.run_worker(
                self._prefetch_batch(source, 1),
                name=f"prefetch_{source}_1",
            )

    # ------------------------------------------------------------------ #
    # Incremental PDF paging: render the first slice, prefetch the rest.
    # ------------------------------------------------------------------ #
    def _update_pager(self, source: str) -> None:
        """Enable/disable the "Load more" control and show a page indicator."""
        try:
            btn = self.query_one("#btn-load-more", Button)
            indicator = self.query_one("#page-indicator", Label)
        except Exception:
            return
        loaded = self.loaded_documents.get(source)
        if loaded is None or not loaded.is_paged:
            btn.disabled = True
            indicator.update("")
            return
        rendered = loaded.rendered_pages
        total = loaded.total_pages
        if total:
            indicator.update(f"[dim]Pages 1-{rendered} of {total}[/dim]")
            done = rendered >= total
        else:
            indicator.update(f"[dim]Pages 1-{rendered} loaded[/dim]")
            # Unknown total: stop once the next slice comes back empty.
            done = (
                loaded.next_batch in loaded.batch_cache
                and not (loaded.batch_cache[loaded.next_batch].raw_markdown or "").strip()
            )
        btn.disabled = done
        btn.label = "All pages loaded ✓" if done else "Load more pages ↓"

    async def _prefetch_batch(self, source: str, batch_index: int) -> None:
        """Fetch PDF page slice *batch_index* in the background and cache it.

        On completion it chains a prefetch of the following slice so the cache
        stays one slice ahead of what the user has revealed.
        """
        loaded = self.loaded_documents.get(source)
        if loaded is None or not loaded.is_paged:
            return
        if batch_index in loaded.pending or batch_index in loaded.batch_cache:
            return
        if loaded.total_pages and batch_index * loaded.batch_size >= loaded.total_pages:
            return

        start = batch_index * loaded.batch_size + 1
        end = start + loaded.batch_size - 1
        if loaded.total_pages:
            end = min(end, loaded.total_pages)

        domain_id = loaded.domain_id
        docling_queue = self._docling_queue()

        def on_complete(status: TaskStatus, payload: Any) -> None:
            if status == TaskStatus.SUCCESS and isinstance(payload, StructuredDocument):
                loaded.batch_cache[batch_index] = payload
                # Keep one slice ahead: prefetch the next one.
                self.run_worker(
                    self._prefetch_batch(source, batch_index + 1),
                    name=f"prefetch_{source}_{batch_index + 1}",
                )
            elif status in (TaskStatus.FAILURE, TaskStatus.REVOKED, TaskStatus.CANCELLED):
                loaded.pending.pop(batch_index, None)

        try:
            handle = await self.presenter.parse_document(
                source=source,
                domain_id=domain_id,
                page_range=(start, end),
                queue=docling_queue,
                on_complete=on_complete,
            )
            loaded.pending[batch_index] = handle
        except Exception as exc:
            logger.warning("Prefetch of slice %d for %s failed: %s", batch_index, source, exc)

    async def _load_more(self, source: str) -> None:
        """Append the next prefetched (or in-flight) PDF slice to the document."""
        loaded = self.loaded_documents.get(source)
        if loaded is None or not loaded.is_paged:
            return

        batch_index = loaded.next_batch
        if batch_index not in loaded.batch_cache:
            if batch_index in loaded.pending:
                await loaded.pending[batch_index].wait()
            else:
                start = batch_index * loaded.batch_size + 1
                end = start + loaded.batch_size - 1
                if loaded.total_pages:
                    end = min(end, loaded.total_pages)
                try:
                    handle = await self.presenter.parse_document(
                        source=source,
                        domain_id=loaded.domain_id,
                        page_range=(start, end),
                        queue=self._docling_queue(),
                    )
                    await handle.wait()
                except Exception as exc:
                    logger.warning("Load-more slice %d failed: %s", batch_index, exc)
                    return
                if handle.status == TaskStatus.SUCCESS and isinstance(
                    handle.result, StructuredDocument
                ):
                    loaded.batch_cache[batch_index] = handle.result
                else:
                    return

        batch = loaded.batch_cache.pop(batch_index, None)
        if batch is None:
            return
        if not (batch.raw_markdown or "").strip():
            # Empty slice => end of document (e.g. unknown total_pages).
            loaded.next_batch = batch_index + 1
            self._update_pager(source)
            return

        # Extend the rendered document and refresh the agent context.
        loaded.structured.raw_markdown += "\n\n" + batch.raw_markdown
        loaded.structured.tables.extend(batch.tables)
        loaded.rendered_pages += int(batch.metadata.get("pages_in_batch") or loaded.batch_size)
        loaded.next_batch = batch_index + 1

        domain_id = loaded.domain_id
        self.doc_context = get_domain(domain_id).context_for(loaded.structured)
        render_pane = self.query_one("#render-pane", DocumentRenderPane)
        render_pane.update_document(loaded.structured, domain_id)
        self._update_pager(source)

        # Make sure the following slice is already being prefetched.
        if batch_index + 1 not in loaded.pending and batch_index + 1 not in loaded.batch_cache:
            self.run_worker(
                self._prefetch_batch(source, batch_index + 1),
                name=f"prefetch_{source}_{batch_index + 1}",
            )

    def _on_parse_failure(self, error_message: str) -> None:
        # Note: is_parsing is cleared by _finish_parse once *all* in-flight
        # parses (possibly concurrent, one per document tab) have settled.
        self.active_error = error_message
        self.write_chat_message(
            f"[bold red]✗ Parsing failed:[/bold red] {escape(extract_message(error_message))}",
            persist=False,
        )

        logger.error(f"Docling parsing failed: {error_message}")

        AuditLogger.log(
            document_id="unknown",
            operation="document_ingested",
            action="FAILURE",
            risk_score=1.0,
        )

    # _build_financial_data_store moved to AppController

    def _handle_chat(self) -> None:
        chat_input = self.query_one("#chat-input", Input)
        user_query = chat_input.value.strip()
        if not user_query:
            return

        chat_logger.info("User query: %s", user_query)
        self.write_chat_message(f"\n[bold blue]You:[/bold blue] {user_query}", persist=True)
        chat_input.value = ""

        selected_model = self.query_one("#model-selector", Select).value
        if selected_model is None or not isinstance(selected_model, str):
            self.write_chat_message(
                "[bold yellow]System: Please select a valid model configuration.[/bold yellow]",
                persist=True,
            )
            return

        api_key = self.query_one("#api-key-input", Input).value.strip()

        if not api_key:
            self.write_chat_message(
                "[bold yellow]System: Warning! API Key is missing. "
                "Please provide a valid key in the sidebar.[/bold yellow]",
                persist=True,
            )
            return

        if len(api_key) < 10:
            self.write_chat_message(
                "[bold red]System: Invalid API Key. The provided key is too short.[/bold red]",
                persist=True,
            )
            return

        chat_logger.info("Agent invoked — model: %s", selected_model)
        self.write_chat_message(
            "[italic dim]Agent is thinking (via LangChain)...[/italic dim]",
            persist=False,
        )
        self.write_agent_response("[italic dim]Agent is thinking (via LangChain)...[/italic dim]")

        # Dispatch the agent execution as an async Textual worker.
        self._active_agent_handles[user_query] = self.run_worker(
            self._dispatch_agent(selected_model, api_key, user_query),
            name="any_agent_worker",
        )

    async def _dispatch_agent(self, model_id: str, api_key: str, user_query: str) -> None:
        """Async Textual worker that delegates agent execution to the Presenter.

        The flow: first run the active domain's post-parse step (e.g. the
        financial data store, or the SWE code/TOC index — both Celery tasks),
        then submit the agent task.  Both are polled by the presenter and
        callbacks update the UI directly on this loop.
        """

        # --- Step 1: domain post-parse (Celery task) ---
        doc = self.structured_doc
        if doc is None:
            self._on_agent_failure("No document loaded.")
            return

        domain_id = self._active_domain_id()
        try:
            ds_handle = await self.presenter.postprocess_document(
                domain_id=domain_id,
                document=doc,
                queue=self._active_queue(),
                on_error=self._on_agent_failure,
            )
            # Wait until the post-parse task finishes (callbacks fire on this loop).
            await ds_handle.wait()
        except Exception as exc:
            self._on_agent_failure(str(exc))
            return

        if ds_handle.status == TaskStatus.SUCCESS:
            payload: dict[str, Any] = ds_handle.result or {}
        else:
            # The failure was already surfaced to the user via on_error.
            return

        data_store: dict[str, str] = dict(payload.get("data_store", {}))
        # Merge domain extras (SWE: toc, code_blocks, injection flags) into the
        # stored document so the renderer/status can surface them.
        extras = {k: v for k, v in payload.items() if k != "data_store"}
        if extras and self.active_document_name:
            source = self._active_source()
            loaded = self.loaded_documents.get(source or "")
            if loaded is not None:
                loaded.structured.metadata.update(extras)

        # --- Step 2: serialise the active domain's MCP server configs ---
        # any_agent MCP configs are Pydantic models; model_dump() yields a
        # broker-safe dict that the worker rehydrates into MCPParams.
        mcp_servers: list[dict[str, Any]] = [
            cfg.model_dump() for cfg in self._mcp_configs_for(domain_id)
        ]

        # --- Step 3: submit the agent task ---
        def on_progress(status: TaskStatus, _payload: Any) -> None:
            if status == TaskStatus.STARTED:
                self.write_chat_message(
                    "[italic dim]Agent is running...[/italic dim]",
                    persist=False,
                )

        def on_complete(status: TaskStatus, result: Any) -> None:
            if status == TaskStatus.SUCCESS and isinstance(result, AgentResult):
                self._handle_agent_result(result, user_query)
            elif status == TaskStatus.REVOKED:
                self._on_agent_failure("Agent task was cancelled.")
            with contextlib.suppress(KeyError):
                del self._active_agent_handles[user_query]

        try:
            agent_handle = await self.presenter.execute_agent(
                model_id=model_id,
                api_key=api_key,
                user_query=user_query,
                doc_context=self.doc_context,
                active_document_name=self.active_document_name,
                data_store=data_store,
                mcp_servers=mcp_servers,
                thread_id=self.active_document_name or None,
                domain_id=domain_id,
                queue=self._active_queue(),
                on_progress=on_progress,
                on_complete=on_complete,
                on_error=self._on_agent_failure,
            )
        except Exception as exc:
            self._on_agent_failure(str(exc))
            return

        # Wait for the agent task to finish.  on_complete (which calls
        # _handle_agent_result) fires before wait() returns.
        await agent_handle.wait()

    def _handle_agent_result(self, result: AgentResult, user_query: str) -> None:
        """Process the agent result (mirrors the old _execute_agent_background)."""
        if result.is_blocked:
            logger.info("Agent result: blocked — %s", result.blocked_reason)
            self._on_agent_blocked(result.blocked_reason)
            return

        if result.pii_redacted_query:
            pii_msg = "[italic dim]PII detected and redacted from query.[/italic dim]"
            self.write_chat_message(pii_msg, persist=True)
            self.write_agent_response(pii_msg)

        if not result.success:
            logger.info("Agent result: failure — %s", result.error_message)
            self._on_agent_failure(result.error_message)
            return

        logger.info("Agent result: success — %d chars, delivering to TUI", len(result.response))

        if result.math_discrepancies:
            disc_msg = (
                "[bold yellow]System: Guardrail checks on the agent output "
                "flagged the following:[/bold yellow]"
            )
            self.write_chat_message(disc_msg, persist=True)
            self.write_agent_response(disc_msg)
            for discrepancy in result.math_discrepancies:
                self.write_chat_message(f"  [yellow]⚠[/yellow] {discrepancy}", persist=True)
                self.write_agent_response(f"  [yellow]⚠[/yellow] {discrepancy}")

        self._on_agent_success(result.response, user_query)

    def _on_agent_success(self, response: str, query: str) -> None:
        # Render the body verbatim (no markup parsing) so ASCII charts and
        # structured payloads display as produced, not as Rich markup.
        self.write_agent_message("[bold green]Agent:[/bold green] ", response)
        chat_logger.info("Agent response (%d chars)", len(response))

        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="agent_response",
            action="ALLOWED",
            input_text=query,
        )

    def _on_agent_blocked(self, reason: str) -> None:
        msg = (
            f"[bold red]✗ Input Blocked:[/bold red] "
            f"Prompt blocked by guardrail system: {escape(extract_message(reason))}"
        )
        self.write_chat_message(msg, persist=True)
        self.write_agent_response(msg)
        chat_logger.warning("Agent BLOCKED: %s", reason)
        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="user_query",
            action="BLOCKED",
            risk_score=1.0,
        )

    def _on_agent_failure(self, error_message: str) -> None:
        display_message = escape(extract_message(error_message))
        msg = f"[bold red]✗ Agent execution failed:[/bold red] {display_message}"
        self.write_chat_message(msg, persist=True)
        self.write_agent_response(msg)
        chat_logger.error("Agent FAILURE: %s", error_message)
        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="agent_response",
            action="FAILURE",
            risk_score=0.5,
        )

    def _rebuild_tabs(self) -> None:
        tabs = self.query_one("#document-tabs", Tabs)
        valid_tab_ids: set[str] = set()
        for idx, source in enumerate(self.ingestion_history):
            doc = self.loaded_documents.get(source)
            if doc:
                tab_id = f"tab_{idx}"
                valid_tab_ids.add(tab_id)
                existing_tabs = tabs.query(f"#{tab_id}")
                if existing_tabs:
                    tab = existing_tabs.first(Tab)
                    if tab and tab.label != doc.file_name:
                        tab.label = doc.file_name
                else:
                    tabs.add_tab(Tab(doc.file_name, id=tab_id))

        for tab in list(tabs.query(Tab)):
            if tab.id and tab.id not in valid_tab_ids:
                tab.remove()

    def _switch_to_document(self, doc: StructuredDocument, source: str) -> None:
        self.structured_doc = doc
        self.active_document_name = doc.file_name

        # Find document source + its domain
        doc_source = ""
        domain_id = "financial"
        for src, loaded in self.loaded_documents.items():
            if loaded.structured == doc:
                doc_source = src
                domain_id = loaded.domain_id
                break

        # The agent context is domain-built (large SWE docs get TOC + first
        # chunks + search instructions instead of the full raw markdown).
        self.doc_context = get_domain(domain_id).context_for(doc)

        # Widget lookups can fail during app teardown (e.g. a queued
        # TabActivated message processed after unmount begins) — tolerate it.
        if doc_source:
            with contextlib.suppress(Exception):
                self.query_one("#doc-source", Input).value = doc_source

        # Update center rendering pane with the domain's renderer
        render_pane = self.query_one("#render-pane", DocumentRenderPane)
        render_pane.update_document(doc, domain_id)

        # Reflect this document's page-slice state in the pager controls.
        self._update_pager(doc_source)

        # Refresh the skills list for the active domain
        self._refresh_skills_list(domain_id)

        # Sync selection across UI elements
        if doc_source:
            try:
                idx = self.ingestion_history.index(doc_source)
                if source in ("list", "ingest"):
                    tabs = self.query_one("#document-tabs", Tabs)
                    tabs.active = f"tab_{idx}"
                if source in ("tab", "ingest"):
                    list_view = self.query_one("#ingest-history", ListView)
                    list_view.index = idx
            except ValueError:
                pass

        # Clear agent response pane when switching documents
        self.query_one("#agent-response-pane", RichLog).clear()

        # Clear and restore active document's chat log
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.clear()
        # If we have no in-memory history yet, try loading from disk
        if doc.file_name not in self.chat_histories:
            self.chat_histories[doc.file_name] = self._load_chat_log(doc.file_name)
        history = self.chat_histories.get(doc.file_name, [])
        if history:
            for msg_markup in history:
                chat_log.write(msg_markup)
        else:
            char_count = len(doc.raw_markdown)
            msg = (
                f"[bold green]✓ Document loaded successfully.[/bold green] "
                f"Character count: {char_count}"
            )
            chat_log.write(msg)
            self.chat_histories[doc.file_name] = [msg]
            self._save_chat_log(doc.file_name)
            chat_logger.info("Document loaded: %s (%d chars)", doc.file_name, char_count)
            logger.info(
                f"Successfully loaded and parsed structured document: "
                f"{doc.file_name} with {char_count} characters."
            )
            AuditLogger.log(
                document_id=doc.file_name,
                operation="document_ingested",
                action="SUCCESS",
            )

        self.query_one("#chat-input", Input).focus()
        self._update_ui_state()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if not event.tab or not event.tab.id:
            return

        if event.tabs.id == "view-tabs":
            doc_container = self.query_one("#doc-view-container")
            agent_pane = self.query_one("#agent-response-pane")
            if event.tab.id == "view-doc":
                doc_container.styles.display = "block"
                agent_pane.styles.display = "none"
            elif event.tab.id == "view-agent":
                doc_container.styles.display = "none"
                agent_pane.styles.display = "block"
                agent_pane.focus()
            return

        try:
            _, idx_str = event.tab.id.split("_", 1)
            idx = int(idx_str)
        except (ValueError, AttributeError):
            return

        if 0 <= idx < len(self.ingestion_history):
            source = self.ingestion_history[idx]
            loaded = self.loaded_documents.get(source)
            if loaded and loaded.file_name != self.active_document_name:
                self._safe_switch(loaded.structured, source="tab")

    def _safe_switch(self, doc: StructuredDocument, source: str) -> None:
        """Switch documents, tolerating a partially-torn-down DOM.

        A queued ``TabActivated``/selection message can be processed after
        unmount begins (e.g. a tab activation scheduled during document load);
        the widget lookups then fail.  Those messages are harmless at that
        point — log and move on rather than letting them crash the app.
        """
        try:
            self._switch_to_document(doc, source)
        except Exception:
            logger.debug("Document switch (%s) ignored during teardown", source, exc_info=True)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "ingest-history" and event.index is not None:
            idx = event.index
            if 0 <= idx < len(self.ingestion_history):
                source = self.ingestion_history[idx]
                loaded = self.loaded_documents.get(source)
                if loaded and loaded.file_name != self.active_document_name:
                    self._safe_switch(loaded.structured, source="list")
