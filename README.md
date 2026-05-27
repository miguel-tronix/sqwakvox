# Sqwakvox — Local AI Financial Document Assistant

███████╗ ██████╗ ██╗    ██╗ █████╗ ██╗  ██╗██╗   ██╗ ██████╗ ██╗  ██╗
██╔════╝██╔═══██╗██║    ██║██╔══██╗██║ ██╔╝██║   ██║██╔═══██╗╚██╗██╔╝
███████╗██║   ██║██║ █╗ ██║███████║█████╔╝ ██║   ██║██║   ██║ ╚███╔╝ 
╚════██║██║▄▄ ██║██║███╗██║██╔══██║██╔═██╗ ╚██╗ ██╔╝██║   ██║ ██╔██╗ 
███████║╚██████╔╝╚███╔███╔╝██║  ██║██║  ██╗ ╚████╔╝ ╚██████╔╝██╔╝ ██╗
╚══════╝ ╚══▀▀═╝  ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚═╝  ╚═╝

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Sqwakvox is a **Textual TUI** app that ingests financial documents (PDFs) via **IBM Docling**, renders tables with sparkline trends, and lets you chat with the document through **Mozilla any-agent** (LangChain) backed by multiple LLM providers — all with prompt guardrails, PII redaction, and numerical cross-validation.

![Screenshot](screenshots/SqwakvoxApp_2026-05-26T11_26_35_185726.svg)

## Features

- **3-Pane TUI** — Sidebar (file load, model config, ingest history), document render pane (markdown + tables + sparklines), chat log
- **Docling Ingestion** — Parse local PDFs or remote URLs; table extraction with dataframe export
- **Multi-Model Chat** — OpenAI GPT-4o / GPT-4o-Mini, Anthropic Claude 3.5 Sonnet, Mistral Small, Gemini 2.5 Pro / Flash
- **Input Guardrails** — Mozilla `any-guardrail` (INJECGUARD) blocks prompt injection
- **PII Redaction** — Automatic redaction of SSNs, credit cards, bank accounts, emails
- **Financial Cross-Validation** — Extracts labelled numeric values from tables and verifies LLM assertions against them
- **Unicode Table Rendering** — Double-lined borders, auto-alignment, numeric column sparklines
- **Audit Logging** — JSONL audit trail at `~/.gemini/antigravity/sqwakvox/audit_log.jsonl`

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   Sqwakvox TUI (Textual)                  │
│  ┌──────────┐  ┌──────────────────┐  ┌────────────────┐ │
│  │ Sidebar  │  │ Document Render  │  │   Chat Log     │ │
│  │ - Source │  │ - Markdown       │  │                │ │
│  │ - Model  │  │ - Tables (Unicode)│  │                │ │
│  │ - API Key│  │ - Sparklines     │  │                │ │
│  │ - History│  │                  │  │                │ │
│  └────┬─────┘  └────────┬─────────┘  └───────┬────────┘ │
│       │                 │                     │          │
└───────┼─────────────────┼─────────────────────┼──────────┘
        │                 │                     │
        ▼                 ▼                     ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐
│  Docling     │  │ Financial    │  │  any-agent (LangChain)│
│  Converter   │  │ Rule Engine  │  │  + any-guardrail      │
│  (PDF→MD)    │  │ (Cross-Val)  │  │  + PII Redactor       │
└──────────────┘  └──────────────┘  └──────────────────────┘
```

## Installation

```bash
pip install sqwakvox
# or from source:
pip install -e .
```

Requires Python ≥ 3.12.

## Usage

```bash
sqwakvox
```

1. **Load a document** — Enter a file path or URL, or click the 📁 button to browse
2. **Select a model** — Pick from the dropdown (Gemini 2.5 Flash, GPT-4o, etc.)
3. **Enter your API key** — For the chosen provider
4. **Click "Load & Parse"** — Docling extracts layout, tables, and text
5. **Ask questions** — The agent answers grounded in the document context

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

API keys are entered at runtime in the TUI sidebar (never stored). Supported environment variables (can be used instead):
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `MISTRAL_API_KEY`
- `GEMINI_API_KEY`

## Guardrails & Safety

1. **Prompt Injection** — `any-guardrail` (INJECGUARD) inspects every user query before it reaches the LLM
2. **PII Redaction** — SSNs, credit cards, IBANs, and emails are automatically redacted from both queries and agent responses
3. **Numerical Cross-Validation** — The `FinancialRuleEngine` parses labelled figures from document tables and cross-checks LLM responses for arithmetic consistency
4. **Audit Logging** — All operations (ingest, query, response) are timestamped and written to an append-only JSONL audit log

## Build With UV

To build the application:

```bash
uv sync
uv build
```

## Run With UV

```bash
uv run sqwakvox
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Lint
ruff check src/

# Type-check
mypy src/

# Test
pytest
```

## License

MIT — see [LICENSE](LICENSE).

## Author

Miguel de Sousa — [miguel.tronix@gmail.com](mailto:miguel.tronix@gmail.com)
