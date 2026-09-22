---
name: sqwakvox-mcp
description: Work with the Sqwakvox MCP server — explore tools, query documents, search SWE chunks, and use the domain-expert agent. Use when the user mentions sqwakvox, wants to interact with the Sqwakvox MCP, asks what tools are available, or is experimenting with the quickstart document.
---

# Sqwakvox MCP — Exploration and Usage

The Sqwakvox MCP server provides tools for working with ingested software engineering documents, domain-expert queries, and structured data extraction. It connects via the `mcp__sqwakvox__` namespace.

## Prerequisites

- The Sqwakvox MCP server must be running and reachable via the MCP tool registry
- The active document determines what `sqwakvox_query` and `sqwakvox_get_tables` operate on

## Tool Inventory

All working tools (verify with `sqwakvox_status`):

| Tool | What it does | Params |
|---|---|---|
| `sqwakvox_status` | Connection health: Redis status, active doc, tables, open docs | none |
| `sqwakvox_search_swe_chunks` | Full-text search across ingested SWE books | `query` (required), `doc_id` (optional), `k` (optional, default 5) |
| `sqwakvox_query` | Query the active doc with a domain expert (SWE reference or financial analysis) | `query` (required), `doc_name` (optional), `timeout` (optional, default 120) |
| `sqwakvox_list_documents` | List all loaded documents with domain and table counts | none |
| `sqwakvox_list_skills` | List reusable engineering skills stored in Sqwakvox | none |
| `sqwakvox_get_tables` | Retrieve extracted tables and financial metrics from a doc | `doc_name` (optional) |
| `list_prompts` | List available prompt templates from the MCP server | none |
| `get_prompt` | Fetch a specific prompt by name with optional arguments | `name` (required), `arguments` (optional) |

**Not currently available:** `list_resources`, `read_resource` — not found in the current build.

## Workflow: Exploring a New MCP Server

1. **Check status** — call `sqwakvox_status` to confirm the connection and see what's loaded
2. **List documents** — call `sqwakvox_list_documents` to see what content is available
3. **Search or query** — use `sqwakvox_search_swe_chunks` for keyword search or `sqwakvox_query` for conversational Q&A
4. **Check tables/prompts/skills** — if you need structured data, prompt templates, or reusable skills

## Known Pitfalls

- **`sqwakvox_query` requires `GEMINI_API_KEY`.** The agent backend uses `gemini:gemini-3.6-flash`. If `GEMINI_API_KEY` is not set in the environment, the query agent fails immediately with an API key error. The other tools (search, status, list) work fine without it.
- **`list_resources` and `read_resource` are not available** in the current build — do not attempt to call them.
- **Only one document may be loaded at a time** (the `quickstart` document in the default setup). To query a different doc, pass `doc_name` explicitly to `sqwakvox_query` or `sqwakvox_get_tables`.
- **Tables count is often 0** — `sqwakvox_get_tables` returns empty unless the document has been processed for table extraction.

## Creating Skills in the Sqwakvox Project

Project skills live at `/home2/app2/sqwakvox/.agents/skills/<skill-name>/SKILL.md`. Create with `write_file` directly (not `skill_manage(action='create')`, which targets `~/.hermes/skills/`). Follow the `hermes-agent-skill-authoring` conventions for frontmatter and body structure.

## Verification Checklist

- [ ] `sqwakvox_status` returns `Connected to Redis bus`
- [ ] `sqwakvox_list_documents` shows at least one loaded document
- [ ] `sqwakvox_search_swe_chunks` returns results for a known keyword
- [ ] `sqwakvox_query` works (requires `GEMINI_API_KEY` — if it fails, check the env var)
