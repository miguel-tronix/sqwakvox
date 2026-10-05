from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from sqwakvox.llm import build_chat_model
from sqwakvox.mcp import MCPConnectionError, MCPParams, connected_tools, tool_names
from sqwakvox.models import ModelProvider
from sqwakvox.telemetry import trace_span

logger = logging.getLogger(__name__)


# Keys that carry human-readable payloads when an agent response arrives as a
# Python/JSON object literal instead of plain text (see _unwrap_literal_text).
_RESPONSE_PAYLOAD_KEYS = (
    "chart",
    "ascii_chart",
    "ascii",
    "graph",
    "diagram",
    "output",
    "text",
    "content",
    "response",
    "answer",
    "result",
    "data",
)


def _parse_literal(val: str) -> Any:
    """Parse string val as JSON or Python literal, returning None on failure."""
    val_str = val.strip()
    try:
        return json.loads(val_str)
    except Exception:
        pass
    try:
        return ast.literal_eval(val_str)
    except Exception:
        return None


def _unwrap_literal_text(text: str) -> str:
    """Recover human-readable text when a response is (or embeds) a literal.

    Two real-world failure modes produce a Python-object-looking blob instead
    of the actual answer / ASCII chart:

    * the ``mcp-ascii-charts`` MCP server returns ``{"chart": ..., "title":
      ...}`` or JSON/Python dict payloads that models frequently echo verbatim into
      their final answer;
    * some providers return content as a list of blocks, and a ``str()`` on that
      list yields its Python repr rather than text.

    Tries both JSON and Python literal parsing to extract embedded chart payloads.
    """
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        parsed = _parse_literal(stripped)
        if parsed is not None and parsed != text and not isinstance(parsed, str):
            extracted = _extract_response(parsed)
            if extracted:
                return extracted

    # Object embedded in prose (e.g. "Here is the chart:\n{'chart': '...'}"):
    # find balanced {...} or [...] spans and unwrap them if they carry a text payload.
    for open_char, close_char in (("{", "}"), ("[", "]")):
        pos = 0
        while True:
            start = text.find(open_char, pos)
            if start == -1:
                break
            depth = 0
            found_end = -1
            for idx in range(start, len(text)):
                if text[idx] == open_char:
                    depth += 1
                elif text[idx] == close_char:
                    depth -= 1
                    if depth == 0:
                        found_end = idx
                        break
            if found_end != -1:
                candidate = text[start : found_end + 1]
                parsed = _parse_literal(candidate)
                if parsed is not None and not isinstance(parsed, str):
                    extracted = _extract_response(parsed)
                    if extracted and extracted != candidate:
                        prefix = text[:start].rstrip()
                        suffix = text[found_end + 1 :].strip()
                        return f"{prefix}\n{extracted}\n{suffix}".strip()
                pos = start + 1
            else:
                break
    return text


def _extract_response(final_output: Any) -> str:
    """Best-effort plain-text extraction from an agent result or message.

    A naive ``str()`` on a dict/BaseModel/list leaks a Python object repr into
    the user-facing response — an ascii-chart answer arrives as ``{'chart':
    '...'}`` instead of the chart itself.
    """
    if final_output is None:
        return ""
    if isinstance(final_output, str):
        return _unwrap_literal_text(final_output)
    if hasattr(final_output, "content"):
        # LangChain messages (AIMessage & co) are pydantic models or objects with content.
        content = getattr(final_output, "content", None)
        if content is not None:
            return _extract_response(content)
        return str(final_output)
    if isinstance(final_output, dict):
        for key in _RESPONSE_PAYLOAD_KEYS:
            if key in final_output:
                value = final_output[key]
                if value is not None:
                    res = _extract_response(value)
                    if res:
                        return res
        messages = final_output.get("messages")
        if isinstance(messages, list) and messages:
            return _extract_response(messages[-1])
        return str(final_output)
    if isinstance(final_output, list):
        # Content blocks: [{"type": "text", "text": "..."}]
        parts: list[str] = []
        for item in final_output:
            if isinstance(item, (dict, list)):
                res = _extract_response(item)
                if res:
                    parts.append(res)
            elif isinstance(item, str):
                parts.append(_unwrap_literal_text(item))
        if parts:
            return "\n".join(parts).strip()
        return str(final_output)
    return str(final_output)


# Conversation memory lives in a dedicated Redis logical DB, separate from the
# Celery broker (db 0) and result backend (db 1).  Override if needed.
MEMORY_REDIS_URL = os.environ.get("SQWAKVOX_MEMORY_REDIS_URL", "redis://localhost:6379/2")

# How many times to attempt MCP connection before falling back to a tools-less
# run. A dead MCP server should not stop the model from answering.
MAX_MCP_STARTUP_ATTEMPTS = 2
# Cap LangGraph agent iterations to prevent runaway loops when MCP tools fail.
# Each iteration is one model-call + optional-tool-exec round trip.
# With 17 tools registered, reasoning models may need 15-20 steps to converge.
MAX_AGENT_RECURSION_LIMIT = 20
# Hard wall-clock timeout for the entire agent run.
AGENT_RUN_TIMEOUT_SECONDS = 180.0


def _default_templates() -> tuple[Any, Any]:
    """Financial-domain templates as the fallback for missing domain prompts."""
    from sqwakvox.domains import get_domain
    from sqwakvox.domains.financial import (
        STANDARD_SYSTEM_INSTRUCTIONS_TEMPLATE,
        UNIFIED_USER_PROMPT_TEMPLATE,
    )

    financial = get_domain("financial")
    return (
        financial.system_instructions or STANDARD_SYSTEM_INSTRUCTIONS_TEMPLATE,
        financial.user_prompt_template or UNIFIED_USER_PROMPT_TEMPLATE,
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
        domain: Any = None,
    ) -> tuple[str | None, str]:
        """Inject and render the domain's Jinja2 templates for the model.

        For models that do not support/accept a separate system role in agent
        queries (e.g., gemini-3.6-flash), system instructions and context are
        injected directly into the user prompt template, and instructions is
        set to None.  For models supporting system roles, system instructions
        are rendered into instructions.

        ``domain`` is a :class:`~sqwakvox.domains.base.DocumentDomain`; when
        None the financial domain is used (backward compatibility).
        """
        from sqwakvox.domains import get_domain

        domain = domain or get_domain("financial")
        system_tpl, user_tpl = _default_templates()
        system_tpl = domain.system_instructions or system_tpl
        user_tpl = domain.user_prompt_template or user_tpl

        if not ModelProvider.supports_system_role(model_id):
            instructions = None
            formatted_prompt = user_tpl.render(context=context, prompt=prompt)
        else:
            instructions = system_tpl.render(context=context)
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
        domain_id: str = "financial",
    ) -> str:
        from sqwakvox.domains import get_domain

        domain = get_domain(domain_id)
        instructions, formatted_prompt = cls.render_prompt(
            model_id=model_id,
            context=context,
            prompt=prompt,
            domain=domain,
        )

        # Attach the Redis checkpointer only when the caller supplies a thread
        # id, so existing stateless call sites (and the no-tools direct path)
        # keep their current behaviour.
        checkpointer = _get_redis_checkpointer() if thread_id else None

        logger.info("Starting agent execution - model: %s", model_id)
        with (
            trace_span("sqwakvox.agent.orchestration", {"model_id": model_id}),
            cls.inject_credentials(env_var, api_key),
        ):
            # Agent creation and execution must share one event loop: MCP stdio
            # connections are bound to the loop that opened them, so connecting
            # in one loop and running in another leaves the subprocess pipes
            # dead.  asyncio.run gives both a single loop, but only because the
            # whole run is one coroutine.
            return asyncio.run(
                cls._run_async(
                    model_id=model_id,
                    api_key=api_key,
                    instructions=instructions,
                    prompt=formatted_prompt,
                    mcp_servers=mcp_servers or [],
                    thread_id=thread_id,
                    checkpointer=checkpointer,
                )
            )

    @classmethod
    async def _run_async(
        cls,
        model_id: str,
        api_key: str,
        instructions: str | None,
        prompt: str,
        mcp_servers: list[MCPParams],
        thread_id: str | None,
        checkpointer: Any | None,
    ) -> str:
        """Connect to MCP servers, run the ReAct agent, and tear everything down.

        With no MCP tools configured the agent loop is skipped entirely and the
        model is called directly.  ``create_react_agent`` with an empty tool
        list still wraps the model in a tool-calling loop, and reasoning models
        can burn the whole recursion budget trying to invoke tools that do not
        exist.
        """
        if not mcp_servers:
            return await cls._run_direct_model(
                model_id=model_id,
                api_key=api_key,
                instructions=instructions,
                prompt=prompt,
            )

        for attempt in range(1, MAX_MCP_STARTUP_ATTEMPTS + 1):
            try:
                async with connected_tools(mcp_servers) as tools:
                    return await cls._run_with_tools(
                        model_id=model_id,
                        api_key=api_key,
                        instructions=instructions,
                        prompt=prompt,
                        tools=tools,
                        mcp_server_count=len(mcp_servers),
                        thread_id=thread_id,
                        checkpointer=checkpointer,
                    )
            except MCPConnectionError as exc:
                if attempt >= MAX_MCP_STARTUP_ATTEMPTS:
                    logger.warning(
                        "MCP startup failed %d time(s); proceeding WITHOUT tools. %s",
                        attempt,
                        exc,
                    )
                    return await cls._run_direct_model(
                        model_id=model_id,
                        api_key=api_key,
                        instructions=instructions,
                        prompt=prompt,
                    )
                logger.warning(
                    "MCP server startup failed (attempt %d/%d): %s",
                    attempt,
                    MAX_MCP_STARTUP_ATTEMPTS,
                    exc,
                )
        raise AssertionError("unreachable")  # pragma: no cover

    @classmethod
    async def _run_with_tools(
        cls,
        model_id: str,
        api_key: str,
        instructions: str | None,
        prompt: str,
        tools: list[BaseTool],
        mcp_server_count: int,
        thread_id: str | None,
        checkpointer: Any | None,
    ) -> str:
        """Run the agent, or degrade to a direct call if no tools survived."""
        if not tools:
            logger.warning(
                "No MCP tools resolved for %d configured server(s); running without tools.",
                mcp_server_count,
            )
            return await cls._run_direct_model(
                model_id=model_id,
                api_key=api_key,
                instructions=instructions,
                prompt=prompt,
            )

        cls._log_available_tools(tools)
        return await cls._invoke_agent(
            model_id=model_id,
            api_key=api_key,
            instructions=instructions,
            prompt=prompt,
            tools=tools,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )

    @classmethod
    async def _invoke_agent(
        cls,
        model_id: str,
        api_key: str,
        instructions: str | None,
        prompt: str,
        tools: list[BaseTool],
        thread_id: str | None,
        checkpointer: Any | None,
    ) -> str:
        """Run the ReAct agent over *tools* and extract the final answer."""
        agent = create_react_agent(
            model=build_chat_model(model_id, api_key),
            tools=tools,
            prompt=instructions,
            checkpointer=checkpointer,
        )

        start_time = time.monotonic()
        with trace_span(
            "sqwakvox.agent.react_agent_run",
            {"model_id": model_id, "mcp_servers_count": len(tools)},
        ) as span:
            try:
                run_config: dict[str, Any] = {
                    "recursion_limit": MAX_AGENT_RECURSION_LIMIT,
                }
                if thread_id:
                    run_config["configurable"] = {"thread_id": thread_id}
                result = await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": [HumanMessage(content=prompt)]},
                        config=cast("Any", run_config),
                    ),
                    timeout=AGENT_RUN_TIMEOUT_SECONDS,
                )
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
                        "Agent run took %.1fs - near the timeout of %ss.",
                        elapsed,
                        AGENT_RUN_TIMEOUT_SECONDS,
                    )

            messages = result.get("messages") if isinstance(result, dict) else None
            if not messages:
                raise ValueError("Agent run returned no messages.")

            # Unwrap the final message so structured/chart payloads don't leak
            # Python object reprs into the user-facing response.
            response = _extract_response(messages[-1])
            span.set_attribute("response_len", len(response))
            span.set_attribute("elapsed_sec", time.monotonic() - start_time)
            span.set_attribute("messages_count", len(messages))
            logger.info(
                "Agent execution complete - response: %d chars, elapsed: %.1fs, messages: %d",
                len(response),
                time.monotonic() - start_time,
                len(messages),
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

    @classmethod
    async def _run_direct_model(
        cls,
        model_id: str,
        api_key: str,
        instructions: str | None,
        prompt: str,
    ) -> str:
        """Call the model directly - no ReAct loop at all.

        One system prompt + one user message = one response.
        """
        from any_llm import acompletion

        logger.info("Running direct model call (no tools) - model: %s", model_id)
        start_time = time.monotonic()
        messages: list[dict[str, Any]] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})

        with trace_span("sqwakvox.agent.direct_model_call", {"model_id": model_id}) as span:
            try:
                response = await asyncio.wait_for(
                    acompletion(
                        model=model_id,
                        api_key=api_key,
                        messages=cast("Any", messages),
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
                    "Direct model call complete - response length: %d chars, elapsed: %.1fs",
                    len(text),
                    elapsed,
                )
                return text
            except TimeoutError:
                elapsed = time.monotonic() - start_time
                logger.error("Direct model call TIMED OUT after %.1fs", elapsed)
                raise

    @staticmethod
    def _log_available_tools(tools: list[BaseTool]) -> None:
        names = tool_names(tools)
        if names:
            logger.info(
                "Agent has %d tool(s) registered: %s",
                len(names),
                ", ".join(names),
            )
        else:
            logger.warning(
                "No tools are registered for this run. If an MCP server (e.g. calc-stats) "
                "was expected, it likely failed to start or connect."
            )
