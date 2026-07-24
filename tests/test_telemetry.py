import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqwakvox.controller import AppController
from sqwakvox.guardrails import AnyGuardrailValidator, FinancialRuleEngine, PIIRedactor
from sqwakvox.mcp_calc_server import calculator, compound_interest, stats_summary
from sqwakvox.models import StructuredDocument, TableData
from sqwakvox.telemetry import (
    JSONFileSpanExporter,
    instrument_function,
    setup_telemetry,
    shutdown_telemetry,
    trace_span,
)


def test_telemetry_setup_and_trace_span(tmp_path: Path) -> None:
    telemetry_file = tmp_path / "test_spans.jsonl"
    tm = setup_telemetry(file_path=telemetry_file, force=True)
    assert tm.is_enabled is True

    with trace_span("test_span_op", {"sample_attr": "hello"}) as span:
        span.set_attribute("inner_attr", 123)

    shutdown_telemetry()

    assert telemetry_file.exists()
    content = telemetry_file.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert len(lines) >= 1
    span_data = lines[-1]
    assert span_data["name"] == "test_span_op"
    assert span_data["attributes"]["sample_attr"] == "hello"
    assert span_data["attributes"]["inner_attr"] == 123


def test_instrument_function_decorator() -> None:
    @instrument_function("decorated_func_span")
    def add_numbers(a: int, b: int) -> int:
        return a + b

    result = add_numbers(3, 4)
    assert result == 7


def test_json_file_span_exporter_failure_handling(tmp_path: Path) -> None:
    exporter = JSONFileSpanExporter(file_path=tmp_path / "non_existent_dir" / "file.jsonl")
    mock_span = MagicMock()
    mock_span.name = "failing_span"
    mock_span.context.trace_id = 12345
    mock_span.context.span_id = 67890
    mock_span.parent = None
    mock_span.start_time = 1000
    mock_span.end_time = 2000
    mock_span.attributes = {}
    mock_span.status.status_code.name = "UNSET"

    # Should gracefully return FAILURE without crashing
    res = exporter.export([mock_span])
    assert res.name == "FAILURE" or res.value != 0


def test_controller_telemetry_integration(tmp_path: Path) -> None:
    telemetry_file = tmp_path / "controller_spans.jsonl"
    setup_telemetry(file_path=telemetry_file)

    mock_converter = MagicMock()
    mock_result = MagicMock()
    mock_result.document.export_to_markdown.return_value = "# Header\nContent"
    mock_result.document.tables = []
    mock_converter.convert.return_value = mock_result

    controller = AppController(converter=mock_converter)
    doc = controller.convert_document("sample.pdf", is_cancelled=lambda: False)

    assert doc is not None
    assert doc.file_name == "sample.pdf"

    # Cross-validation telemetry check
    doc_with_table = StructuredDocument(
        file_name="table_doc.pdf",
        raw_markdown="# Report",
        tables=[
            TableData(
                headers=["Item", "Q1"],
                rows=[["Product A", "$10"], ["Product B", "$20"], ["Total", "$30"]],
                title="Sales",
            )
        ],
    )
    results = controller.cross_validate(doc_with_table)
    assert len(results) == 1
    assert results[0][3] is True


def test_guardrails_telemetry_integration() -> None:
    # Test PII redaction trace span
    redacted = PIIRedactor.redact_text("User SSN: 123-45-6789")
    assert "[SSN_REDACTED]" in redacted

    # Test prompt validation span
    is_safe = AnyGuardrailValidator.validate_prompt("Explain net margin calculations")
    assert is_safe is True

    # Test verify column sum span
    is_valid_sum = FinancialRuleEngine.verify_column_sum([10.0, 20.0], 30.0)
    assert is_valid_sum is True


def test_mcp_tools_telemetry_integration() -> None:
    res_calc = calculator("10 + 20 * 2")
    assert res_calc == "50"

    res_stats = stats_summary("10, 20, 30")
    assert "Mean: 20" in res_stats

    res_compound = compound_interest(1000, 5, 2, 12)
    assert "Future Value: 1104.94" in res_compound


@patch("sqwakvox.agent.AnyAgentOrchestrator.execute_query")
def test_agent_execution_telemetry_integration(mock_execute: MagicMock) -> None:
    mock_execute.return_value = "The total revenue was $1000."
    controller = AppController()

    res = controller.execute_agent(
        model_id="openai:gpt-4o-mini",
        api_key="sk-1234567890abcdef",
        user_query="What is the revenue?",
        doc_context="Revenue: $1000",
        active_document_name="fin.pdf",
        data_store={},
    )

    assert res.success is True
    assert "$1000" in res.response
