## Recommended Fixes

### Priority 1: Fix MCP Threading (unblock tool usage) — PARTIALLY ADDRESSED

The `any-agent` library bridges async→sync via `run_async_in_sync`, which creates event loop / cancel scope conflicts. The fix is in `agent.py` to run MCP servers in a way that avoids cross-task cancel scopes. Options:

1. **Use `subprocess.Popen` + a dedicated event loop per MCP server** — launch each MCP server in its own process with its own asyncio event loop, managed from the sync thread.

2. **Switch MCP transport from stdio to SSE/HTTP** — have the `calc-stats` server run as a long-lived process (e.g., `mcp.run(transport="sse")`) and connect via HTTP, avoiding the stdio threading problem entirely.

**Implemented (option 2, opt-in):** `mcp_calc_server.py` now accepts `--transport {stdio,sse,http}`, `--host`, and `--port`. The loader in `app.py` (`_load_mcp_servers` / `_build_http_mcp`) now supports HTTP-based (`url` + `transport`) MCP configs via `MCPSse` / `MCPStreamableHttp`. See `mcp_servers.json.example` for a `calc-stats-http` example. Stdio remains the default for backward compatibility. To fully avoid stdio threading issues, run the calc server with `--transport sse` (or `http`) as a long-lived process and point the config at its URL.

### Priority 2: Fix Agent Cleanup — IMPLEMENTED

The `generator didn't stop after athrow()` warning means the any-agent's async generator doesn't clean up properly. This could be a bug in `any-agent` or in how `run_async_in_sync` is used. Consider:
- Catching `GeneratorExit` explicitly in cleanup — **done** in `AnyAgentOrchestrator._cleanup`.
- Using a short timeout when awaiting `cleanup_async()` — **done** (`CLEANUP_TIMEOUT_SECONDS = 30`).
- Killing orphaned MCP child processes after agent completion — **done** in `_kill_orphaned_mcp_children` (requires `psutil`; best-effort, skipped if unavailable).

### Priority 3: Add Tool-Availability Logging — IMPLEMENTED

Before invoking the agent, log which tools are actually registered/available. If the MCP server crashed, log a warning so it's clear to the user/developer that tools aren't available for this run. **Done** in `AnyAgentOrchestrator._log_available_tools`, called after agent creation.

### Priority 4: Add Retry for MCP Server Startup — IMPLEMENTED

If the MCP server connection fails, try restarting it once before proceeding without tools. **Done** in `AnyAgentOrchestrator._create_agent`: on an MCP-detected startup failure it retries once, then falls back to a tools-less run (still warning the operator) so the agent still responds.
