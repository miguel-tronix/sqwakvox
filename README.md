# Sqwakvox — Local AI Multi-Document Assistant

```
███████╗ ██████╗ ██╗    ██╗ █████╗ ██╗  ██╗██╗   ██╗ ██████╗ ██╗  ██╗
██╔════╝██╔═══██╗██║    ██║██╔══██╗██║ ██╔╝██║   ██║██╔═══██╗╚██╗██╔╝
███████╗██║   ██║██║ █╗ ██║███████║█████╔╝ ██║   ██║██║   ██║ ╚███╔╝ 
╚════██║██║▄▄ ██║██║███╗██║██╔══██║██╔═██╗ ╚██╗ ██╔╝██║   ██║ ██╔██╗ 
███████║╚██████╔╝╚███╔███╔╝██║  ██║██║  ██╗ ╚████╔╝ ╚██████╔╝██╔╝ ██╗
╚══════╝ ╚══▀▀═╝  ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚═╝  ╚═╝
```

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Sqwakvox is a terminal user interface application for **document analysis with multiple expert assistants**. It uses IBM Docling to read documents (PDFs, EPUBs, URLs), connects to language models through Mozilla any-agent, and runs domain-specific guardrails, rendering, and tools.

- **Financial** — tables, sparkline trends, numerical cross-validation, calc-stats MCP tools.
- **Software Engineering (SWE)** — library docs, PDF/EPUB engineering books (e.g. Martin Fowler); builds TOC + code-block indexes and can author reusable **skill files** during a chat.

![Screenshot](screenshots/SqwakvoxApp_2026-05-26T11_26_35_185726.svg)

## Features

- **3-Pane TUI**: The interface has a sidebar, a document render pane, and a chat log.
- **Multiple expert types**: Pick the "Agent Expert Type" when loading a document; each tab keeps its own domain (prompts, guardrails, rendering, tools).
- **Docling Integration**: The application parses local PDF/EPUB/Markdown files and remote URLs, and can **crawl a docs site** (sitemap-aware, same-host, capped) for multi-page library documentation.
- **Multi-Model Support**: The application connects to OpenAI, Anthropic, Mistral, and Gemini models.
- **Input Guardrails**: Mozilla `any-guardrail` blocks prompt injection attacks before queries reach the model.
- **PII Redaction**: The system redacts Social Security numbers, credit cards, bank accounts, and email addresses.
- **Financial Cross-Validation**: The rule engine extracts numeric table values and verifies calculated results.
- **SWE skills**: The SWE assistant can create/read/update reusable `SKILL.md` files (YAML frontmatter) under `./skills/swe/<name>/` via its skills MCP tools.
- **Chunked retrieval**: large SWE documents (Fowler-scale books) are indexed into SQLite FTS5 chunks; the agent searches them via the `retrieval` MCP tool instead of reading the whole book, and the TUI warns about prompt-injection text found in ingested docs.
- **OpenTelemetry Instrumentation**: The system records traces and metrics for document processing, agent execution, and tool calls.
- **Unicode Table Rendering**: The application displays double borders, automatic column alignment, and numeric sparklines.
- **Audit Logging**: The application writes events to an append-only JSONL audit log.

## Architecture

Sqwakvox uses a model-view-presenter (MVP) layout decoupled by Celery. The
Textual TUI (view) never blocks on heavy work — it submits Celery tasks through
the presenter and polls for progress, while a separate worker process runs
document ingestion, post-processing, and agent execution.

Domains are declared in the registry (`sqwakvox/domains/`) — a `DocumentDomain` profile carries its prompts, ingest plan, post-parse processing, guardrail pipeline, renderer, tool set, and skills storage. Adding a new assistant is registering a new domain; the core never changes.

```
┌─────────────────────────────────────────────────────────────┐
│                    View — Textual TUI (app.py)               │
│  3-pane interface: sidebar / document render / chat log      │
└──────────────────────────┬──────────────────────────────────┘
                           │ submit + poll (AsyncResult)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│            Presenter (presenter.py)                          │
│  async facade: TaskHandle, callbacks, revoke, wait()         │
└──────────────────────────┬──────────────────────────────────┘
                           │ Redis broker + result backend
                           ▼
┌─────────────────────────────────────────────────────────────┐
│     Backend — Celery workers (run_worker.py)                 │
│  ┌───────────────────────────┐   ┌────────────────────────┐ │
│  │ sqwakvox.docling (×1)     │   │ sqwakvox.doc<N> (×tabs)│ │
│  │ Docling conversion only   │   │ data store / agent     │ │
│  │ → AppController           │   │ → AppController        │ │
│  └───────────────────────────┘   └────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### Running the backend

You no longer need to start a worker by hand. The TUI spawns and manages its
own Celery workers automatically:

- **One shared Docling ingest worker** on the `sqwakvox.docling` queue.
  Document conversion is a one-time ingestion step, so all tabs share a
  single worker — Docling's heavyweight OCR/layout models load at most a
  handful of times machine-wide instead of once per open tab.
- **One agent worker per document tab** on its own queue
  (`sqwakvox.doc0`, `sqwakvox.doc1`, ...). Data-store, cross-validation, and
  agent queries run in isolation, so a slow agent call on one document never
  blocks chat or parsing on another — and a Docling crash only affects the
  shared ingest worker, which is respawned on the next parse.

Workers are shut down when the TUI exits, and their logs are written
to `./sqwakvox_workers/`.

```bash
# Start Redis, then just the TUI:
redis-server &
uv run sqwakvox
```

Prefer to run the worker yourself (single shared queue)? Set
`SQWAKVOX_MANAGED_WORKERS=0` when launching the TUI:

```bash
# Terminal 1 — Celery worker
python -m sqwakvox.run_worker
# or with uv: uv run python -m sqwakvox.run_worker

# Terminal 2 — TUI (managed workers disabled)
SQWAKVOX_MANAGED_WORKERS=0 sqwakvox
```

The broker and result-backend default to `redis://localhost:6379`. Override
them with `SQWAKVOX_CELERY_BROKER` and `SQWAKVOX_CELERY_BACKEND`. For offline
testing (no Redis), set `SQWAKVOX_CELERY_EAGER=1` to run tasks in-process.

## Installation

Install the package with pip:

```bash
pip install sqwakvox
# or from source:
pip install -e .
```

This application requires Python 3.12 or higher.

## Usage

Run the application:

```bash
sqwakvox
```

Follow these steps to analyze a document:

1. If you have a local file or URL, enter the path in the input field or click the browse button.
2. Pick the **Agent Expert Type** for the document: *Financial* or *Software Engineering*.
3. Select a model provider from the dropdown menu.
4. Enter your API key for the selected provider.
5. Click **Load & Parse** to extract layout, text, and tables (PDF/EPUB/Markdown/URL).
6. Enter a question in the chat input.

### Document assistants (domains)

Each loaded document is tagged with the expert type you selected, and tabs of
different domains can coexist. The domain controls the agent's system prompt,
the guardrail pipeline, how the document renders, and which MCP tools are
attached (`domains` tag in `mcp_servers.json`; untagged servers are global):

| Domain | Sources | Extras |
|---|---|---|
| Financial | PDF, tables | math cross-validation, sparklines, calc-stats tools |
| SWE | PDF, **EPUB**, docs URLs (**crawlable sites**), Markdown | TOC + code-block index, injection scan, secret redaction, **skills**, **chunked retrieval** |

#### SWE skills

The SWE assistant can create reusable skills during a chat (ask it to "save
that as a skill"): it uses the `skills` MCP server to write standard
`SKILL.md` files (YAML frontmatter: `name`, `description`) under
`./skills/swe/<skill-name>/` in the current directory — the same convention
as `.agents/skills/`. Stored skills appear in the sidebar's **Skills** pane
and are reusable by any tooling that reads that format. Override the root
with `SQWAKVOX_SKILLS_DIR`.

#### External Agent Integration (MCP Gateway)

You can connect external AI agents (like `hermes-agent`, Claude Code, Cursor, or Antigravity) to Sqwakvox. External agents use the MCP gateway to inspect open documents, ask questions, and search SWE book chunks.

Start the gateway:

```bash
uv run python -m sqwakvox.mcp_gateway
```

Example configuration for Hermes (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  sqwakvox:
    command: /home/migtronix/app2/sqwakvox/.venv/bin/python
    args:
      - -m
      - sqwakvox.mcp_gateway
    env:
      SQWAKVOX_CELERY_BROKER: redis://localhost:6379/0
      PYTHONPATH: /home/migtronix/app2/sqwakvox/src
    enabled: true
    timeout: 180
```

##### Gateway & Sibling MCP Security Settings

- **Stdio transport**: The gateway runs over stdio only (`uv run python -m sqwakvox.mcp_gateway`).
- **Worker-side API keys**: API keys do not cross the Celery broker; worker processes resolve their own provider keys from the worker environment.
- `SQWAKVOX_MCP_QUERY_RPM`: In-process rate limit for `sqwakvox_query` (default: 60 RPM).
- `SQWAKVOX_MCP_ALLOW_HTTP=1` & `SQWAKVOX_MCP_HTTP_TOKEN=<token>`: Required if running sibling servers (`calc`, `skills`, `retrieval`) over SSE or HTTP transport.
- `SQWAKVOX_MCP_READ_ONLY=1`: Enforces read-only mode on the skills server (blocks `create_skill`, `update_skill`, `delete_skill`).

### Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `Ctrl+L` | Focus document source input |
| `Ctrl+F` | Focus chat input |
| `Ctrl+R` | Clear chat log |
| `Tab` | Cycle through panes (source → render → chat) |
| `Up/Down` | Scroll focused pane |
| `Ctrl+X` | Run numerical cross-validation on loaded tables |

## Configuration

You enter API keys in the sidebar at runtime. The application does not store your keys.

You can set these environment variables instead of entering keys in the sidebar:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `MISTRAL_API_KEY`
- `GEMINI_API_KEY`

## Guardrails & Safety

1. **Prompt Injection**: `any-guardrail` inspects every user query before the model receives the text.
2. **PII Redaction**: The redactor strips Social Security numbers, credit card numbers, IBANs, and email addresses from user queries and model responses.
3. **Numerical Cross-Validation**: The `FinancialRuleEngine` extracts labelled numbers from document tables and verifies model calculations.
4. **Audit Logging**: The application writes timestamped logs to `~/.gemini/antigravity/sqwakvox/audit_log.jsonl`.

## OpenTelemetry & Performance Monitoring

Sqwakvox records OpenTelemetry traces and performance metrics.

### Traces & Spans

- `sqwakvox.document.convert`: Measures document parsing latency and file size.
- `sqwakvox.agent.execute`: Tracks agent query latency, execution duration, and prompt length.
- `sqwakvox.guardrail.validate_prompt`: Records prompt validation latency and results.
- `sqwakvox.guardrail.redact_pii`: Records PII redaction duration and detected items.
- `sqwakvox.cross_validate`: Measures table column sum validation time.
- `sqwakvox.mcp_tool.<tool_name>`: Records execution time and status for MCP tools.

### Metrics

- `sqwakvox.document.ingest.duration`
- `sqwakvox.document.ingest.count`
- `sqwakvox.agent.execution.duration`
- `sqwakvox.agent.execution.count`
- `sqwakvox.guardrail.duration`
- `sqwakvox.guardrail.violations.count`
- `sqwakvox.mcp.tool.duration`
- `sqwakvox.mcp.tool.count`
- `sqwakvox.active_documents.count`

### Telemetry Configuration

Configure telemetry with environment variables:

```bash
# Enable or disable telemetry (default: true)
export SQWAKVOX_TELEMETRY_ENABLED=true

# Select exporter: otlp, file, console, or none
export SQWAKVOX_TELEMETRY_EXPORTER=file

# Set path for local JSON lines trace log
export SQWAKVOX_TELEMETRY_FILE=sqwakvox_telemetry.jsonl

# Set OTLP collector endpoint
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
```

## Build with UV

Build the application package:

```bash
uv sync
uv build
```

## Run with UV

Run the application:

```bash
uv run sqwakvox
```

## Development

Run development commands:

```bash
# Install development dependencies
pip install -e ".[dev]"

# Run code linter
ruff check src/

# Run type checker
mypy src/

# Run tests
pytest
```

## License

MIT — see [LICENSE](LICENSE).

## Author

Miguel de Sousa — [miguel.tronix@gmail.com](mailto:miguel.tronix@gmail.com)
