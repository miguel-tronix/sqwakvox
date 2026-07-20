from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager

from any_agent import AgentConfig, AgentFramework, AnyAgent
from any_agent import AnyAgent as AnyAgentLib
from any_agent.config import MCPParams
from any_llm.utils.aio import run_async_in_sync

logger = logging.getLogger(__name__)

CLEANUP_TIMEOUT_SECONDS = 30.0
MAX_STARTUP_RETRIES = 1


class AnyAgentOrchestrator:
    _lock = threading.Lock()

    @staticmethod
    @contextmanager
    def inject_credentials(env_var: str, api_key: str) -> Generator[None, None, None]:
        """Temporarily inject api key into environment securely using a process-wide lock.

        Ensures thread-safe environment variable injection during concurrent query executions.
        """
        with AnyAgentOrchestrator._lock:
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
        env_var: str,
        mcp_servers: list[MCPParams] | None = None,
    ) -> str:
        instructions = (
            f"You are a helpful Financial Document Assistant.\n"
            f"Always ground your answers in the document context provided below.\n\n"
            f"--- DOCUMENT CONTEXT ---\n{context}\n------------------------"
        )

        config = AgentConfig(
            model_id=model_id,
            instructions=instructions,
            tools=list(mcp_servers) if mcp_servers else [],
        )

        logger.info("Starting any-agent execution — model: %s", model_id)
        with cls.inject_credentials(env_var, api_key):
            # Priority 4: MCP server startup can fail intermittently (e.g. stdio
            # cancel-scope conflicts). Retry once before falling back to a
            # tool-less run so the agent still responds.
            agent, used_tools = cls._create_agent(config, mcp_servers or [])
            if used_tools and len(used_tools) < len(mcp_servers or []):
                logger.warning(
                    "Falling back to a reduced tool set (%d of %d MCP servers available).",
                    len(used_tools),
                    len(mcp_servers or []),
                )

            # Priority 3: log the tools that are actually available so it's
            # obvious when an MCP server failed to provide tools.
            cls._log_available_tools(agent)

            try:
                trace = agent.run(prompt)
                response = str(trace.final_output)
                logger.info("Agent execution complete — response length: %d chars", len(response))
                return response
            finally:
                cls._cleanup(agent)

    @classmethod
    def _create_agent(
        cls, config: AgentConfig, mcp_servers: list[MCPParams]
    ) -> tuple[AnyAgent, list[MCPParams]]:
        """Create the agent, retrying MCP-backed tool loading once.

        On a retried failure to load MCP servers we fall back to a run with no
        tools (warning the operator) instead of hard-failing the whole query.
        """
        if not mcp_servers:
            return (
                AnyAgentLib.create(
                    agent_framework=AgentFramework("langchain"),
                    agent_config=config,
                ),
                [],
            )

        try:
            return (
                AnyAgentLib.create(
                    agent_framework=AgentFramework("langchain"),
                    agent_config=config,
                ),
                mcp_servers,
            )
        except Exception as first_err:
            if not cls._is_mcp_error(first_err):
                raise
            logger.warning(
                "MCP server startup failed (%s). Retrying once before falling back "
                "to a tools-less run.",
                first_err,
            )
            try:
                agent = AnyAgentLib.create(
                    agent_framework=AgentFramework("langchain"),
                    agent_config=config,
                )
                return agent, mcp_servers
            except Exception as retry_err:
                if not cls._is_mcp_error(retry_err):
                    raise
                logger.warning(
                    "MCP server startup failed again (%s). Proceeding WITHOUT tools for this run.",
                    retry_err,
                )
                fallback_config = config.model_copy(update={"tools": []})
                return (
                    AnyAgentLib.create(
                        agent_framework=AgentFramework("langchain"),
                        agent_config=fallback_config,
                    ),
                    [],
                )

    @staticmethod
    def _is_mcp_error(err: Exception) -> bool:
        """Heuristically detect MCP / stdio client startup failures."""
        text = str(err).lower()
        markers = (
            "mcp",
            "stdio",
            "tool",
            "connect",
            "closed",
            "cancel",
            "session",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _log_available_tools(agent: AnyAgent) -> None:
        names = getattr(agent, "tools", None)
        if names is None:
            logger.info("No tool information available for agent.")
            return
        try:
            tool_names = [getattr(t, "name", str(t)) for t in names]
        except TypeError:
            tool_names = [str(t) for t in names]
        if tool_names:
            logger.info(
                "Agent has %d tool(s) registered: %s", len(tool_names), ", ".join(tool_names)
            )
        else:
            logger.warning(
                "No tools are registered for this run. If an MCP server (e.g. calc-stats) "
                "was expected, it likely failed to start or connect."
            )

    @staticmethod
    def _cleanup(agent: AnyAgent) -> None:
        """Clean up the agent, tolerating cancel-scope / generator warnings.

        Priority 2:
        - Await cleanup with a short timeout to avoid hanging.
        - Swallow the `GeneratorExit` / "generator didn't stop after athrow()"
          warning that any-agent's async generator can raise.
        - Kill any orphaned MCP child processes (stdio servers) that the
          library failed to terminate.
        """
        try:
            run_async_in_sync(
                asyncio.wait_for(agent.cleanup_async(), timeout=CLEANUP_TIMEOUT_SECONDS)
            )
        except GeneratorExit:
            logger.debug("Ignoring GeneratorExit during any-agent cleanup.")
        except TimeoutError:
            logger.warning("any-agent cleanup timed out after %ss.", CLEANUP_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning("Failed to clean up any-agent: %s", e)
        finally:
            AnyAgentOrchestrator._kill_orphaned_mcp_children()

    @staticmethod
    def _kill_orphaned_mcp_children() -> None:
        """Best-effort termination of lingering MCP stdio child processes.

        any-agent's stdio client can leak child processes when a cancel scope
        is torn down across threads. We reap any python processes launched for
        our known MCP servers that are still alive.
        """
        try:
            import psutil  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("psutil not installed; skipping orphaned MCP process reap.")
            return

        current_pid = os.getpid()
        targets = {"mcp_calc_server", "mcp-server-fetch", "mcp-server-sqlite"}
        for proc in psutil.process_iter(["pid", "ppid", "cmdline", "name"]):
            try:
                if proc.pid == current_pid or proc.ppid() != current_pid:
                    continue
                cmdline = proc.info.get("cmdline") or []
                joined = " ".join(cmdline)
                if any(target in joined for target in targets):
                    logger.warning("Killing orphaned MCP child process %s: %s", proc.pid, joined)
                    proc.send_signal(signal.SIGTERM)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
