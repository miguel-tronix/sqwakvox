from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from any_agent import AgentConfig

from sqwakvox.agent import AnyAgentOrchestrator
from sqwakvox.models import ModelProvider


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


@patch.object(AnyAgentOrchestrator, "_execute_in_single_loop")
def test_execute_query_gemini_36(mock_execute: MagicMock) -> None:
    mock_execute.return_value = "Test response"

    result = AnyAgentOrchestrator.execute_query(
        model_id="gemini:gemini-3.6-flash",
        api_key="test-key",
        context="Context payload",
        prompt="User question",
        env_var="GEMINI_API_KEY",
    )

    assert result == "Test response"
    assert mock_execute.called
    kwargs = mock_execute.call_args.kwargs
    config: AgentConfig = kwargs["config"]
    formatted_prompt: str = kwargs["prompt"]

    assert config.model_id == "gemini:gemini-3.6-flash"
    assert config.instructions is None
    assert "Context payload" in formatted_prompt
    assert "User question" in formatted_prompt


@patch.object(AnyAgentOrchestrator, "_execute_in_single_loop")
def test_execute_query_gemini_35(mock_execute: MagicMock) -> None:
    mock_execute.return_value = "Test response"

    result = AnyAgentOrchestrator.execute_query(
        model_id="gemini:gemini-3.5-flash",
        api_key="test-key",
        context="Context payload",
        prompt="User question",
        env_var="GEMINI_API_KEY",
    )

    assert result == "Test response"
    assert mock_execute.called
    kwargs = mock_execute.call_args.kwargs
    config: AgentConfig = kwargs["config"]
    formatted_prompt: str = kwargs["prompt"]

    assert config.model_id == "gemini:gemini-3.5-flash"
    assert config.instructions is not None
    assert "Context payload" in config.instructions
    assert formatted_prompt == "User question"


@pytest.mark.asyncio
async def test_run_direct_model_without_instructions() -> None:
    config = AgentConfig(
        model_id="gemini:gemini-3.6-flash",
        instructions=None,
    )
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "Direct answer"

    with patch("any_llm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = fake_response
        res = await AnyAgentOrchestrator._run_direct_model(config, "Combined prompt text")

        assert res == "Direct answer"
        mock_acompletion.assert_called_once()
        messages = mock_acompletion.call_args.kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Combined prompt text"


@pytest.mark.asyncio
async def test_run_direct_model_with_instructions() -> None:
    config = AgentConfig(
        model_id="gemini:gemini-3.5-flash",
        instructions="System instructions text",
    )
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "Direct answer"

    with patch("any_llm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = fake_response
        res = await AnyAgentOrchestrator._run_direct_model(config, "User prompt text")

        assert res == "Direct answer"
        mock_acompletion.assert_called_once()
        messages = mock_acompletion.call_args.kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "System instructions text"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "User prompt text"


def test_gemini_provider_patch_converts_function_role_to_user() -> None:
    import json

    import any_llm.providers.gemini.utils as gemini_utils

    test_messages = [
        {"role": "user", "content": "Query"},
        {"role": "tool", "name": "calc", "content": json.dumps({"result": 42})},
    ]

    formatted_messages, _ = gemini_utils._convert_messages(test_messages)
    for msg in formatted_messages:
        assert msg.role in ("user", "model"), f"Role {msg.role} is not supported by Gemini API!"
