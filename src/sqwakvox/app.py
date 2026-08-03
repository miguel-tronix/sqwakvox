from __future__ import annotations

import contextlib
import json
import logging
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
from sqwakvox.guardrails import AuditLogger
from sqwakvox.models import ModelProvider, StructuredDocument
from sqwakvox.presenter import Presenter, TaskStatus
from sqwakvox.renderer import DocumentRenderPane
from sqwakvox.telemetry import get_telemetry

logger = logging.getLogger(__name__)
chat_logger = logging.getLogger("sqwakvox.chat")

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
        self._active_parse_handle: Worker[None] | None = None
        self._active_agent_handles: dict[str, Worker[None]] = {}
        self.doc_context: str = ""
        self.structured_doc: StructuredDocument | None = None
        self.ingestion_history: list[str] = []
        self.loaded_documents: dict[str, StructuredDocument] = {}
        self.chat_histories: dict[str, list[str]] = {}
        self._chat_log_dir = Path.home() / ".sqwakvox_chat_logs"
        self._chat_log_dir.mkdir(parents=True, exist_ok=True)
        self.mcp_configs: list[tuple[str, Any]] = []

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

            yield Label("[bold]Ingest History[/bold]", id="history-label")
            yield ListView(id="ingest-history")

        with Vertical(id="center-column"):
            yield Tabs(id="document-tabs")
            yield Tabs(
                Tab("Document", id="view-doc"),
                Tab("Agent Response", id="view-agent"),
                id="view-tabs",
            )
            yield DocumentRenderPane(id="render-pane")
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
                            self.mcp_configs.append((name, mcp_opt))
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
            for name, config in self.mcp_configs:
                list_view.append(
                    ListItem(
                        Label(f"🟢 [bold]{name}[/bold] ({config.command})"),
                        id=f"mcp-item-{name}",
                    )
                )

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

        if self.is_parsing:
            status_bar.update("Status: Processing Layout via Docling...")
            spinner.visible = True
            chat_input.disabled = True
            btn_send.disabled = True
            btn_parse.disabled = True
        else:
            is_ready = bool(self.doc_context)
            status_bar.update("Status: Idle | Ready" if is_ready else "Status: Idle")
            spinner.visible = False
            chat_input.disabled = not is_ready
            btn_send.disabled = not is_ready
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

        # If a parse is already in flight, do nothing.
        if self._active_parse_handle is not None:
            self.write_chat_message(
                "[bold yellow]A document parse is already in progress.[/bold yellow]",
                persist=False,
            )
            return

        self.is_parsing = True
        self.active_error = None

        self.write_chat_message(
            f"\n[italic dim]Starting layout ingestion for: {source}...[/italic dim]",
            persist=False,
        )
        self.write_chat_message(
            "[italic dim]Initializing Docling Parser (this may take a few seconds)...[/italic dim]",
            persist=False,
        )
        logger.info("Initiated parsing for document source: %s", source)

        # Textual workers are asyncio Tasks on the same event loop as the
        # presenter, so we can await presenter calls directly.
        self._active_parse_handle = self.run_worker(
            self._dispatch_parse(source),
            name="docling_parser",
        )

    async def _dispatch_parse(self, source: str) -> None:
        """Async Textual worker that delegates document parsing to the
        Presenter (which talks to Celery in a background thread)."""

        def on_progress(status: TaskStatus, _payload: Any) -> None:
            if status == TaskStatus.STARTED:
                self.write_chat_message(
                    "[italic dim]Docling parser is running...[/italic dim]",
                    persist=False,
                )

        def on_complete(status: TaskStatus, payload: Any) -> None:
            if status == TaskStatus.SUCCESS:
                if payload is None:
                    self._on_parse_failure("Parse was cancelled.")
                else:
                    self._on_parse_success(payload, source)
            elif status == TaskStatus.FAILURE:
                self._on_parse_failure(payload if isinstance(payload, str) else str(payload))
            elif status in (TaskStatus.REVOKED, TaskStatus.CANCELLED):
                self._on_parse_failure("Parse was cancelled.")
            self._active_parse_handle = None

        try:
            await self.presenter.parse_document(
                source=source,
                on_progress=on_progress,
                on_complete=on_complete,
            )
        except Exception as exc:
            self._active_parse_handle = None
            self._on_parse_failure(str(exc))

    def _on_parse_success(self, structured: StructuredDocument, source: str) -> None:
        self.is_parsing = False
        self.loaded_documents[source] = structured
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

    def _on_parse_failure(self, error_message: str) -> None:
        self.is_parsing = False
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

        The flow: first build the financial data store from the parsed doc
        (also a Celery task), then submit the agent task.  Both are polled
        by the presenter and callbacks update the UI directly on this loop.
        """

        # --- Step 1: build the financial data store (Celery task) ---
        doc = self.structured_doc
        if doc is None:
            self._on_agent_failure("No document loaded.")
            return

        try:
            ds_handle = await self.presenter.build_data_store(
                document=doc,
                on_error=self._on_agent_failure,
            )
            # Wait until the data-store task finishes (callbacks fire on this loop).
            await ds_handle.wait()
        except Exception as exc:
            self._on_agent_failure(str(exc))
            return

        if ds_handle.status == TaskStatus.SUCCESS:
            data_store: dict[str, str] = ds_handle.result or {}
        else:
            # The failure was already surfaced to the user via on_error.
            return

        # --- Step 2: serialise MCP server configs for the broker ---
        # any_agent MCP configs are Pydantic models; model_dump() yields a
        # broker-safe dict that the worker rehydrates into MCPParams.
        mcp_servers: list[dict[str, Any]] = [cfg.model_dump() for _, cfg in self.mcp_configs]

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
                "[bold yellow]System: Numerical discrepancies detected between "
                "agent assertions and parsed tables![/bold yellow]"
            )
            self.write_chat_message(disc_msg, persist=True)
            self.write_agent_response(disc_msg)
            for discrepancy in result.math_discrepancies:
                self.write_chat_message(f"  [yellow]⚠[/yellow] {discrepancy}", persist=True)
                self.write_agent_response(f"  [yellow]⚠[/yellow] {discrepancy}")

        self._on_agent_success(result.response, user_query)

    def _on_agent_success(self, response: str, query: str) -> None:
        self.write_chat_message(f"[bold green]Agent:[/bold green] {response}", persist=True)
        self.write_agent_response(f"[bold green]Agent:[/bold green] {response}")
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
        tabs.clear()
        for idx, source in enumerate(self.ingestion_history):
            doc = self.loaded_documents.get(source)
            if doc:
                tab_id = f"tab_{idx}"
                tabs.add_tab(Tab(doc.file_name, id=tab_id))

    def _switch_to_document(self, doc: StructuredDocument, source: str) -> None:
        self.structured_doc = doc
        self.doc_context = doc.raw_markdown
        self.active_document_name = doc.file_name

        # Find document source
        doc_source = ""
        for src, d in self.loaded_documents.items():
            if d == doc:
                doc_source = src
                break

        if doc_source:
            self.query_one("#doc-source", Input).value = doc_source

        # Update center rendering pane
        render_pane = self.query_one("#render-pane", DocumentRenderPane)
        render_pane.update_document(doc)

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
            render_pane = self.query_one("#render-pane")
            agent_pane = self.query_one("#agent-response-pane")
            if event.tab.id == "view-doc":
                render_pane.styles.display = "block"
                agent_pane.styles.display = "none"
            elif event.tab.id == "view-agent":
                render_pane.styles.display = "none"
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
            doc = self.loaded_documents.get(source)
            if doc and doc.file_name != self.active_document_name:
                self._switch_to_document(doc, source="tab")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "ingest-history" and event.index is not None:
            idx = event.index
            if 0 <= idx < len(self.ingestion_history):
                source = self.ingestion_history[idx]
                doc = self.loaded_documents.get(source)
                if doc and doc.file_name != self.active_document_name:
                    self._switch_to_document(doc, source="list")
