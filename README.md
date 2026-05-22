This is a fantastic stack for a local AI document assistant. You are combining **Textual** (the gold standard for Python Text User Interfaces) with IBM's powerful **Docling** for document ingestion, and Mozilla.ai's choice-first stack (**any-guardrail** and **any-agent**) to safely chat with your data.

Here is a step-by-step blueprint and a fully functional code architectural template to achieve this.

### Architectural Blueprint

```
[ Textual TUI Interface ] 
       │ (Input: Path/URL)
       ▼
[ Docling Converter ] ──► [ Markdown String Parsing ]
                                    │
                                    ▼
                          [ any-guardrail (Input Validation) ]
                                    │ (If Safe)
                                    ▼
                          [ any-agent / LLM Orchestration ]
                                    │
                                    ▼
                          [ any-guardrail (Output Validation) ]
                                    │
                                    ▼
                        [ Textual Chat Log Display ]

```

---

### Phase 1: Installation & Requirements

Ensure you install the necessary libraries. For Mozilla's toolchain, you also need an underlying agent framework/LLM (like OpenAI or a local llamafile) that `any-agent` hooks into.

```bash
pip install textual docling any-guardrail
# Install your preferred underlying agent runner if required by any-agent, e.g.:
# pip install openai 

```

---

### Phase 2: Building the TUI Architecture

Below is a single-file Python script implementing the Textual layout, handling file/URL entry, triggering a background worker thread to let Docling parse without freezing the UI, and processing the query through Mozilla's guardrails and agent stack.

```python
import os
import threading
from docling.document_converter import DocumentConverter
from any_guardrail import AnyGuardrail, GuardrailName, GuardrailOutput

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Input, Button, RichLog, Label
from textual.worker import get_current_worker

# NOTE: Since any-agent integrates tightly with standard OpenTelemetry/Framework architectures,
# we simulate the LLM call using a standard wrapper. Replace this with your actual any-agent executor setup.
def run_any_agent(context: str, prompt: str) -> str:
    # This is where your any-agent initialization and runtime execution goes
    # For demonstration, a placeholder response grounded by the text:
    return f"Based on the document context, here is the answer to: '{prompt}'."

class DoclingAgentTUI(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    .input-box {
        margin: 1;
        height: auto;
    }
    #chat-log {
        background: $surface;
        border: solid $primary;
        margin: 1;
        height: 1fr;
    }
    .status-bar {
        background: $accent;
        color: white;
        padding: 0 1;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        # Section 1: File Ingestion
        with Vertical(classes="input-box"):
            yield Label("Step 1: Enter Local File Path or URL")
            with Horizontal():
                yield Input(placeholder="e.g., https://arxiv.org/pdf/2408.09869 or ./report.pdf", id="doc-source")
                yield Button("Load & Parse", variant="primary", id="btn-parse")
        
        # Section 2: Chat Interface
        with Vertical(classes="input-box"):
            yield Label("Step 2: Ask questions about the document")
            with Horizontal():
                yield Input(placeholder="Ask anything...", id="chat-input", disabled=True)
                yield Button("Send", variant="success", id="btn-send", disabled=True)
        
        # Chat Outputs
        yield RichLog(id="chat-log", highlight=True, markup=True)
        yield Label("Status: Waiting for document ingestion...", id="status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.doc_context = ""
        self.converter = DocumentConverter()
        # Initialize Mozilla.ai Guardrail (using DEEPSET or another provider)
        try:
            self.guardrail = AnyGuardrail.create(GuardrailName.DEEPSET)
        except Exception:
            # Fallback placeholder if HuggingFace credentials are not configured locally
            self.guardrail = None

        self.query_buttons_toggle(False)

    def query_buttons_toggle(self, enable: bool) -> None:
        self.query_one("#chat-input").disabled = not enable
        self.query_one("#btn-send").disabled = not enable

    #### EVENT HANDLERS ####

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-parse":
            source = self.query_one("#doc-source", Input).value.strip()
            if source:
                self.query_one("#status", Label).update("Status: Parsing document via Docling (running in background)...")
                event.button.disabled = True
                # Run Docling conversion in a Textual background thread to keep UI interactive
                self.run_worker(self.parse_document(source), thread=True)
                
        elif event.button.id == "btn-send":
            self.process_chat()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-input" and not event.input.disabled:
            self.process_chat()

    #### BACKGROUND WORKERS & PROCESSING ####

    async def parse_document(self, source: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        try:
            # Running synchronous Docling processing inside a worker thread safely
            result = self.converter.convert(source)
            self.doc_context = result.document.export_to_markdown()
            
            # Update UI on completion safely via main loop call
            self.call_from_thread(self.parsing_complete, len(self.doc_context))
        except Exception as e:
            self.call_from_thread(self.parsing_failed, str(e))

    def parsing_complete(self, doc_length: int) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold green]✓ Document loaded successfully.[/bold green] Character count: {doc_length}")
        self.query_one("#status", Label).update("Status: Document ready. Ask your questions below.")
        self.query_one("#btn-parse").disabled = False
        self.query_buttons_toggle(True)

    def parsing_failed(self, error_msg: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold red]✗ Parsing failed:[/bold red] {error_msg}")
        self.query_one("#status", Label).update("Status: Error during extraction.")
        self.query_one("#btn-parse").disabled = False

    def process_chat(self) -> None:
        input_widget = self.query_one("#chat-input", Input)
        user_query = input_widget.value.strip()
        if not user_query:
            return

        log = self.query_one("#chat-log", RichLog)
        log.write(f"\n[bold blue]You:[/bold blue] {user_query}")
        input_widget.value = "" # Clear input box

        # 1. Apply any-guardrail to inspect the Input Prompt
        if self.guardrail:
            guard_result: GuardrailOutput = self.guardrail.validate(user_query)
            if not guard_result.valid:
                log.write(f"[bold red]Mozilla Any-Guardrail Alert (Input Blocked):[/bold red] {guard_result.explanation}")
                return

        # 2. Query execution (Ideally you feed `self.doc_context` to your agent)
        # Wrapping agent calling process here
        log.write("[italic dim]Agent is thinking...[/italic dim]")
        
        # Here we run any-agent logic
        agent_response = run_any_agent(self.doc_context, user_query)

        # 3. Apply any-guardrail to inspect Output Response
        if self.guardrail:
            output_guard: GuardrailOutput = self.guardrail.validate(agent_response)
            if not output_guard.valid:
                log.write(f"[bold red]Mozilla Any-Guardrail Alert (Output Filtered):[/bold red] Prompt generated unsafe content.")
                return

        # Print safe response to log
        log.write(f"[bold green]Agent:[/bold green] {agent_response}")

if __name__ == "__main__":
    app = DoclingAgentTUI()
    app.run()

```

---

### Crucial Implementation Notes

1. **Textual Thread Management (`run_worker`):**
Docling relies on hefty under-the-hood OCR/Layout machine learning models (like TableFormer). If you invoke `converter.convert()` directly on the main thread, your entire TUI application window will freeze completely while it runs. Using `self.run_worker(..., thread=True)` forces Textual to spawn it off safely into an asymmetric background state.
2. **Mozilla's `any-guardrail` Check:**
In the `process_chat` step, the code verifies queries via `guardrail.validate()`. If someone tries a jailbreak or enters malicious text, `any-guardrail` flags `valid=False`, letting you intercept the text cleanly inside the TUI before sending a payload over to `any-agent`.
3. **Docling Input Ingestion Flexibility:**
The `DocumentConverter` seamlessly flags inputs whether they are local file system pathways (`./docs/paper.pdf`) or full remote HTTP web locations (`https://...`). Docling handles the underlying fetch request, making secondary HTTP download handlers unnecessary.