## Recommended Fixes

### Priority 1: Fix MCP Threading (unblock tool usage) — FIXED

The `any-agent` library bridges async→sync via `run_async_in_sync`, which creates a temporary event loop for agent creation, then closes it. MCP stdio connections (subprocess pipes) are tied to that loop and die when it closes. When the agent later runs in a new loop, tool calls fail, and the model loops retrying the broken tools.

**Fix implemented in `agent.py`**: `execute_query` now creates a **single long-lived event loop** that spans both agent creation AND execution. This is done via `_execute_in_single_loop` → `_create_and_run_async`. The graph:

```
execute_query (sync)
  └─ _execute_in_single_loop    ← one event loop for everything
       └─ _create_and_run_async  ← async: create agent, run, cleanup
            ├─ _create_agent_async    ← MCP connections established here
            ├─ agent.run_async(prompt) ← same loop, connections still alive!
            └─ _cleanup_async          ← graceful shutdown
```

The old approach used `AnyAgentLib.create()` (sync) which calls `run_async_in_sync(create_async())` — creating one loop for creation, then `agent.run()` (sync) calls `run_async_in_sync(run_async())` — creating ANOTHER loop for execution. The MCP connections died between the two loops.

### Priority 2: Fix Agent Cleanup — IMPROVED

- Using native async `_cleanup_async` method (no more `run_async_in_sync`)
- `CLEANUP_TIMEOUT_SECONDS = 30`
- Swallows `GeneratorExit`, `StopAsyncIteration`, and cancel-scope `RuntimeError`s
- Kills orphaned MCP child processes after cleanup via `_kill_orphaned_mcp_children`
- Logs a warning if cleanup takes > 5 seconds

### Priority 3: Tool-Availability Logging — FIXED

`_log_available_tools` now correctly checks `agent._tools` (was incorrectly checking `agent.tools` which doesn't exist). Logs the names of all registered tools so operators can see what's available.

### Priority 4: MCP Startup Retry — KEPT

`_create_agent_async` retries once on MCP startup failures, then falls back to a tools-less run so the agent can still respond.

### Priority 5: Recursion Limit & Timeout — NEW

- `MAX_AGENT_RECURSION_LIMIT = 10` — passed to LangGraph via `agent_args={"recursion_limit": 10}`. Limits model+tool iterations to 10 round-trips.
- `AGENT_RUN_TIMEOUT_SECONDS = 180` — hard wall-clock timeout on the entire agent execution via `asyncio.wait_for`.
- Logs a warning if the agent takes > 80% of the timeout.

These prevent the model from looping indefinitely when tools fail.
