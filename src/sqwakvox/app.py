from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

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
)
from textual.worker import get_current_worker

from sqwakvox.controller import AppController, extract_message
from sqwakvox.guardrails import AuditLogger
from sqwakvox.models import ModelProvider, StructuredDocument
from sqwakvox.renderer import DocumentRenderPane

logger = logging.getLogger(__name__)
chat_logger = logging.getLogger("sqwakvox.chat")

CSS = """
Screen {
    layout: grid;
    grid-size: 3;
    grid-columns: 1fr 2.6fr 1.4fr;
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

#render-pane {
    border: solid $secondary;
    padding: 1;
    background: $surface;
    overflow-y: auto;
}

#render-pane:focus {
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
    overflow-x: auto;
}

#chat-input {
    height: 3;
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

    def __init__(self, controller=None) -> None:
        super().__init__()
        self.controller = controller or AppController()
        self.doc_context: str = ""
        self.structured_doc: StructuredDocument | None = None
        self.ingestion_history: list[str] = []

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
                value="openai:gpt-4o-mini",
                id="model-selector",
            )
            yield Label("Enter Provider API Key:")
            yield Input(
                placeholder="sk-...",
                password=True,
                id="api-key-input",
            )

            yield Label("[bold]Ingest History[/bold]", id="history-label")
            yield ListView(id="ingest-history")

        yield DocumentRenderPane(id="render-pane")

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
        chat_log.write("[italic]Chat cleared.[/italic]")
        chat_logger.info("Chat log cleared by user")

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
            chat_log = self.query_one("#chat-log", RichLog)
            chat_log.write("[bold yellow]No document loaded to cross-validate.[/bold yellow]")
            return

        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write("\n[bold underline]Numerical Cross-Validation[/bold underline]")

        results = self.controller.cross_validate(self.structured_doc)
        for col_name, expected, actual, is_valid in results:
            if is_valid:
                chat_log.write(
                    f"  [green]✓[/green] Column '{col_name}' "
                    f"sums to {expected} (actual: {actual:.2f})"
                )
            else:
                chat_log.write(
                    f"  [red]✗[/red] Column '{col_name}' expected {expected} but got {actual:.2f}"
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
        self.is_parsing = True
        self.active_error = None

        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"\n[italic dim]Starting layout ingestion for: {source}...[/italic dim]")
        chat_log.write(
            "[italic dim]Initializing Docling Parser (this may take a few seconds)...[/italic dim]"
        )
        logger.info(f"Initiated parsing for document source: {source}")

        self.run_worker(
            lambda: self._convert_document_in_background(source),
            thread=True,
            name="docling_parser",
        )

    def _convert_document_in_background(self, source: str) -> None:
        worker = get_current_worker()
        try:
            structured = self.controller.convert_document(source, lambda: worker.is_cancelled)
            if structured:
                self.call_from_thread(self._on_parse_success, structured, source)
        except Exception as e:
            if not worker.is_cancelled:
                self.call_from_thread(self._on_parse_failure, str(e))

    def _on_parse_success(self, structured: StructuredDocument, source: str) -> None:
        self.structured_doc = structured
        self.doc_context = structured.raw_markdown
        self.active_document_name = structured.file_name
        self.is_parsing = False

        if source not in self.ingestion_history:
            self.ingestion_history.append(source)
            history_list = self.query_one("#ingest-history", ListView)
            history_list.append(ListItem(Label(f"• {structured.file_name} (Ready)")))

        render_pane = self.query_one("#render-pane", DocumentRenderPane)
        render_pane.update_document(structured)

        chat_log = self.query_one("#chat-log", RichLog)
        char_count = len(structured.raw_markdown)
        chat_log.write(
            f"[bold green]✓ Document loaded successfully.[/bold green] "
            f"Character count: {char_count}"
        )
        chat_logger.info("Document loaded: %s (%d chars)", structured.file_name, char_count)

        logger.info(
            f"Successfully loaded and parsed structured document: "
            f"{structured.file_name} with {char_count} characters."
        )

        AuditLogger.log(
            document_id=structured.file_name,
            operation="document_ingested",
            action="SUCCESS",
        )

        self.query_one("#chat-input", Input).focus()
        self._update_ui_state()

    def _on_parse_failure(self, error_message: str) -> None:
        self.is_parsing = False
        self.active_error = error_message
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(
            f"[bold red]✗ Parsing failed:[/bold red] {escape(extract_message(error_message))}"
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

        chat_log = self.query_one("#chat-log", RichLog)
        chat_logger.info("User query: %s", user_query)
        chat_log.write(f"\n[bold blue]You:[/bold blue] {user_query}")
        chat_input.value = ""

        selected_model = self.query_one("#model-selector", Select).value
        if selected_model is None or not isinstance(selected_model, str):
            chat_log.write(
                "[bold yellow]System: Please select a valid model configuration.[/bold yellow]"
            )
            return

        api_key = self.query_one("#api-key-input", Input).value.strip()

        if not api_key:
            chat_log.write(
                "[bold yellow]System: Warning! API Key is missing. "
                "Please provide a valid key in the sidebar.[/bold yellow]"
            )
            return

        if len(api_key) < 10:
            chat_log.write(
                "[bold red]System: Invalid API Key. The provided key is too short.[/bold red]"
            )
            return

        chat_logger.info("Agent invoked — model: %s", selected_model)
        chat_log.write("[italic dim]Agent is thinking (via LangChain)...[/italic dim]")

        self.run_worker(
            lambda: self._execute_agent_background(selected_model, api_key, user_query),
            thread=True,
            name="any_agent_worker",
        )

    def _execute_agent_background(self, model_id: str, api_key: str, user_query: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)

        data_store = self.controller.build_financial_data_store(self.structured_doc)
        result = self.controller.execute_agent(
            model_id=model_id,
            api_key=api_key,
            user_query=user_query,
            doc_context=self.doc_context,
            active_document_name=self.active_document_name,
            data_store=data_store,
        )

        if result.is_blocked:
            self.call_from_thread(self._on_agent_blocked, result.blocked_reason)
            return

        if result.pii_redacted_query:
            chat_log.write("[italic dim]PII detected and redacted from query.[/italic dim]")

        if not result.success:
            self.call_from_thread(self._on_agent_failure, result.error_message)
            return

        if result.math_discrepancies:
            chat_log.write(
                "[bold yellow]System: Numerical discrepancies detected between "
                "agent assertions and parsed tables![/bold yellow]"
            )
            for discrepancy in result.math_discrepancies:
                chat_log.write(f"  [yellow]⚠[/yellow] {discrepancy}")

        self.call_from_thread(self._on_agent_success, result.response, user_query)

    def _on_agent_success(self, response: str, query: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"[bold green]Agent:[/bold green] {response}")
        chat_logger.info("Agent response (%d chars)", len(response))

        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="agent_response",
            action="ALLOWED",
            input_text=query,
        )

    def _on_agent_blocked(self, reason: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(
            f"[bold red]✗ Input Blocked:[/bold red] "
            f"Prompt blocked by guardrail system: {escape(extract_message(reason))}"
        )
        chat_logger.warning("Agent BLOCKED: %s", reason)
        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="user_query",
            action="BLOCKED",
            risk_score=1.0,
        )

    def _on_agent_failure(self, error_message: str) -> None:
        display_message = extract_message(error_message)
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"[bold red]✗ Agent execution failed:[/bold red] {escape(display_message)}")
        chat_logger.error("Agent FAILURE: %s", error_message)
        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="agent_response",
            action="FAILURE",
            risk_score=0.5,
        )
