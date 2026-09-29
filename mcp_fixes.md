# MCP Notes

Historical record of the MCP integration problems, and what the current
architecture does instead. Kept because the constraints below still apply and
will bite again if reintroduced.

## Current architecture

```
execute_query (sync; Celery requires a sync entry point)
  └─ asyncio.run(_run_async(...))        ← ONE event loop for the whole run
       └─ connected_tools(...)           ← async with; MCP sessions open here
            └─ _invoke_agent(...)        ← create_react_agent + ainvoke
```

The agent is a `langgraph.prebuilt.create_react_agent` over a
`ChatAnyLLM` model (`src/sqwakvox/llm.py`), which is a `BaseChatModel` wrapper
around `any_llm.acompletion`. Tool connections come from
`langchain-mcp-adapters`. Config models live in `src/sqwakvox/mcp.py`.

## Why one event loop is mandatory

MCP stdio subprocess pipes are bound to the event loop that opened them.
Creating the agent in one loop and running it in another leaves the tool
connections dead, and the model then loops retrying broken tools until it burns
the recursion budget.

This was previously a live bug: `any-agent` bridged async to sync with
`run_async_in_sync`, which spun up a temporary loop for agent creation and
closed it, then spun up a second loop for the run. The fix at the time was
`_execute_in_single_loop`, a hand-rolled event loop that spanned both phases.

That scaffolding is gone. `execute_query` now wraps the whole run in a single
`asyncio.run`, so there is only ever one loop, and
`connected_tools` holds the MCP sessions open in an `async with` for exactly
as long as the agent needs them.

**Constraint: do not split agent creation and execution across loops.** If you
ever move the MCP session lifetime outside the agent run, the stdio tools
break.

## Cleanup

Sessions are released by the `async with` scope in `connected_tools`, and the
`AsyncExitStack` inside it closes every session it opened — including partial
failures, where one server connects and the next does not.

There is no separate cleanup step, no timeout on teardown, and no swallowing of
`GeneratorExit` / `StopAsyncIteration` / cancel-scope errors. Those existed only
to paper over cross-loop teardown, which no longer happens.

The old code also ran a `psutil`-based reaper (`_kill_orphaned_mcp_children`)
after every run to SIGTERM leaked MCP children. Verified unnecessary: 5
consecutive agent runs with a live stdio server left no orphaned processes.
The reaper was deleted.

## Tool availability logging

`_log_available_tools` logs the resolved tool names on every run. If an
expected tool (e.g. `calc-stats`) is missing, the log names what did connect.

## MCP startup retry

`_run_async` retries the whole connection attempt up to
`MAX_MCP_STARTUP_ATTEMPTS` (2) times. A persistently failing server degrades to
a tools-less direct model call rather than failing the query outright — the
model can still answer from the document.

## Timeouts and recursion

- `MAX_AGENT_RECURSION_LIMIT = 20` — LangGraph `recursion_limit`, i.e. model +
  tool round trips. With ~13 tools from calc-stats plus others, reasoning models
  need 15–20 steps to converge.
- `AGENT_RUN_TIMEOUT_SECONDS = 180` — hard wall-clock ceiling via
  `asyncio.wait_for`. Runs exceeding 80% of it are logged.
- `client_session_timeout_seconds` per server (default 5s, 300s in
  `mcp_servers.json`).

When no MCP servers are configured the ReAct loop is skipped entirely and the
model is called directly. A tool-calling loop with an empty tool list makes
reasoning models spin against tools that do not exist.

## Broker credential security

API keys do not cross the Celery broker. The gateway dispatches tasks with
`api_key=None`; workers resolve the key from their own environment via
`ModelProvider.resolve_key()`.

## HTTP transport guard

Sibling MCP servers (`calc`, `skills`, `retrieval`) refuse network-binding
transports (`--transport sse` / `--transport http`) unless explicitly opted in
with `SQWAKVOX_MCP_ALLOW_HTTP=1` and a non-empty bearer token in
`SQWAKVOX_MCP_HTTP_TOKEN`. The gateway itself is locked to `stdio` only.

## Read-only mode & tool annotations

Setting `SQWAKVOX_MCP_READ_ONLY=1` disables mutating tools (`create_skill`,
`update_skill`, `delete_skill`) in the skills server. Tools declare
`readOnlyHint`, `destructiveHint`, and `idempotentHint` annotations.

## Input clamping & rate limiting

`query` length clamped to 8,000 chars, `k` clamped to 1–50, `timeout` clamped
to 5–600s. Sliding-window rate limit on `sqwakvox_query` via
`SQWAKVOX_MCP_QUERY_RPM` (default 60).

## Broker config wire format

MCP configs cross the Celery broker as `model_dump()` dicts and are rehydrated
by `McpConnection`. Each config carries a `transport` discriminator
(`stdio` / `sse` / `streamable_http`).

That discriminator is load-bearing: `MCPSse` and `MCPStreamableHttp` have
identical field sets, and an undiscriminated pydantic union resolves every
remote config to whichever member is declared first — a streamable-HTTP server
gets dialled as SSE. Payloads written by pre-migration workers have no
`transport` key, so `_infer_transport` fills it in from shape (`command` means
stdio, bare `url` means SSE, matching the old ambiguous behaviour).

## FastMCP 4 deferral

Upgrading to FastMCP 4 (`mcp>=2.0.0`) is still deferred: `fastmcp` 3.x imports
`McpError` from `mcp.shared.exceptions`, removed in mcp 2.0.0. The
`mcp>=1.29.0,<2.0` pin in `pyproject.toml` is a sqwakvox constraint, not a
transitive one — the `any-agent` cap that originally forced it is gone, and
`langchain-mcp-adapters` declares only `mcp>=1.9.2`. So the blocker is now
sqwakvox's own fastmcp pin and can be revisited on its own.
