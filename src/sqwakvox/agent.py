from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from typing import Any, cast

from any_agent import AgentConfig, AgentFramework, AnyAgent
from any_agent import AnyAgent as AnyAgentLib
from any_agent.config import MCPParams
from jinja2 import Template

from sqwakvox.models import ModelProvider
from sqwakvox.telemetry import trace_span

logger = logging.getLogger(__name__)

# Conversation memory lives in a dedicated Redis logical DB, separate from the
# Celery broker (db 0) and result backend (db 1).  Override if needed.
MEMORY_REDIS_URL = os.environ.get("SQWAKVOX_MEMORY_REDIS_URL", "redis://localhost:6379/2")

CLEANUP_TIMEOUT_SECONDS = 30.0
MAX_STARTUP_RETRIES = 1
# Cap LangGraph agent iterations to prevent runaway loops when MCP tools fail.
# Each iteration is one model-call + optional-tool-exec round trip.
# With 17 tools registered, reasoning models may need 15-20 steps to converge.
MAX_AGENT_RECURSION_LIMIT = 20
# Hard wall-clock timeout for the entire agent run.
AGENT_RUN_TIMEOUT_SECONDS = 180.0


def _patch_gemini_provider() -> None:
    """Monkey-patch any_llm Gemini utils to convert role='function' to role='user'.

    Google GenAI SDK only supports 'user' and 'model' roles. When tool responses
    are returned with role='function', Gemini API fails with:
    400 INVALID_ARGUMENT: "Role 'function' is not supported. Please use a valid role".
    """
    try:
        import any_llm.providers.gemini.utils as gemini_utils

        if getattr(gemini_utils, "_sqwakvox_patched", False):
            return

        original_convert = gemini_utils._convert_messages

        def patched_convert_messages(
            messages: list[dict[str, Any]], provider_name: str = "gemini"
        ) -> tuple[list[Any], str | None]:
            formatted_messages, system_instruction = original_convert(messages, provider_name)
            for msg in formatted_messages:
                if getattr(msg, "role", None) == "function":
                    msg.role = "user"
            return formatted_messages, system_instruction

        gemini_utils._convert_messages = patched_convert_messages
        gemini_utils._sqwakvox_patched = True  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("Failed to apply Gemini provider role patch: %s", exc)


_patch_gemini_provider()
STANDARD_SYSTEM_INSTRUCTIONS_TEMPLATE = Template(
    "You are a helpful Financial Document Assistant.\n"
    "Always ground your answers in the document context provided below.\n\n"
    "--- DOCUMENT CONTEXT ---\n"
    "{{ context }}\n"
    "------------------------"
)

UNIFIED_USER_PROMPT_TEMPLATE = Template(
    "You are a helpful Financial Document Assistant.\n"
    "Always ground your answers in the document context provided below.\n\n"
    "--- DOCUMENT CONTEXT ---\n"
    "{{ context }}\n"
    "------------------------\n\n"
    "{{ prompt }}"
)


_redis_checkpointer: Any | None = None
_checkpointer_lock = threading.Lock()


def _get_redis_checkpointer() -> Any | None:
    """Return a lazily-initialised LangGraph checkpointer backed by Redis.

    Gives the react agent conversational memory across turns within a
    ``thread_id`` (one thread per active document).  Uses the plain-Redis
    :class:`~sqwakvox.backend.redis_checkpointer.RedisCheckpointer`, which
    needs no RediSearch module.  The saver is created once per process.

    Returns ``None`` when Redis is unreachable so agent runs degrade to the
    previous stateless behaviour instead of failing hard.
    """
    global _redis_checkpointer
    if _redis_checkpointer is None:
        with _checkpointer_lock:
            if _redis_checkpointer is None:
                try:
                    from sqwakvox.backend.redis_checkpointer import RedisCheckpointer

                    saver: Any = RedisCheckpointer(redis_url=MEMORY_REDIS_URL)
                except Exception as exc:
                    logger.warning(
                        "Redis memory checkpointer unavailable (%s); agent will run stateless",
                        exc,
                    )
                    return None
                _redis_checkpointer = saver
                logger.info("Redis conversation memory enabled (%s)", MEMORY_REDIS_URL)
    return _redis_checkpointer


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
    def render_prompt(
        cls,
        model_id: str,
        context: str,
        prompt: str,
    ) -> tuple[str | None, str]:
        """Inject and render the correct Jinja2 template depending on which model is selected.

        For models that do not support/accept a separate system role in agent queries
        (e.g., gemini-3.6-flash), system instructions and context are injected directly into
        the user prompt template, and instructions is set to None.
        For models supporting system roles, system instructions are rendered into instructions.
        """
        if not ModelProvider.supports_system_role(model_id):
            instructions = None
            formatted_prompt = UNIFIED_USER_PROMPT_TEMPLATE.render(context=context, prompt=prompt)
        else:
            instructions = STANDARD_SYSTEM_INSTRUCTIONS_TEMPLATE.render(context=context)
            formatted_prompt = prompt

        return instructions, formatted_prompt

    @classmethod
    def execute_query(
        cls,
        model_id: str,
        api_key: str,
        context: str,
        prompt: str,
        env_var: str,
        mcp_servers: list[MCPParams] | None = None,
        thread_id: str | None = None,
    ) -> str:
        instructions, formatted_prompt = cls.render_prompt(
            model_id=model_id,
            context=context,
            prompt=prompt,
        )

        # Attach the Redis checkpointer only when the caller supplies a thread
        # id, so existing stateless call sites (and the no-tools direct path)
        # keep their current behaviour.
        checkpointer = _get_redis_checkpointer() if thread_id else None
        config = AgentConfig(
            model_id=model_id,
            instructions=instructions,
            tools=list(mcp_servers) if mcp_servers else [],
            agent_args={"checkpointer": checkpointer} if checkpointer else None,
        )
        logger.info("Starting any-agent execution — model: %s", model_id)
        with (
            trace_span("sqwakvox.agent.orchestration", {"model_id": model_id}),
            cls.inject_credentials(env_var, api_key),
        ):
            # We use a single persistent event loop for agent creation AND
            # execution so that MCP stdio subprocess connections survive.
            # run_async_in_sync creates/destroys temp loops which would
            # kill anyio streams between create() and run().
            return cls._execute_in_single_loop(
                config=config,
                prompt=formatted_prompt,
                raw_mcp_servers=mcp_servers or [],
                thread_id=thread_id,
            )

    @classmethod
    def _execute_in_single_loop(
        cls,
        config: AgentConfig,
        prompt: str,
        raw_mcp_servers: list[MCPParams],
        thread_id: str | None = None,
    ) -> str:
        """Create agent and run it inside a single asyncio event loop.

        This is the key fix for MCP stdio transport: the subprocess pipes
        opened during agent creation must stay alive until execution completes.
        A single long-lived event loop ensures the anyio streams aren't torn
        down prematurely.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result: str | None = None
        error: BaseException | None = None
        try:
            result = loop.run_until_complete(
                cls._create_and_run_async(
                    config=config,
                    prompt=prompt,
                    raw_mcp_servers=raw_mcp_servers,
                    thread_id=thread_id,
                )
            )
        except BaseException as exc:
            error = exc
        finally:
            # Drain pending tasks before closing to avoid "Event loop is closed"
            # errors from httpx keepalive connections.
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            with suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            with suppress(Exception):
                loop.close()
            with suppress(Exception):
                asyncio.set_event_loop(None)
        if error is not None:
            raise error
        return result  # type: ignore[return-value]

    @classmethod
    async def _create_and_run_async(
        cls,
        config: AgentConfig,
        prompt: str,
        raw_mcp_servers: list[MCPParams],
        thread_id: str | None = None,
    ) -> str:
        """Async body: create agent (or direct model call if no tools), then run."""
        # When no MCP tools are configured, skip the LangGraph react-agent
        # loop entirely.  create_react_agent with an empty tool list still
        # wraps the model in a tool-calling loop — reasoning models can get
        # stuck trying to invoke non-existent tools, burning through the
        # recursion budget.
        # Direct model call: one prompt → one response, no looping.
        if not raw_mcp_servers:
            return await cls._run_direct_model(config, prompt)

        agent, used_tools = await cls._create_agent_async(config, raw_mcp_servers)

        if used_tools and len(used_tools) < len(raw_mcp_servers):
            logger.warning(
                "Falling back to a reduced tool set (%d of %d MCP servers available).",
                len(used_tools),
                len(raw_mcp_servers),
            )

        cls._log_available_tools(agent)

        start_time = time.monotonic()
        with trace_span(
            "sqwakvox.agent.react_agent_run",
            {"model_id": config.model_id, "mcp_servers_count": len(raw_mcp_servers)},
        ) as span:
            try:
                run_config: dict[str, Any] = {
                    "recursion_limit": MAX_AGENT_RECURSION_LIMIT,
                }
                if thread_id:
                    run_config["configurable"] = {"thread_id": thread_id}
                trace = await asyncio.wait_for(
                    agent.run_async(
                        prompt,
                        config=run_config,
                    ),
                    timeout=AGENT_RUN_TIMEOUT_SECONDS,
                )
                elapsed = time.monotonic() - start_time
                response = str(trace.final_output)
                span.set_attribute("response_len", len(response))
                span.set_attribute("elapsed_sec", elapsed)
                span.set_attribute("trace_spans_count", len(trace.spans))
                logger.info(
                    "Agent execution complete — response: %d chars, elapsed: %.1fs, spans: %d",
                    len(response),
                    elapsed,
                    len(trace.spans),
                )
                logger.debug("Agent response body:\n%s", response)
                if "need more steps" in response.lower() or len(response) < 80:
                    logger.warning(
                        "Agent returned a short/truncated response (%d chars). "
                        "This usually means the recursion limit (%d) was hit "
                        "before the model finished. The model may be looping "
                        "on tool calls. Response: %s",
                        len(response),
                        MAX_AGENT_RECURSION_LIMIT,
                        response[:200],
                    )
                return response
            except TimeoutError:
                elapsed = time.monotonic() - start_time
                logger.error(
                    "Agent execution TIMED OUT after %.1fs (limit %ss).",
                    elapsed,
                    AGENT_RUN_TIMEOUT_SECONDS,
                )
                raise
            finally:
                elapsed = time.monotonic() - start_time
                if elapsed > AGENT_RUN_TIMEOUT_SECONDS * 0.8:
                    logger.warning(
                        "Agent run took %.1fs — near the timeout of %ss.",
                        elapsed,
                        AGENT_RUN_TIMEOUT_SECONDS,
                    )
                await cls._cleanup_async(agent)

    @classmethod
    async def _run_direct_model(cls, config: AgentConfig, prompt: str) -> str:
        """Call the model directly — no LangGraph agent loop at all.

        Used when no MCP tools are configured.  This is the simplest possible
        path: one system prompt + one user message = one response.
        """
        from any_llm import acompletion

        logger.info("Running direct model call (no tools) — model: %s", config.model_id)
        start_time = time.monotonic()
        messages: list[dict[str, Any]] = []
        if config.instructions:
            messages.append({"role": "system", "content": config.instructions})
        messages.append({"role": "user", "content": prompt})

        with trace_span("sqwakvox.agent.direct_model_call", {"model_id": config.model_id}) as span:
            try:
                response = await asyncio.wait_for(
                    acompletion(
                        model=config.model_id,
                        messages=cast(Any, messages),
                    ),
                    timeout=AGENT_RUN_TIMEOUT_SECONDS,
                )
                elapsed = time.monotonic() - start_time
                if hasattr(response, "choices"):
                    text = response.choices[0].message.content or ""
                else:
                    chunks = []
                    async for chunk in response:
                        if chunk.choices and chunk.choices[0].delta.content:
                            chunks.append(chunk.choices[0].delta.content)
                    text = "".join(chunks)
                span.set_attribute("response_len", len(text))
                span.set_attribute("elapsed_sec", elapsed)
                logger.info(
                    "Direct model call complete — response length: %d chars, elapsed: %.1fs",
                    len(text),
                    elapsed,
                )
                return text
            except TimeoutError:
                elapsed = time.monotonic() - start_time
                logger.error("Direct model call TIMED OUT after %.1fs", elapsed)
                raise

    @classmethod
    async def _create_agent_async(
        cls,
        config: AgentConfig,
        mcp_servers: list[MCPParams],
    ) -> tuple[AnyAgent, list[MCPParams]]:
        """Create the agent with retry logic for MCP startup failures.

        On repeated MCP failures we fall back to a tools-less run so the
        agent can still respond.
        """
        if not mcp_servers:
            agent = await AnyAgentLib.create_async(
                agent_framework=AgentFramework("langchain"),
                agent_config=config,
            )
            return agent, []

        try:
            agent = await AnyAgentLib.create_async(
                agent_framework=AgentFramework("langchain"),
                agent_config=config,
            )
            return agent, mcp_servers
        except Exception as exc:
            if not cls._is_mcp_error(exc):
                raise
            logger.warning(
                "MCP server startup failed (%s). Trying once more, then "
                "falling back to a tools-less run.",
                exc,
            )
            cls._kill_orphaned_mcp_children()
            try:
                agent = await AnyAgentLib.create_async(
                    agent_framework=AgentFramework("langchain"),
                    agent_config=config,
                )
                return agent, mcp_servers
            except Exception as retry_err:
                if not cls._is_mcp_error(retry_err):
                    raise
                logger.warning(
                    "MCP startup failed again (%s). Proceeding WITHOUT tools.",
                    retry_err,
                )
                cls._kill_orphaned_mcp_children()
                fallback_config = config.model_copy(update={"tools": []})
                agent = await AnyAgentLib.create_async(
                    agent_framework=AgentFramework("langchain"),
                    agent_config=fallback_config,
                )
                return agent, []

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
        # AnyAgent stores tools on the instance as `_tools`, not `tools`.
        names = getattr(agent, "_tools", None)
        if names is None:
            logger.info("No tool information available for agent.")
            return
        try:
            tool_names = [getattr(t, "name", str(t)) for t in names]
        except TypeError:
            tool_names = [str(t) for t in names]
        if tool_names:
            logger.info(
                "Agent has %d tool(s) registered: %s",
                len(tool_names),
                ", ".join(tool_names),
            )
        else:
            logger.warning(
                "No tools are registered for this run. If an MCP server (e.g. calc-stats) "
                "was expected, it likely failed to start or connect."
            )

    @classmethod
    async def _cleanup_async(cls, agent: AnyAgent) -> None:
        """Clean up the agent, tolerating cancel-scope / generator warnings."""
        cleanup_start = time.monotonic()
        try:
            await asyncio.wait_for(agent.cleanup_async(), timeout=CLEANUP_TIMEOUT_SECONDS)
        except (GeneratorExit, StopAsyncIteration):
            logger.debug("Ignoring GeneratorExit/StopAsyncIteration during cleanup.")
        except TimeoutError:
            logger.warning("Agent cleanup timed out after %ss.", CLEANUP_TIMEOUT_SECONDS)
        except RuntimeError as e:
            msg = str(e).lower()
            if "cancel scope" in msg or "event loop is closed" in msg:
                logger.debug("Cancel-scope / event-loop error during cleanup: %s", e)
            else:
                logger.warning("Failed to clean up agent: %s", e)
        except Exception as e:
            logger.warning("Failed to clean up agent: %s", e)
        finally:
            elapsed = time.monotonic() - cleanup_start
            if elapsed > 5.0:
                logger.warning(
                    "Agent cleanup took %.1fs — MCP subprocesses may be orphaned.",
                    elapsed,
                )
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
