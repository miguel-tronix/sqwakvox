from __future__ import annotations

from pathlib import Path

from docling.document_converter import DocumentConverter
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button,
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

from sqwakvox.guardrails import AuditLogger, FinancialRuleEngine, PIIRedactor
from sqwakvox.models import ModelProvider, StructuredDocument, TableData
from sqwakvox.renderer import DocumentRenderPane

CSS = """
Screen {
    layout: grid;
    grid-size: 3;
    grid-columns: 1fr 2fr 2fr;
}

#sidebar {
    border: solid $primary;
    padding: 1;
    background: $surface;
}

#sidebar Label {
    margin-bottom: 1;
}

#doc-source {
    margin-bottom: 1;
}

#ingest-history {
    height: 1fr;
    margin-top: 1;
}

#render-pane {
    border: solid $secondary;
    padding: 1;
    background: $surface;
    overflow-y: auto;
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
}

#status-bar {
    column-span: 3;
    height: 1;
    background: $accent;
    color: white;
    padding: 0 1;
}

#loading-spinner {
    column-span: 3;
    height: 1;
    dock: bottom;
}

#error-banner {
    column-span: 3;
    height: auto;
    visibility: hidden;
}
"""


class SqwakvoxApp(App):
    CSS = CSS

    BINDINGS = [
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

    def __init__(self) -> None:
        super().__init__()
        self.doc_context: str = ""
        self.structured_doc: StructuredDocument | None = None
        self.converter = DocumentConverter()
        self.ingestion_history: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="sidebar"):
            yield Label("[bold]Load Document[/bold]")
            yield Input(
                placeholder="File path or URL...",
                id="doc-source",
            )
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
            yield RichLog(id="chat-log", highlight=True, markup=True)
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
            status_bar.update(
                "Status: Idle | Ready" if is_ready else "Status: Idle"
            )
            spinner.visible = False
            chat_input.disabled = not is_ready
            btn_send.disabled = not is_ready
            btn_parse.disabled = False

    def watch_is_parsing(self, new_value: bool) -> None:
        self._update_ui_state()

    def watch_active_error(self, error: str | None) -> None:
        error_pane = self.query_one("#error-banner", Label)
        if error:
            error_pane.update(f"[bold white on red]Error: {error}[/]")
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
        self.call_from_thread(chat_log.write, "[italic]Chat cleared.[/italic]")

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
            chat_log.write(
                "[bold yellow]No document loaded to cross-validate.[/bold yellow]"
            )
            return

        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(
            "\n[bold underline]Numerical Cross-Validation[/bold underline]"
        )

        for table in self.structured_doc.tables:
            for col_idx in range(len(table.headers)):
                values: list[float] = []
                for row in table.rows:
                    if col_idx < len(row):
                        cleaned = (
                            row[col_idx]
                            .replace("$", "")
                            .replace(",", "")
                            .replace("%", "")
                        )
                        try:
                            values.append(float(cleaned))
                        except ValueError:
                            break
                if values and len(values) >= 3:
                    expected = values[-1]
                    actual = values[:-1]
                    if FinancialRuleEngine.verify_column_sum(
                        actual, expected
                    ):
                        chat_log.write(
                            f"  [green]✓[/green] Column '{table.headers[col_idx]}' sums to {expected} "
                            f"(actual: {sum(actual):.2f})"
                        )
                    else:
                        chat_log.write(
                            f"  [red]✗[/red] Column '{table.headers[col_idx]}' expected {expected} "
                            f"but got {sum(actual):.2f}"
                        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-parse":
            self._handle_parse()
        elif event.button.id == "btn-send":
            self._handle_chat()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-input" and not event.input.disabled:
            self._handle_chat()

    def _handle_parse(self) -> None:
        source = self.query_one("#doc-source", Input).value.strip()
        if not source:
            return
        self.is_parsing = True
        self.active_error = None
        self.run_worker(
            self._convert_document_in_background(source),
            thread=True,
            name="docling_parser",
        )

    async def _convert_document_in_background(self, source: str) -> None:
        worker = self.get_current_worker()
        try:
            result = self.converter.convert(source)
            if worker.is_cancelled:
                return

            doc_md = result.document.export_to_markdown()
            doc_name = Path(source).name if "/" in source or "\\" in source else source

            tables: list[TableData] = []
            if hasattr(result.document, "tables") and result.document.tables:
                for tbl in result.document.tables:
                    headers = (
                        [cell.text for cell in tbl.header_row]
                        if hasattr(tbl, "header_row") and tbl.header_row
                        else []
                    )
                    rows = [
                        [cell.text for cell in row]
                        for row in tbl.rows
                        if hasattr(tbl, "rows")
                    ]
                    tables.append(
                        TableData(
                            headers=headers,
                            rows=rows,
                            title=getattr(tbl, "caption", None),
                        )
                    )

            structured = StructuredDocument(
                file_name=doc_name,
                raw_markdown=doc_md,
                tables=tables,
            )

            self.call_from_thread(self._on_parse_success, structured, source)
        except Exception as e:
            if not worker.is_cancelled:
                self.call_from_thread(self._on_parse_failure, str(e))

    def _on_parse_success(
        self, structured: StructuredDocument, source: str
    ) -> None:
        self.is_parsing = False
        self.structured_doc = structured
        self.doc_context = structured.raw_markdown
        self.active_document_name = structured.file_name

        if source not in self.ingestion_history:
            self.ingestion_history.append(source)
            history_list = self.query_one("#ingest-history", ListView)
            history_list.append(
                ListItem(Label(f"• {structured.file_name} (Ready)"))
            )

        render_pane = self.query_one("#render-pane", DocumentRenderPane)
        render_pane.update_document(structured)

        chat_log = self.query_one("#chat-log", RichLog)
        char_count = len(structured.raw_markdown)
        chat_log.write(
            f"[bold green]✓ Document loaded successfully.[/bold green] "
            f"Character count: {char_count}"
        )

        AuditLogger.log(
            document_id=structured.file_name,
            operation="document_ingested",
            action="SUCCESS",
        )

        self.query_one("#chat-input", Input).focus()

    def _on_parse_failure(self, error_message: str) -> None:
        self.is_parsing = False
        self.active_error = error_message
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(
            f"[bold red]✗ Parsing failed:[/bold red] {error_message}"
        )

        AuditLogger.log(
            document_id="unknown",
            operation="document_ingested",
            action="FAILURE",
            risk_score=1.0,
        )

    def _handle_chat(self) -> None:
        chat_input = self.query_one("#chat-input", Input)
        user_query = chat_input.value.strip()
        if not user_query:
            return

        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"\n[bold blue]You:[/bold blue] {user_query}")
        chat_input.value = ""

        selected_model = self.query_one("#model-selector", Select).value
        api_key = self.query_one("#api-key-input", Input).value.strip()

        if not api_key:
            chat_log.write(
                "[bold yellow]System: Warning! API Key is missing. "
                "Please provide a valid key in the sidebar.[/bold yellow]"
            )
            return

        chat_log.write("[italic dim]Agent is thinking (via LangChain)...[/italic dim]")

        self.run_worker(
            self._execute_agent_background(selected_model, api_key, user_query),
            thread=True,
            name="any_agent_worker",
        )

    async def _execute_agent_background(
        self, model_id: str, api_key: str, user_query: str
    ) -> None:
        chat_log = self.query_one("#chat-log", RichLog)

        redacted_query = PIIRedactor.redact_text(user_query)
        if redacted_query != user_query:
            chat_log.write(
                "[italic dim]PII detected and redacted from query.[/italic dim]"
            )

        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="user_query",
            guardrail_checks={"pii_redacted": redacted_query != user_query},
            action="ALLOWED",
            input_text=user_query,
        )

        env_var = ModelProvider.get_env_var(model_id)

        try:
            from sqwakvox.agent import AnyAgentOrchestrator

            agent_response = AnyAgentOrchestrator.execute_query(
                model_id=model_id,
                api_key=api_key,
                context=self.doc_context,
                prompt=redacted_query,
                env_var=env_var,
            )

            agent_redacted = PIIRedactor.redact_text(agent_response)

            self.call_from_thread(self._on_agent_success, agent_redacted, user_query)

        except Exception as e:
            self.call_from_thread(self._on_agent_failure, str(e))

    def _on_agent_success(self, response: str, query: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"[bold green]Agent:[/bold green] {response}")

        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="agent_response",
            action="ALLOWED",
            input_text=query,
        )

    def _on_agent_failure(self, error_message: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(
            f"[bold red]Agent execution failed:[/bold red] {error_message}"
        )
