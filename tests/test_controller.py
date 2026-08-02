import json
from unittest.mock import MagicMock, patch

from sqwakvox.controller import AppController, extract_message
from sqwakvox.models import StructuredDocument, TableData


def test_extract_message_plain_text() -> None:
    assert extract_message("Simple error") == "Simple error"


def test_extract_message_json() -> None:
    error_json = json.dumps({"error": {"message": "Invalid API key"}})
    assert extract_message(error_json) == "Invalid API key"


def test_extract_message_embedded_json() -> None:
    error_str = 'Exception: failed with response {"error": {"message": "Rate limit exceeded"}}'
    assert extract_message(error_str) == "Rate limit exceeded"


def test_extract_message_multiline() -> None:
    error_str = (
        "Traceback (most recent call last):\n"
        "  File 'xyz'\n"
        "RuntimeError: Something went wrong"
    )
    assert extract_message(error_str) == "RuntimeError: Something went wrong"


def test_app_controller_build_financial_data_store() -> None:
    controller = AppController()

    doc = StructuredDocument(
        file_name="test.md",
        raw_markdown="",
        tables=[
            TableData(
                headers=["Label", "Value"],
                rows=[
                    ["Revenue", "$1,000.50"],
                    ["Growth", "15%"],
                    ["Invalid", "N/A"]
                ],
                title="Financials"
            )
        ]
    )

    data_store = controller.build_financial_data_store(doc)

    assert "Revenue" in data_store
    assert data_store["Revenue"] == 1000.50
    assert "Growth" in data_store
    assert data_store["Growth"] == 0.15
    assert "Invalid" not in data_store


def test_app_controller_cross_validate() -> None:
    controller = AppController()

    doc = StructuredDocument(
        file_name="test.md",
        raw_markdown="",
        tables=[
            TableData(
                headers=["Q1", "Q2", "Q3", "Total"],
                rows=[
                    ["$100", "$200", "$300", "$600"],
                ],
                title="Financials"
            )
        ]
    )

    # Mock FinancialRuleEngine.verify_column_sum
    with patch(
        "sqwakvox.controller.FinancialRuleEngine.verify_column_sum", return_value=True
    ) as mock_verify:
        results = controller.cross_validate(doc)

        # We process by column, not row.
        # Wait, the table in the doc for the actual test might have rows of data for columns.
        # The loop in cross_validate processes columns from rows. Let's see...
        # In the app, it iterates: for col_idx in range(len(table.headers)) ...
        # For col 0: rows are ["$100"], values = [100.0] - len < 3, so not evaluated.
        # This is fine for testing the logic, we should probably pass a table that works for it.
        pass

    # Let's write a better table that has at least 3 values per column
    doc_cols = StructuredDocument(
        file_name="test.md",
        raw_markdown="",
        tables=[
            TableData(
                headers=["Expenses"],
                rows=[
                    ["100"],
                    ["150"],
                    ["250"], # expected
                ]
            )
        ]
    )

    with patch(
        "sqwakvox.controller.FinancialRuleEngine.verify_column_sum", return_value=True
    ) as mock_verify:
        results = controller.cross_validate(doc_cols)

        assert len(results) == 1
        col_name, expected, actual, is_valid = results[0]
        assert col_name == "Expenses"
        assert expected == 250.0
        assert actual == 250.0
        assert is_valid is True
        mock_verify.assert_called_once_with([100.0, 150.0], 250.0)


@patch("sqwakvox.controller.AnyGuardrailValidator")
@patch("sqwakvox.controller.PIIRedactor")
@patch("sqwakvox.controller.AuditLogger")
@patch("sqwakvox.agent.AnyAgentOrchestrator")
@patch("sqwakvox.controller.FinancialRuleEngine")
def test_app_controller_execute_agent_success(
    mock_engine: MagicMock,
    mock_orchestrator: MagicMock,
    mock_audit: MagicMock,
    mock_redactor: MagicMock,
    mock_guardrail: MagicMock,
) -> None:
    controller = AppController()

    # Set up mocks
    mock_guardrail.validate_prompt.return_value = True
    mock_redactor.redact_text.side_effect = lambda x: x  # no redaction
    mock_orchestrator.execute_query.return_value = "Agent response"

    mock_verification = MagicMock()
    mock_verification.passed = True
    mock_engine.cross_check_text_assertions.return_value = mock_verification

    result = controller.execute_agent(
        model_id="test-model",
        api_key="sk-test",
        user_query="Hello",
        doc_context="Context",
        active_document_name="doc.md",
        data_store={}
    )

    assert result.success is True
    assert result.is_blocked is False
    assert result.response == "Agent response"
    assert result.math_discrepancies == []

    mock_orchestrator.execute_query.assert_called_once()
    mock_audit.log.assert_called()


@patch("sqwakvox.controller.AnyGuardrailValidator")
@patch("sqwakvox.controller.PIIRedactor")
@patch("sqwakvox.controller.AuditLogger")
@patch("sqwakvox.agent.AnyAgentOrchestrator")
@patch("sqwakvox.controller.FinancialRuleEngine")
def test_app_controller_execute_agent_blocked(
    mock_engine: MagicMock,
    mock_orchestrator: MagicMock,
    _mock_audit: MagicMock,
    mock_redactor: MagicMock,
    mock_guardrail: MagicMock,
) -> None:
    controller = AppController()
    mock_guardrail.validate_prompt.return_value = False
    mock_redactor.redact_text.side_effect = lambda x: x
    mock_orchestrator.execute_query.return_value = "Agent response"

    mock_verification = MagicMock()
    mock_verification.passed = True
    mock_engine.cross_check_text_assertions.return_value = mock_verification

    result = controller.execute_agent(
        model_id="test-model",
        api_key="sk-test",
        user_query="Evil query",
        doc_context="Context",
        active_document_name="doc.md",
        data_store={}
    )

    assert result.is_blocked is True
    assert result.success is False


@patch("sqwakvox.controller.AnyGuardrailValidator")
@patch("sqwakvox.controller.PIIRedactor")
@patch("sqwakvox.controller.AuditLogger")
@patch("sqwakvox.agent.AnyAgentOrchestrator")
@patch("sqwakvox.controller.FinancialRuleEngine")
def test_app_controller_execute_agent_with_mcp(
    mock_engine: MagicMock,
    mock_orchestrator: MagicMock,
    _mock_audit: MagicMock,
    mock_redactor: MagicMock,
    mock_guardrail: MagicMock,
) -> None:
    controller = AppController()
    mock_guardrail.validate_prompt.return_value = True
    mock_redactor.redact_text.side_effect = lambda x: x
    mock_orchestrator.execute_query.return_value = "Agent response"

    mock_verification = MagicMock()
    mock_verification.passed = True
    mock_engine.cross_check_text_assertions.return_value = mock_verification

    mcp_servers = ["dummy_mcp_config"]

    result = controller.execute_agent(
        model_id="test-model",
        api_key="sk-test",
        user_query="Hello",
        doc_context="Context",
        active_document_name="doc.md",
        data_store={},
        mcp_servers=mcp_servers
    )

    assert result.success is True
    mock_orchestrator.execute_query.assert_called_once_with(
        model_id="test-model",
        api_key="sk-test",
        context="Context",
        prompt="Hello",
        env_var="OPENAI_API_KEY",
        mcp_servers=mcp_servers
    )

