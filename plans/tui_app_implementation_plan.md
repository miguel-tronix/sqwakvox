# Implementation Plan: TUI Python Application Shell

This plan outlines the design, layout, state management, and keyboard navigation systems for the Textual-based terminal user interface of Sqwakvox.

---

## 1. Objectives & Focus

1. **Textual Layout**: Design a premium terminal interface using three distinct logical panes (Ingestion sidebar, Chat log, Document Render view).
2. **Background Threading Model**: Standardize asynchronous background parsing via Textual's worker system to ensure the UI remains fully responsive.
3. **Reactive State Variables**: Connect document ingestion, parsing progress, and error reports into reactive properties that automatically trigger re-renders.
4. **Keyboard Bindings & Access**: Define hotkeys for navigating panels, loading documents, and clearing histories.

---

## 2. Textual UI Layout & Wireframe

The terminal layout is divided into a three-column grid for high efficiency:

```
┌────────────────────────────────────────────────────────────────────────┐
│                          SQWAKVOX FINANCIAL TUI                        │
├───────────────────────┬──────────────────────┬─────────────────────────┤
│ [ SIDEBAR ]           │ [ RENDER VIEW ]      │ [ SECURED CHAT LOG ]    │
│                       │                      │                         │
│ Load File/URL:        │ Q2 Revenue Summary:  │ User: What is variance? │
│ [___________________] │                      │                         │
│                       │   ╔═══════════════╗  │ Agent: The variance in  │
│ Ingest History:       │   ║ Metric ║ Q2   ║  │ Q2 revenue is +9.4%.    │
│  • q2_26.pdf (Ready)  │   ╠═══════════════╣  │                         │
│  • balance.xlsx (Err) │   ║ Rev    ║ 136.2║  │                         │
│                       │   ╚═══════════════╝  │                         │
├───────────────────────┴──────────────────────┴─────────────────────────┤
│ Status: Idle | Ready                        │ [Input: Ask a question]  │
└────────────────────────────────────────────────────────────────────────┘
```

> [!TIP]
> **Adaptive Grid Design**: We utilize Textual's CSS-Grid layouts (`layout: grid; grid-size: 3;`) to ensure the panels resize smoothly based on the host terminal's width, collapsing panels automatically on smaller screens.

---

## 3. Background Threading Model (Textual Workers)

IBM Docling layout and OCR extraction models block Python's synchronous global interpreter lock (GIL). If run directly inside event handlers (e.g., `on_button_pressed`), the interface will freeze.

### Textual Background Execution Flow:
1. When a user clicks **Load & Parse**, the TUI spawns a background worker thread via `self.run_worker(self._perform_parsing(source), thread=True)`.
2. The UI enters a loading state, animating a status spinner.
3. The background thread invokes the `DocumentConverter` and parses the file.
4. On success, the thread schedules a UI update on the main thread loop using `self.call_from_thread(self._on_parsing_complete, parsed_doc)`.

```python
from docling.document_converter import DocumentConverter
from textual.app import App
from textual.worker import Worker, get_current_worker

class SqwakvoxApp(App):
    # ... composition and setup ...

    def action_parse_document(self, file_path_or_url: str) -> None:
        """Spawn background worker thread to convert document."""
        self.is_parsing = True
        self.active_error = None
        
        # Spawn off the worker thread
        self.run_worker(
            self.convert_document_in_background(file_path_or_url),
            thread=True,
            name="docling_parser"
        )

    async def convert_document_in_background(self, source: str) -> None:
        """Runs in separate OS thread; GIL-bound Docling work goes here."""
        worker = get_current_worker()
        try:
            converter = DocumentConverter()
            result = converter.convert(source)
            doc_md = result.document.export_to_markdown()
            
            # Safely push results back to main thread
            if not worker.is_cancelled:
                self.call_from_thread(self.on_parse_success, doc_md)
        except Exception as e:
            if not worker.is_cancelled:
                self.call_from_thread(self.on_parse_failure, str(e))

    def on_parse_success(self, markdown_text: str) -> None:
        self.is_parsing = False
        # Update render pane and enable query panel...

    def on_parse_failure(self, error_message: str) -> None:
        self.is_parsing = False
        self.active_error = error_message
```

---

## 4. State Management (Reactive Properties)

We leverage Textual's native `reactive` properties to update UI components when state values mutate:

```python
from textual.reactive import reactive

class SqwakvoxApp(App):
    # Reactive properties that trigger automatic UI updates when changed
    is_parsing = reactive(False)
    active_document_name = reactive("")
    active_error = reactive(None)

    def watch_is_parsing(self, is_parsing: bool) -> None:
        """Watch method that automatically toggles input controls and loading spinner."""
        status_bar = self.query_one("#status-label")
        spinner = self.query_one("#loading-spinner")
        
        if is_parsing:
            status_bar.update("Status: Processing Layout via Docling...")
            spinner.visible = True
            self.query_one("#chat-input").disabled = True
        else:
            status_bar.update("Status: Idle")
            spinner.visible = False
            self.query_one("#chat-input").disabled = False

    def watch_active_error(self, error: str | None) -> None:
        """Watch method that highlights system faults in the UI."""
        error_pane = self.query_one("#error-banner")
        if error:
            error_pane.update(f"[bold white on red]Error: {error}[/]")
            error_pane.visible = True
        else:
            error_pane.visible = False
```

---

## 5. Keyboard Navigation & Bindings

To preserve an ultra-fast, premium terminal experience, Sqwakvox will support comprehensive keyboard navigation:

### Key Bindings Reference:

| Key | Action | Context |
|---|---|---|
| `q` / `ctrl+q` | Quit Application | Global |
| `ctrl+l` | Load new document | Focuses Path Input |
| `ctrl+f` | Focus Chat box | Focuses Query Input |
| `ctrl+r` | Clear active chat | Resets context and history |
| `tab` | Move panel focus | Sidebar -> Render View -> Chat Log |
| `up` / `down` | Scroll Active Pane | Moves viewport up/down |
| `ctrl+x` | Cross-validate numerical rules | Triggers Guardrail Rule Checker |
