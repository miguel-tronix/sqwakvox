import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from sqwakvox.agent import (
    MAX_AGENT_RECURSION_LIMIT,
    MAX_MCP_STARTUP_ATTEMPTS,
    AnyAgentOrchestrator,
    _extract_response,
)
from sqwakvox.llm import _convert_message_to_dict
from sqwakvox.mcp import MCPConnectionError, MCPStdio
from sqwakvox.models import ModelProvider


def _completed(content: str) -> dict[str, list[AIMessage]]:
    """Shape a LangGraph agent result carrying a single final AI message."""
    return {"messages": [AIMessage(content=content)]}


def _fake_tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda **_kwargs: "ok",
        name=name,
        description="test tool",
    )


def _fake_connected_tools(
    tools: list[Any],
) -> Any:
    @asynccontextmanager
    async def _cm(_configs: Any) -> AsyncIterator[list[Any]]:
        yield tools

    return _cm


CHART_TEXT = (
    "Cash Flow From Operating Activities\n"
    "2022  ████████████████████ 124.0\n"
    "2023  ████████████████████████ 158.0"
)


def test_extract_response_plain_str_passthrough() -> None:
    assert _extract_response(CHART_TEXT) == CHART_TEXT


def test_extract_response_python_repr_object() -> None:
    """Model echoes the mcp-ascii-charts JSON object as a Python repr."""
    payload = repr({"chart": CHART_TEXT, "title": "t", "dimensions": {"width": 60}})
    out = _extract_response(payload)
    assert out == CHART_TEXT
    assert "'chart'" not in out


def test_extract_response_json_object() -> None:
    payload = json.dumps({"chart": CHART_TEXT, "title": "t"})
    assert _extract_response(payload) == CHART_TEXT


def test_extract_response_prose_with_embedded_object() -> None:
    payload = "Here is the chart:\n" + repr({"chart": CHART_TEXT}) + "\nHope that helps!"
    out = _extract_response(payload)
    assert CHART_TEXT in out
    assert "'chart'" not in out


def test_extract_response_content_block_list_repr() -> None:
    """any_agent's str(content) leak for content-block lists is recoverable."""
    payload = repr([{"type": "text", "text": "Here:\n" + CHART_TEXT}])
    out = _extract_response(payload)
    assert "Here:" in out
    assert CHART_TEXT in out
    assert "{'type'" not in out


def test_extract_response_output_dict() -> None:
    assert _extract_response({"output": "chart answer"}) == "chart answer"


def test_extract_response_messages_dict() -> None:
    payload = {"messages": [{"type": "ai", "content": CHART_TEXT}]}
    assert _extract_response(payload) == CHART_TEXT


def test_extract_response_aimessage_object() -> None:
    assert _extract_response(AIMessage(content=CHART_TEXT)) == CHART_TEXT


def test_extract_response_none() -> None:
    assert _extract_response(None) == ""


def test_extract_response_prose_untouched() -> None:
    prose = "Revenue grew 12% year over year."
    assert _extract_response(prose) == prose


def test_extract_response_json_with_lowercase_boolean() -> None:
    payload = '{"chart": "A ███ 10", "title": "Sales", "success": true}'
    out = _extract_response(payload)
    assert out == "A ███ 10"


def test_extract_response_embedded_json_with_boolean() -> None:
    payload = 'Here is chart:\n{"chart": "A ███ 10", "success": true}\nHope it helps!'
    out = _extract_response(payload)
    assert out == "Here is chart:\nA ███ 10\nHope it helps!"


def test_extract_response_nested_chart_dict() -> None:
    payload = {"chart": {"text": "A ███ 10", "title": "Sales"}}
    out = _extract_response(payload)
    assert out == "A ███ 10"


def test_model_provider_supports_system_role() -> None:
    assert ModelProvider.supports_system_role("gemini:gemini-3.6-flash") is False
    assert ModelProvider.supports_system_role("gemini:gemini-3.5-flash") is True
    assert ModelProvider.supports_system_role("gemini:gemini-3.5-pro") is True
    assert ModelProvider.supports_system_role("openai:gpt-5.5-high") is True
    assert ModelProvider.supports_system_role("unknown:model") is True


def test_render_prompt_gemini_36_flash() -> None:
    context = "Q4 Net Income: $50M"
    prompt = "What is the net income?"

    instructions, formatted_prompt = AnyAgentOrchestrator.render_prompt(
        model_id="gemini:gemini-3.6-flash",
        context=context,
        prompt=prompt,
    )

    assert instructions is None
    assert "Financial Document Assistant" in formatted_prompt
    assert "--- DOCUMENT CONTEXT ---" in formatted_prompt
    assert "Q4 Net Income: $50M" in formatted_prompt
    assert "What is the net income?" in formatted_prompt


def test_render_prompt_standard_model() -> None:
    context = "Q4 Net Income: $50M"
    prompt = "What is the net income?"

    instructions, formatted_prompt = AnyAgentOrchestrator.render_prompt(
        model_id="gemini:gemini-3.5-flash",
        context=context,
        prompt=prompt,
    )

    assert instructions is not None
    assert "Financial Document Assistant" in instructions
    assert "Q4 Net Income: $50M" in instructions
    assert formatted_prompt == prompt


@patch.object(AnyAgentOrchestrator, "_run_async")
def test_execute_query_gemini_36(mock_run: AsyncMock) -> None:
    mock_run.return_value = "Test response"

    result = AnyAgentOrchestrator.execute_query(
        model_id="gemini:gemini-3.6-flash",
        api_key="test-key",
        context="Context payload",
        prompt="User question",
        env_var="GEMINI_API_KEY",
    )

    assert result == "Test response"
    assert mock_run.called
    kwargs = mock_run.call_args.kwargs
    assert kwargs["model_id"] == "gemini:gemini-3.6-flash"
    assert kwargs["api_key"] == "test-key"
    assert kwargs["instructions"] is None
    formatted_prompt: str = kwargs["prompt"]
    assert "Context payload" in formatted_prompt
    assert "User question" in formatted_prompt


@patch.object(AnyAgentOrchestrator, "_run_async")
def test_execute_query_gemini_35(mock_run: AsyncMock) -> None:
    mock_run.return_value = "Test response"

    result = AnyAgentOrchestrator.execute_query(
        model_id="gemini:gemini-3.5-flash",
        api_key="test-key",
        context="Context payload",
        prompt="User question",
        env_var="GEMINI_API_KEY",
    )

    assert result == "Test response"
    kwargs = mock_run.call_args.kwargs
    assert kwargs["model_id"] == "gemini:gemini-3.5-flash"
    assert kwargs["instructions"] is not None
    assert "Context payload" in kwargs["instructions"]
    assert kwargs["prompt"] == "User question"


@patch.object(AnyAgentOrchestrator, "_run_async")
def test_execute_query_injects_api_key_into_environment(
    mock_run: AsyncMock,
) -> None:
    """The key must reach the provider process env for the duration of the run."""
    mock_run.return_value = _completed("ok")
    env_var = "SQWAKVOX_TEST_KEY"

    AnyAgentOrchestrator.execute_query(
        model_id="openai:gpt-5.5-high",
        api_key="secret-value",
        context="ctx",
        prompt="q",
        env_var=env_var,
    )

    assert env_var not in os.environ


def test_execute_query_sets_env_var_during_run() -> None:
    env_var = "SQWAKVOX_TEST_KEY"
    seen: list[str | None] = []

    async def fake_run(**_kwargs: Any) -> str:
        seen.append(os.environ.get(env_var))
        return "ok"

    with patch.object(AnyAgentOrchestrator, "_run_async", new=fake_run):
        AnyAgentOrchestrator.execute_query(
            model_id="openai:gpt-5.5-high",
            api_key="secret-value",
            context="ctx",
            prompt="q",
            env_var=env_var,
        )

    assert seen == ["secret-value"]
    assert env_var not in os.environ


@pytest.mark.asyncio
async def test_run_direct_model_without_instructions() -> None:
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "Direct answer"

    with patch("any_llm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = fake_response
        res = await AnyAgentOrchestrator._run_direct_model(
            model_id="gemini:gemini-3.6-flash",
            api_key="test-key",
            instructions=None,
            prompt="Combined prompt text",
        )

        assert res == "Direct answer"
        mock_acompletion.assert_called_once()
        kwargs = mock_acompletion.call_args.kwargs
        assert kwargs["model"] == "gemini:gemini-3.6-flash"
        assert kwargs["api_key"] == "test-key"
        messages = kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Combined prompt text"


@pytest.mark.asyncio
async def test_run_direct_model_with_instructions() -> None:
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "Direct answer"

    with patch("any_llm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = fake_response
        res = await AnyAgentOrchestrator._run_direct_model(
            model_id="gemini:gemini-3.5-flash",
            api_key="test-key",
            instructions="System instructions text",
            prompt="User prompt text",
        )

        assert res == "Direct answer"
        mock_acompletion.assert_called_once()
        messages = mock_acompletion.call_args.kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "System instructions text"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "User prompt text"


@pytest.mark.asyncio
async def test_run_async_without_mcp_servers_uses_direct_model() -> None:
    """No tools configured means no ReAct loop at all."""
    with patch.object(
        AnyAgentOrchestrator, "_run_direct_model", new_callable=AsyncMock
    ) as mock_direct:
        mock_direct.return_value = "direct"
        res = await AnyAgentOrchestrator._run_async(
            model_id="openai:gpt-5.5-high",
            api_key="k",
            instructions=None,
            prompt="p",
            mcp_servers=[],
            thread_id=None,
            checkpointer=None,
        )

    assert res == "direct"
    mock_direct.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_async_no_resolved_tools_falls_back_to_direct_model() -> None:
    """A server that connects but exposes nothing must not leave the model stranded."""
    with (
        patch(
            "sqwakvox.agent.connected_tools",
            new=_fake_connected_tools([]),
        ),
        patch.object(
            AnyAgentOrchestrator, "_run_direct_model", new_callable=AsyncMock
        ) as mock_direct,
    ):
        mock_direct.return_value = "direct"
        res = await AnyAgentOrchestrator._run_async(
            model_id="openai:gpt-5.5-high",
            api_key="k",
            instructions=None,
            prompt="p",
            mcp_servers=[MCPStdio(command="x", args=[])],
            thread_id=None,
            checkpointer=None,
        )

    assert res == "direct"
    mock_direct.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_async_retries_mcp_then_succeeds() -> None:
    """A transient MCP startup failure is retried, not fatal."""
    calls = {"n": 0}

    @asynccontextmanager
    async def flaky(_configs: Any) -> AsyncIterator[list[Any]]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise MCPConnectionError("boom")
        yield [_fake_tool("calculator")]

    with (
        patch("sqwakvox.agent.connected_tools", new=flaky),
        patch.object(AnyAgentOrchestrator, "_invoke_agent", new_callable=AsyncMock) as mock_invoke,
    ):
        mock_invoke.return_value = "answered"
        res = await AnyAgentOrchestrator._run_async(
            model_id="openai:gpt-5.5-high",
            api_key="k",
            instructions=None,
            prompt="p",
            mcp_servers=[MCPStdio(command="x", args=[])],
            thread_id=None,
            checkpointer=None,
        )

    assert res == "answered"
    assert calls["n"] == 2
    mock_invoke.assert_awaited_once()
    assert mock_invoke.call_args.kwargs["tools"][0].name == "calculator"


@pytest.mark.asyncio
async def test_run_async_degrades_to_no_tools_after_retry_failure() -> None:
    """Persistent MCP failure still yields an answer, without tools."""
    calls = {"n": 0}

    @asynccontextmanager
    async def always_fails(_configs: Any) -> AsyncIterator[list[Any]]:
        calls["n"] += 1
        raise MCPConnectionError("down")
        yield []  # pragma: no cover

    with (
        patch("sqwakvox.agent.connected_tools", new=always_fails),
        patch.object(
            AnyAgentOrchestrator, "_run_direct_model", new_callable=AsyncMock
        ) as mock_direct,
    ):
        mock_direct.return_value = "no tools answer"
        res = await AnyAgentOrchestrator._run_async(
            model_id="openai:gpt-5.5-high",
            api_key="k",
            instructions=None,
            prompt="p",
            mcp_servers=[MCPStdio(command="x", args=[])],
            thread_id=None,
            checkpointer=None,
        )

    assert res == "no tools answer"
    assert calls["n"] == MAX_MCP_STARTUP_ATTEMPTS
    mock_direct.assert_awaited_once()


@pytest.mark.asyncio
async def test_invoke_agent_extracts_final_message() -> None:
    result = _completed("The final answer")
    fake_agent = MagicMock()
    fake_agent.ainvoke = AsyncMock(return_value=result)

    with patch("sqwakvox.agent.create_react_agent", return_value=fake_agent) as mock_create:
        res = await AnyAgentOrchestrator._invoke_agent(
            model_id="openai:gpt-5.5-high",
            api_key="k",
            instructions="sys",
            prompt="the question",
            tools=[_fake_tool("calculator")],
            thread_id="thread-1",
            checkpointer=None,
        )

    assert res == "The final answer"
    assert mock_create.call_args.kwargs["prompt"] == "sys"
    assert mock_create.call_args.kwargs["model"].model == "openai:gpt-5.5-high"
    assert mock_create.call_args.kwargs["model"].api_key == "k"

    _input, config_kwargs = fake_agent.ainvoke.call_args
    sent = _input[0]["messages"][0]
    assert isinstance(sent, HumanMessage)
    assert sent.content == "the question"
    assert config_kwargs["config"]["recursion_limit"] == MAX_AGENT_RECURSION_LIMIT
    assert config_kwargs["config"]["configurable"] == {"thread_id": "thread-1"}


@pytest.mark.asyncio
async def test_invoke_agent_omits_thread_config_when_no_thread_id() -> None:
    fake_agent = MagicMock()
    fake_agent.ainvoke = AsyncMock(return_value=_completed("answer"))

    with patch("sqwakvox.agent.create_react_agent", return_value=fake_agent):
        await AnyAgentOrchestrator._invoke_agent(
            model_id="openai:gpt-5.5-high",
            api_key="k",
            instructions=None,
            prompt="p",
            tools=[_fake_tool("calculator")],
            thread_id=None,
            checkpointer=None,
        )

    _input, config_kwargs = fake_agent.ainvoke.call_args
    assert "configurable" not in config_kwargs["config"]


@pytest.mark.asyncio
async def test_invoke_agent_raises_when_no_messages() -> None:
    fake_agent = MagicMock()
    fake_agent.ainvoke = AsyncMock(return_value={"messages": []})

    with (
        patch("sqwakvox.agent.create_react_agent", return_value=fake_agent),
        pytest.raises(ValueError, match="no messages"),
    ):
        await AnyAgentOrchestrator._invoke_agent(
            model_id="openai:gpt-5.5-high",
            api_key="k",
            instructions=None,
            prompt="p",
            tools=[_fake_tool("calculator")],
            thread_id=None,
            checkpointer=None,
        )


def test_gemini_accepts_tool_role_messages() -> None:
    """Regression guard for Gemini tool role compatibility.

    The ReAct loop emits ToolMessage, which any-llm maps to role="tool"; the
    Gemini provider must translate that to a role it accepts ('user' or 'model').
    """
    import any_llm.providers.gemini.utils as gemini_utils

    tool_message = _convert_message_to_dict(
        ToolMessage(content=json.dumps({"result": 42}), tool_call_id="call-1")
    )
    assert tool_message["role"] == "tool"

    formatted_messages, _ = gemini_utils._convert_messages(
        [
            {"role": "user", "content": "Query"},
            {"role": "tool", "name": "calc", "content": json.dumps({"result": 42})},
        ]
    )
    for msg in formatted_messages:
        assert msg.role in ("user", "model"), f"Role {msg.role} is not supported by Gemini API!"


def test_redis_checkpointer_sanitizes_non_packable_aimessage() -> None:
    from sqwakvox.backend.redis_checkpointer import RedisCheckpointer

    class UnpackableObj:
        def __repr__(self) -> str:
            return "<UnpackableObj>"

    msg = AIMessage(
        content="Chart result",
        response_metadata={"raw_client": UnpackableObj()},
        additional_kwargs={"extra": UnpackableObj()},
    )

    checkpointer = RedisCheckpointer("redis://localhost:6379/2")
    raw_dump = checkpointer._dumps(msg)
    assert isinstance(raw_dump, str)

    loaded_msg = checkpointer._loads(raw_dump)
    assert isinstance(loaded_msg, AIMessage)
    assert loaded_msg.content == "Chart result"
    assert loaded_msg.response_metadata["raw_client"] == "<UnpackableObj>"
    assert loaded_msg.additional_kwargs["extra"] == "<UnpackableObj>"
