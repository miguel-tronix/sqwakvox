"""LangChain chat model backed by ``any-llm``.

This is a vendored, async-only reduction of two files from the (now
soft-deprecated) ``any-agent`` package:

* ``any_agent/vendor/langchain_any_llm.py`` — message <-> OpenAI-dict codecs,
  itself vendored from ``langchain-litellm`` and adapted to any-llm;
* ``any_agent/frameworks/langchain.py::ChatAnyLLM`` — the ``BaseChatModel`` that
  drives those codecs.

Keeping any-llm as the provider layer means one integration covers every
provider in :class:`sqwakvox.models.ModelProvider` (OpenAI, Anthropic, Gemini,
DeepSeek, Bedrock, Ollama, ...) behind a single ``"provider:model"`` string.
``any-guardrail`` also depends on ``any-llm-sdk``, so it stays either way.

Trimmed relative to the original:

* only ``_agenerate`` is implemented.  Sqwakvox is async end to end
  (:meth:`sqwakvox.agent.AnyAgentOrchestrator.execute_query` is sync only
  because Celery requires it, and it immediately delegates to an async body),
  so the sync and streaming paths were dead code.  Re-adding ``_astream`` later
  is additive.
* the exception-wrapping callbacks any-agent layered on top are gone; they
  existed to normalise errors across seven agent frameworks, and there is only
  one here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any, cast

from any_llm.types.completion import ChatCompletion as AnyLLMChatCompletion
from any_llm.types.completion import ChoiceDelta
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    BaseMessageChunk,
    ChatMessage,
    ChatMessageChunk,
    FunctionMessage,
    FunctionMessageChunk,
    HumanMessage,
    HumanMessageChunk,
    SystemMessage,
    SystemMessageChunk,
    ToolCall,
    ToolCallChunk,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

__all__ = ["ChatAnyLLM", "build_chat_model"]


def _convert_dict_to_message(_dict: Mapping[str, Any]) -> BaseMessage:
    role = _dict["role"]
    if role == "user":
        return HumanMessage(content=_dict["content"])
    if role == "assistant":
        content = _dict.get("content", "") or ""
        additional_kwargs: dict[str, Any] = {}
        if _dict.get("function_call"):
            additional_kwargs["function_call"] = dict(_dict["function_call"])
        if _dict.get("tool_calls"):
            additional_kwargs["tool_calls"] = _dict["tool_calls"]
        return AIMessage(content=content, additional_kwargs=additional_kwargs)
    if role == "system":
        return SystemMessage(content=_dict["content"])
    if role == "function":
        return FunctionMessage(content=_dict["content"], name=_dict["name"])
    if role == "tool":
        return ToolMessage(content=_dict["content"], tool_call_id=_dict["tool_call_id"])
    return ChatMessage(content=_dict["content"], role=role)


def _convert_delta_to_message_chunk(
    delta: ChoiceDelta, default_class: type[BaseMessageChunk]
) -> BaseMessageChunk:
    role = delta.role
    content = delta.content or ""
    additional_kwargs: dict[str, Any] = {}
    if delta.function_call:
        additional_kwargs["function_call"] = dict(delta.function_call)
    reasoning = getattr(delta, "reasoning", None)
    if reasoning and reasoning.content:
        additional_kwargs["reasoning_content"] = reasoning.content

    tool_call_chunks = []
    if raw_tool_calls := delta.tool_calls:
        additional_kwargs["tool_calls"] = raw_tool_calls
        # A provider that sends tool-call deltas without a `function` payload
        # would raise KeyError here; skip those rather than lose the stream.
        with suppress(KeyError):
            tool_call_chunks = [
                ToolCallChunk(
                    name=rtc.function.name if rtc.function else "",
                    args=rtc.function.arguments if rtc.function else "",
                    id=rtc.id,
                    index=rtc.index,
                )
                for rtc in raw_tool_calls
                if rtc.function
            ]

    if role == "user" or default_class == HumanMessageChunk:
        return HumanMessageChunk(content=content)
    if role == "assistant" or default_class == AIMessageChunk:
        return AIMessageChunk(
            content=content,
            additional_kwargs=additional_kwargs,
            tool_call_chunks=tool_call_chunks,
        )
    if role == "system" or default_class == SystemMessageChunk:
        return SystemMessageChunk(content=content)
    if default_class == FunctionMessageChunk and delta.function_call:
        return FunctionMessageChunk(
            content=delta.function_call.arguments or "",
            name=delta.function_call.name or "",
        )
    if role == "tool" or default_class == ChatMessageChunk:
        return ChatMessageChunk(content=content, role=role)  # type: ignore[arg-type]
    return default_class(content=content)  # type: ignore[call-arg]


def _lc_tool_call_to_openai_tool_call(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "type": "function",
        "id": tool_call["id"],
        "function": {
            "name": tool_call["name"],
            "arguments": json.dumps(tool_call["args"]),
        },
    }


def _convert_message_to_dict(message: BaseMessage) -> dict[str, Any]:
    message_dict: dict[str, Any] = {"content": message.content}
    if isinstance(message, ChatMessage):
        message_dict["role"] = message.role
    elif isinstance(message, HumanMessage):
        message_dict["role"] = "user"
    elif isinstance(message, AIMessage):
        message_dict["role"] = "assistant"
        if "function_call" in message.additional_kwargs:
            message_dict["function_call"] = message.additional_kwargs["function_call"]
        if message.tool_calls:
            message_dict["tool_calls"] = [
                _lc_tool_call_to_openai_tool_call(tc) for tc in message.tool_calls
            ]
        elif "tool_calls" in message.additional_kwargs:
            message_dict["tool_calls"] = message.additional_kwargs["tool_calls"]
    elif isinstance(message, SystemMessage):
        message_dict["role"] = "system"
    elif isinstance(message, FunctionMessage):
        message_dict["role"] = "function"
        message_dict["name"] = message.name
    elif isinstance(message, ToolMessage):
        message_dict["role"] = "tool"
        message_dict["tool_call_id"] = message.tool_call_id
    else:
        raise ValueError(f"Got unknown type {message}")
    if "name" in message.additional_kwargs:
        message_dict["name"] = message.additional_kwargs["name"]
    return message_dict


class ChatAnyLLM(BaseChatModel):
    """A :class:`~langchain_core.language_models.chat_models.BaseChatModel` over any-llm.

    ``model`` is an any-llm model id in ``"provider:model"`` form, e.g.
    ``"anthropic:claude-4.6"``.  The API key is passed explicitly rather than
    read from the environment: it is injected per request by
    :meth:`sqwakvox.agent.AnyAgentOrchestrator.inject_credentials` and travels
    through the Celery task rather than a shared process env.
    """

    model: str
    api_key: str | None = None
    api_base: str | None = None
    model_kwargs: Any = None

    @property
    def _llm_type(self) -> str:
        return "anyllm-chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model, **(self.model_kwargs or {})}

    def _completion_params(self, stop: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "api_key": self.api_key,
            "api_base": self.api_base,
            "model": self.model,
            **(self.model_kwargs or {}),
        }
        if stop is not None:
            if "stop" in params:
                raise ValueError("`stop` found in both the input and default params.")
            params["stop"] = stop
        return {**params, **kwargs}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Unsupported: ``BaseChatModel`` declares this abstract.

        Sqwakvox runs agents only from async contexts.  Use ``ainvoke`` /
        ``agenerate``; reaching this method means a sync call slipped in.
        """
        raise NotImplementedError(
            "ChatAnyLLM is async-only. Use ainvoke()/agenerate() instead of invoke()/generate()."
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        # Required by the BaseChatModel override. Unused: sqwakvox drives
        # observability through its own OTel spans, not LangChain callbacks.
        run_manager: AsyncCallbackManagerForLLMRun | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> ChatResult:
        from any_llm import acompletion

        response = await acompletion(
            messages=cast("Any", [_convert_message_to_dict(m) for m in messages]),
            **self._completion_params(stop, **kwargs),
        )
        if not isinstance(response, AnyLLMChatCompletion):
            raise ValueError(f"Expected ChatCompletion, got {type(response)}")

        usage = response.usage
        generations = []
        for choice in response.model_dump()["choices"]:
            message = _convert_dict_to_message(choice["message"])
            if isinstance(message, AIMessage) and usage:
                message.response_metadata = {"model_name": self.model}
                message.usage_metadata = UsageMetadata(
                    input_tokens=usage.prompt_tokens,
                    output_tokens=usage.completion_tokens,
                    total_tokens=usage.prompt_tokens + usage.completion_tokens,
                )
            generations.append(
                ChatGeneration(
                    message=message,
                    generation_info={"finish_reason": choice.get("finish_reason")},
                )
            )
        return ChatResult(
            generations=generations,
            llm_output={"token_usage": usage, "model": self.model},
        )

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type[BaseModel] | Callable[..., Any] | BaseTool],
        tool_choice: dict[str, Any] | str | bool | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Bind LangChain tools to the underlying any-llm provider.

        Every provider in any-llm is normalised to the OpenAI tool-calling
        schema, so a single conversion covers them all.
        """
        formatted_tools = [convert_to_openai_tool(tool) for tool in tools]
        return super().bind(tools=formatted_tools, tool_choice=tool_choice, **kwargs)


def build_chat_model(model_id: str, api_key: str) -> ChatAnyLLM:
    """Construct a :class:`ChatAnyLLM` for *model_id* authenticated by *api_key*."""
    return ChatAnyLLM(model=model_id, api_key=api_key, api_base=None, model_kwargs={})
