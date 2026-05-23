# Implementation Plan: any-agent Integration with LangChain Backend

This implementation plan outlines the architectural blueprint, UI enhancement, and backend orchestration necessary to integrate the **any-agent** framework (with a **LangChain** backend) into the Sqwakvox application. It enables dynamic model selection from a predefined list and secure API key entry directly through the Textual Terminal User Interface.

---

## 1. Objectives & Architectural Overview

The primary objective is to transition from simulated agent execution to a fully functional, production-ready orchestrator powered by Mozilla's `any-agent` framework, leveraging `langchain` as the underlying executor.

### Key Goals:
1. **Universal Abstraction**: Write uniform agent logic once and execute it through the LangChain framework using `AnyAgent`.
2. **Dynamic Model Selection**: Allow users to swap LLMs (e.g., OpenAI, Anthropic, Mistral) on-the-fly via sidebar widgets.
3. **Secure Credential Injection**: Provide a sandboxed field in the TUI to input API keys, which are injected safely into runtime process environment variables.
4. **Tight Guardrail Sandwich**: Keep Mozilla's `any-guardrail` validation wrapped around the newly integrated LangChain execution cycle.

```
                  ┌──────────────────────────────────────────┐
                  │          Textual Sidebar Widgets         │
                  │  [ Model Selector ]  &  [ API Key Input ]│
                  └─────┬──────────────────────────────┬─────┘
                        │                              │ (Inject Key into os.environ)
                        ▼                              ▼
┌──────────────────┐  [ Input validation ]  ┌───────────────────────────────────┐
│ User Chat Prompt ├──► any-guardrail    ──►│ any-agent (AgentFramework.LANGCHAIN) │
└──────────────────┘  [ (PII & Injection) ] └──────────────┬────────────────────┘
                                                           │
                                                           ▼
┌──────────────────┐  [ Output validation ]  ┌─────────────┴─────────────────────┐
│ TUI Chat Display ◄── any-guardrail     ◄──│ LangChain Model Execution         │
└──────────────────┘  [ (Math & PII) ]       └───────────────────────────────────┘
```

> [!NOTE]
> **Soft Deprecation of any-agent**: Mozilla.ai has noted that `any-agent` is in soft deprecation in favor of their leaner package, `mozilla-ai-tinyagent`. However, `any-agent` remains fully operational for projects requiring a unified multi-framework API (like switching to smolagents or LlamaIndex later). This blueprint ensures Sqwakvox can support both `any-agent` (LangChain backend) and `tinyagent` seamlessly.

---

## 2. System Dependencies

To utilize the LangChain framework from `any-agent`, the `pyproject.toml` and lock files must be updated.

### A. Required Packages:
- **`any-agent[langchain]`**: Core framework adapter.
- **Provider-specific LangChain packages**:
  - `langchain-openai` (for GPT models)
  - `langchain-anthropic` (for Claude models)
  - `langchain-mistralai` (for Mistral models)

### B. Dependency Specification (additions to `pyproject.toml`):
```toml
dependencies = [
    "textual>=0.50.0",
    "docling>=2.0.0",
    "any-guardrail>=0.1.0",
    "any-agent[langchain]>=0.1.0",
    "langchain-openai>=0.1.0",
    "langchain-anthropic>=0.1.0",
    "langchain-mistralai>=0.1.0",
    "pydantic>=2.6.0",
    "jinja2>=3.1.0",
]
```

---

## 3. Dynamic Model & Key Configuration Schema

To ensure seamless integration, we define a robust mapping of supported models to their respective frameworks and target API key environment variables.

| Model ID | Provider Name | Target Env Variable | Class Path / SDK |
|---|---|---|---|
| `openai:gpt-4o` | OpenAI | `OPENAI_API_KEY` | `langchain_openai.ChatOpenAI` |
| `openai:gpt-4o-mini` | OpenAI | `OPENAI_API_KEY` | `langchain_openai.ChatOpenAI` |
| `anthropic:claude-3-5-sonnet` | Anthropic | `ANTHROPIC_API_KEY` | `langchain_anthropic.ChatAnthropic` |
| `mistral:mistral-small-latest` | Mistral AI | `MISTRAL_API_KEY` | `langchain_mistralai.ChatMistralAI` |

### Provider Mapping helper:
```python
# sqwakvox/models.py or sqwakvox/agent.py
class ModelProvider:
    MAP = {
        "openai:gpt-4o": {"env_var": "OPENAI_API_KEY", "friendly_name": "OpenAI GPT-4o"},
        "openai:gpt-4o-mini": {"env_var": "OPENAI_API_KEY", "friendly_name": "OpenAI GPT-4o-Mini"},
        "anthropic:claude-3-5-sonnet": {"env_var": "ANTHROPIC_API_KEY", "friendly_name": "Anthropic Claude 3.5 Sonnet"},
        "mistral:mistral-small-latest": {"env_var": "MISTRAL_API_KEY", "friendly_name": "Mistral Small"},
    }

    @classmethod
    def get_env_var(cls, model_id: str) -> str:
        return cls.MAP.get(model_id, {}).get("env_var", "OPENAI_API_KEY")
```

---

## 4. TUI Sidebar Interface Uplift

We will extend the `sidebar` layout in `src/sqwakvox/app.py` to add interactive inputs for model selection and API key submission.

### Wireframe Layout:
```
┌───────────────────────────┐
│ Load Document             │
│ [ File Path / URL...   ]  │
│ ┌───────────────────────┐ │
│ │     Load & Parse      │ │
│ └───────────────────────┘ │
├───────────────────────────┤
│ Model Configuration       │
│ Model:                    │
│ [openai:gpt-4o-mini   [▼]]│
│ API Key:                  │
│ [••••••••••••••••••••• ]  │
└───────────────────────────┘
```

### Textual Code Specification:
Using Textual's standard widgets like `Select` (dropdown) and `Input` (with password masking):

```python
from textual.widgets import Select, Input, Label, Button
from sqwakvox.models import ModelProvider

# Inside SqwakvoxApp.compose():
with Vertical(id="sidebar"):
    yield Label("[bold]Load Document[/bold]")
    yield Input(placeholder="File path or URL...", id="doc-source")
    yield Button("Load & Parse", variant="primary", id="btn-parse")
    
    yield Label("[bold]Model Configuration[/bold]", id="model-config-label")
    
    # 1. Model selection list
    yield Label("Select Model:")
    yield Select(
        options=[(info["friendly_name"], model_id) for model_id, info in ModelProvider.MAP.items()],
        value="openai:gpt-4o-mini",
        id="model-selector",
    )
    
    # 2. Key entry with secure password hiding
    yield Label("Enter Provider API Key:")
    yield Input(
        placeholder="sk-...",
        password=True,
        id="api-key-input"
    )
    
    yield Label("[bold]Ingest History[/bold]", id="history-label")
    yield ListView(id="ingest-history")
```

---

## 5. Implementation Code Blueprint

To avoid blocking the UI, the orchestration of `any-agent` must be executed within a background worker thread. Below is the blueprint of how to setup and invoke the agent:

### Orchestrator Class (`src/sqwakvox/agent.py`):
```python
import os
from contextlib import contextmanager
from any_agent import AnyAgent, AgentConfig, AgentFramework

class AnyAgentOrchestrator:
    """Wraps any-agent initializing and execution logic."""

    @staticmethod
    @contextmanager
    def inject_credentials(env_var: str, api_key: str):
        """Temporarily inject api key into environment securely."""
        original_val = os.environ.get(env_var)
        os.environ[env_var] = api_key
        try:
            yield
        finally:
            if original_val is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = original_val

    @classmethod
    def execute_query(
        cls, 
        model_id: str, 
        api_key: str, 
        context: str, 
        prompt: str,
        env_var: str
    ) -> str:
        """Configures and runs any-agent with LangChain framework adapter."""
        
        # 1. Establish prompt template with grounded document context
        instructions = (
            f"You are a helpful Financial Document Assistant.\n"
            f"Always ground your answers in the document context provided below.\n\n"
            f"--- DOCUMENT CONTEXT ---\n{context}\n------------------------"
        )
        
        config = AgentConfig(
            model_id=model_id,
            instructions=instructions,
        )
        
        # 2. Safely run within environment credential block
        with cls.inject_credentials(env_var, api_key):
            agent = AnyAgent.create(
                framework=AgentFramework("langchain"),
                config=config
            )
            # Run the agent execution
            return agent.run(prompt)
```

### Integration in TUI App (`src/sqwakvox/app.py`):
Replace `_handle_chat` with an async-worker invocation to guarantee zero UI stuttering:

```python
    def _handle_chat(self) -> None:
        chat_input = self.query_one("#chat-input", Input)
        user_query = chat_input.value.strip()
        if not user_query:
            return

        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"\n[bold blue]You:[/bold blue] {user_query}")
        chat_input.value = ""

        # Retrieve widget settings
        selected_model = self.query_one("#model-selector", Select).value
        api_key = self.query_one("#api-key-input", Input).value.strip()

        # Validate api key presence
        if not api_key:
            chat_log.write("[bold yellow]System: Warning! API Key is missing. Please provide a valid key in the sidebar.[/bold yellow]")
            return

        chat_log.write("[italic dim]Agent is thinking (via LangChain)...[/italic dim]")

        # Run any-agent in background worker thread
        self.run_worker(
            self._execute_agent_background(selected_model, api_key, user_query),
            thread=True,
            name="any_agent_worker"
        )

    async def _execute_agent_background(self, model_id: str, api_key: str, user_query: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        
        # 1. Input Guardrail Verification
        redacted_query = PIIRedactor.redact_text(user_query)
        if redacted_query != user_query:
            chat_log.write("[italic dim]PII detected and redacted from query.[/italic dim]")

        # 2. Get environment mapping
        env_var = ModelProvider.get_env_var(model_id)

        try:
            from sqwakvox.agent import AnyAgentOrchestrator
            
            # Execute LangChain Agent synchronously inside the worker thread
            agent_response = AnyAgentOrchestrator.execute_query(
                model_id=model_id,
                api_key=api_key,
                context=self.doc_context,
                prompt=redacted_query,
                env_var=env_var
            )

            # 3. Output Guardrail Redaction
            agent_redacted = PIIRedactor.redact_text(agent_response)
            
            # Push response back to TUI main thread
            self.call_from_thread(self._on_agent_success, agent_redacted, user_query)
            
        except Exception as e:
            self.call_from_thread(self._on_agent_failure, str(e))

    def _on_agent_success(self, response: str, query: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"[bold green]Agent:[/bold green] {response}")
        
        # Audit Logs
        AuditLogger.log(
            document_id=self.active_document_name or "unknown",
            operation="agent_response",
            action="ALLOWED",
            input_text=query
        )

    def _on_agent_failure(self, error_message: str) -> None:
        chat_log = self.query_one("#chat-log", RichLog)
        chat_log.write(f"[bold red]✗ Agent execution failed:[/bold red] {error_message}")
```

---

## 6. Verification and Security Best Practices

> [!CAUTION]
> **API Key Safety Guidelines**:
> - **In-Memory Retention Only**: API Keys entered into the TUI should only live in RAM (via the widget state). Under no circumstances should these keys be serialized, saved to disk, or written to standard logs (like the Audit JSONL log).
> - **Safe Injection**: The `inject_credentials` context manager guarantees that the API key is only placed into the OS environment during the active request window and is completely purged immediately afterward.
