from unittest.mock import patch

from any_agent.config import MCPStdio

from sqwakvox.backend import tasks as tasks_mod
from sqwakvox.models import StructuredDocument, TableData


def test_build_financial_data_store_roundtrip() -> None:
    doc = StructuredDocument(
        file_name="t.pdf",
        raw_markdown="",
        tables=[TableData(headers=["Label", "Value"], rows=[["Revenue", "$1,000.50"]])],
    )

    result = tasks_mod.build_financial_data_store.apply(
        kwargs={"document_dump": doc.model_dump()}
    ).get()

    assert result == {"Revenue": "$1,000.50"}


def test_cross_validate_roundtrip() -> None:
    doc = StructuredDocument(
        file_name="t.pdf",
        raw_markdown="",
        tables=[TableData(headers=["Expenses"], rows=[["100"], ["150"], ["250"]])],
    )

    result = tasks_mod.cross_validate.apply(kwargs={"document_dump": doc.model_dump()}).get()

    assert result == [("Expenses", 250.0, 250.0, True)]


def test_execute_agent_rehydrates_mcp_servers() -> None:
    mcp_dump = MCPStdio(command="node", args=["calc-server.js"]).model_dump()

    with patch.object(tasks_mod, "_get_controller") as fake_controller:
        result = tasks_mod.execute_agent.apply(
            args=(
                "openai:gpt-4o-mini",
                "sk-test-key",
                "What is the revenue?",
                "Revenue: $1000",
                "fin.pdf",
                {},
                [mcp_dump],
            )
        )

    assert result.successful()
    _, kwargs = fake_controller.return_value.execute_agent.call_args
    rehydrated = kwargs["mcp_servers"]
    assert len(rehydrated) == 1
    assert isinstance(rehydrated[0], MCPStdio)
    assert rehydrated[0].command == "node"
    assert rehydrated[0].args == ["calc-server.js"]


def test_execute_agent_resolves_api_key_worker_side() -> None:
    """Gateway sends api_key=None; worker must resolve it from its env."""
    mcp_dump = MCPStdio(command="node", args=["calc-server.js"]).model_dump()

    with (
        patch.object(tasks_mod, "_get_controller") as fake_controller,
        patch.dict("os.environ", {"OPENAI_API_KEY": "worker-side-key"}, clear=False),
    ):
        result = tasks_mod.execute_agent.apply(
            args=(
                "openai:gpt-4o-mini",
                None,  # api_key slot as sent by the gateway
                "What is the revenue?",
                "Revenue: $1000",
                "fin.pdf",
                {},
                [mcp_dump],
            )
        )

    assert result.successful()
    _, kwargs = fake_controller.return_value.execute_agent.call_args
    assert kwargs["api_key"] == "worker-side-key"


def test_execute_agent_missing_key_returns_error() -> None:
    """When no key is provided and none is in env, return a clean error."""
    with (
        patch.object(tasks_mod, "_get_controller"),
        patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
            },
            clear=False,
        ),
    ):
        result = tasks_mod.execute_agent.apply(
            args=(
                "openai:gpt-4o-mini",
                None,
                "hi",
                "ctx",
                "doc.pdf",
                {},
                None,
            )
        )

    assert result.successful()
    assert result.result["success"] is False
    assert "API key" in result.result["error_message"]
