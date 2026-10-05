from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from any_llm.types.completion import ChatCompletion, Choice, CompletionUsage
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool

from sqwakvox.llm import (
    ChatAnyLLM,
    _convert_delta_to_message_chunk,
    _convert_dict_to_message,
    _convert_message_to_dict,
    build_chat_model,
)


def _tool() -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda expression: expression,
        name="calculator",
        description="Evaluate an expression",
    )


def _completion(content: str) -> ChatCompletion:
    return ChatCompletion(
        id="id",
        created=0,
        model="gpt-5.5-high",
        object="chat.completion",
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message={"role": "assistant", "content": content},
            )
        ],
        usage=CompletionUsage(prompt_tokens=11, completion_tokens=3, total_tokens=14),
    )


class TestBuildChatModel:
    def test_carries_model_id_and_key(self) -> None:
        model = build_chat_model("anthropic:claude-4.6", "sk-test")

        assert isinstance(model, ChatAnyLLM)
        assert model.model == "anthropic:claude-4.6"
        assert model.api_key == "sk-test"

    def test_llm_type_is_stable(self) -> None:
        """LangGraph uses this for tracing/caching keys."""
        assert build_chat_model("openai:gpt-5.5-high", "k")._llm_type == "anyllm-chat"


class TestSyncCallIsRejected:
    def test_generate_raises_clear_error(self) -> None:
        model = build_chat_model("openai:gpt-5.5-high", "k")

        with pytest.raises(NotImplementedError, match="async-only"):
            model.invoke("hello")


class TestBindTools:
    def test_binds_openai_tool_schema(self) -> None:
        bound = build_chat_model("openai:gpt-5.5-high", "k").bind_tools([_tool()])

        tools = bound.kwargs["tools"]
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "calculator"
        assert "expression" in tools[0]["function"]["parameters"]["properties"]


class TestAgenerate:
    @pytest.mark.asyncio
    async def test_returns_text_and_usage(self) -> None:
        model = build_chat_model("openai:gpt-5.5-high", "sk-test")

        with patch("any_llm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.return_value = _completion("Hello there")
            result = await model.agenerate([[HumanMessage(content="hi")]])

        generation = result.generations[0][0]
        assert generation.message.content == "Hello there"
        assert generation.message.usage_metadata == {
            "input_tokens": 11,
            "output_tokens": 3,
            "total_tokens": 14,
        }
        assert generation.generation_info == {"finish_reason": "stop"}

    @pytest.mark.asyncio
    async def test_forwards_model_id_and_key_to_any_llm(self) -> None:
        model = build_chat_model("gemini:gemini-3.5-pro", "gem-key")

        with patch("any_llm.acompletion", new_callable=AsyncMock) as mock_acompletion:
            mock_acompletion.return_value = _completion("ok")
            await model.agenerate([[SystemMessage(content="s"), HumanMessage(content="h")]])

        kwargs = mock_acompletion.call_args.kwargs
        assert kwargs["model"] == "gemini:gemini-3.5-pro"
        assert kwargs["api_key"] == "gem-key"
        assert kwargs["messages"] == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "h"},
        ]

    @pytest.mark.asyncio
    async def test_rejects_non_chatcompletion(self) -> None:
        model = build_chat_model("openai:gpt-5.5-high", "k")

        with (
            patch("any_llm.acompletion", new_callable=AsyncMock) as mock_acompletion,
            pytest.raises(ValueError, match="Expected ChatCompletion"),
        ):
            mock_acompletion.return_value = object()
            await model.agenerate([[HumanMessage(content="hi")]])


class TestMessageCodecs:
    @pytest.mark.parametrize(
        "message",
        [
            SystemMessage(content="s"),
            HumanMessage(content="h"),
            AIMessage(content="a"),
            FunctionMessage(content="f", name="fn"),
            ToolMessage(content="t", tool_call_id="call-1"),
        ],
    )
    def test_round_trip_preserves_role(self, message: Any) -> None:
        encoded = _convert_message_to_dict(message)

        assert _convert_dict_to_message(encoded).content == message.content

    def test_tool_message_encodes_call_id(self) -> None:
        """LangGraph matches tool results by id; dropping it breaks the loop."""
        encoded = _convert_message_to_dict(ToolMessage(content="t", tool_call_id="call-9"))

        assert encoded["role"] == "tool"
        assert encoded["tool_call_id"] == "call-9"

    def test_ai_message_encodes_tool_calls(self) -> None:
        message = AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "calculator", "args": {"expression": "1+1"}}],
        )

        encoded = _convert_message_to_dict(message)

        assert encoded["role"] == "assistant"
        assert encoded["tool_calls"][0]["function"]["name"] == "calculator"
        assert encoded["tool_calls"][0]["type"] == "function"

    def test_delta_converts_to_ai_chunk(self) -> None:
        from any_llm.types.completion import ChoiceDelta

        chunk = _convert_delta_to_message_chunk(
            ChoiceDelta(role="assistant", content="partial"),
            AIMessageChunk,
        )

        assert isinstance(chunk, AIMessageChunk)
        assert chunk.content == "partial"

    def test_delta_skips_malformed_tool_calls(self) -> None:
        """A provider delta without a function payload must not raise."""
        from any_llm.types.completion import ChoiceDelta

        chunk = _convert_delta_to_message_chunk(
            ChoiceDelta(role="assistant", content="x", tool_calls=[]),
            AIMessageChunk,
        )

        assert chunk.content == "x"
