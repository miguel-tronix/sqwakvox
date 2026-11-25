import gzip
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.trace import Status, StatusCode

from sqwakvox.controller import AppController
from sqwakvox.guardrails import AnyGuardrailValidator, AuditLogger, FinancialRuleEngine, PIIRedactor
from sqwakvox.mcp_calc_server import calculator, compound_interest, stats_summary
from sqwakvox.models import StructuredDocument, TableData
from sqwakvox.telemetry import (
    GCSJSONLSpanExporter,
    JSONFileSpanExporter,
    flush_telemetry,
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


def _mock_span() -> MagicMock:
    span = MagicMock()
    span.name = "gcs_span"
    span.context.trace_id = 12345
    span.context.span_id = 67890
    span.parent = None
    span.start_time = 1_700_000_000_000_000_000
    span.end_time = 1_700_000_000_001_000_000
    span.attributes = {"key": "value"}
    span.status.status_code.name = "OK"
    span.status.description = None
    span.events = []
    span.resource.attributes = {"service.name": "sqwakvox"}
    return span


def _install_fake_gcs(monkeypatch: pytest.MonkeyPatch, client_cls: type[Any]) -> None:
    fake_storage = types.ModuleType("google.cloud.storage")
    fake_storage.Client = client_cls  # type: ignore[attr-defined]
    fake_google = types.ModuleType("google")
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.storage = fake_storage  # type: ignore[attr-defined]
    fake_google.cloud = fake_cloud  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)


def test_gcs_jsonl_span_exporter_uploads_compressed_partitioned_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[dict[str, Any]] = []
    closed: list[bool] = []

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def upload_from_string(
            self,
            data: bytes | str,
            content_type: str | None = None,
            if_generation_match: int | None = None,
            timeout: float | None = None,
        ) -> None:
            uploads.append(
                {
                    "name": self.name,
                    "data": data,
                    "content_type": content_type,
                    "if_generation_match": if_generation_match,
                    "timeout": timeout,
                }
            )

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            return FakeBlob(name)

    class FakeClient:
        def bucket(self, name: str) -> FakeBucket:
            assert name == "test-bucket"
            return FakeBucket()

        def close(self) -> None:
            closed.append(True)

    _install_fake_gcs(monkeypatch, FakeClient)

    exporter = GCSJSONLSpanExporter(
        bucket="test-bucket", prefix="/telemetry/spans/", compress=True, timeout=8.0
    )
    # Empty batch is a no-op and creates no empty object.
    assert exporter.export([]).name == "SUCCESS"
    assert uploads == []

    res = exporter.export([_mock_span()])

    assert res.name == "SUCCESS"
    assert len(uploads) == 1
    up = uploads[0]
    assert up["name"].startswith("telemetry/spans/dt=")
    assert up["name"].endswith(".jsonl.gz")
    assert up["content_type"] == "application/gzip"
    assert up["if_generation_match"] == 0
    assert up["timeout"] == 8.0
    assert isinstance(up["data"], bytes)
    payload = gzip.decompress(up["data"]).decode("utf-8")
    lines = [json.loads(line) for line in payload.splitlines() if line.strip()]
    assert lines[0]["name"] == "gcs_span"
    assert lines[0]["attributes"]["key"] == "value"
    assert lines[0]["start_time"] is not None
    assert lines[0]["resource"]["service.name"] == "sqwakvox"

    exporter.shutdown()
    assert closed == [True]


def test_gcs_jsonl_span_exporter_uncompressed_and_empty_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[dict[str, Any]] = []

    class FakeBlob:
        def __init__(self, name: str) -> None:
            self.name = name

        def upload_from_string(self, data: bytes | str, **kwargs: Any) -> None:
            uploads.append({"name": self.name, "data": data, **kwargs})

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            return FakeBlob(name)

    class FakeClient:
        def bucket(self, name: str) -> FakeBucket:
            return FakeBucket()

    _install_fake_gcs(monkeypatch, FakeClient)

    exporter = GCSJSONLSpanExporter(bucket="test-bucket", prefix="", compress=False)
    res = exporter.export([_mock_span()])

    assert res.name == "SUCCESS"
    assert len(uploads) == 1
    assert uploads[0]["name"].startswith("dt=")
    assert not uploads[0]["name"].startswith("/")
    assert uploads[0]["name"].endswith(".jsonl")
    assert uploads[0]["content_type"] == "application/x-ndjson"
    assert isinstance(uploads[0]["data"], str)


def test_gcs_jsonl_span_exporter_recreates_client_after_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_pids: list[int] = []
    current_pid = 1000

    class FakeBlob:
        def upload_from_string(self, data: bytes | str, **kwargs: Any) -> None:
            pass

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            return FakeBlob()

    class FakeClient:
        def __init__(self) -> None:
            created_pids.append(current_pid)

        def bucket(self, name: str) -> FakeBucket:
            return FakeBucket()

    _install_fake_gcs(monkeypatch, FakeClient)
    monkeypatch.setattr("sqwakvox.telemetry.os.getpid", lambda: current_pid)

    exporter = GCSJSONLSpanExporter(bucket="test-bucket")
    assert created_pids == [1000]

    # Same PID reuses client.
    exporter.export([_mock_span()])
    assert created_pids == [1000]

    # Simulated fork changes PID -> fresh client created in child.
    current_pid = 2000
    exporter.export([_mock_span()])
    assert created_pids == [1000, 2000]


def test_gcs_jsonl_span_exporter_failure_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenBucket:
        def blob(self, name: str):
            raise RuntimeError("boom")

    class BrokenClient:
        def bucket(self, name: str):
            return BrokenBucket()

    _install_fake_gcs(monkeypatch, BrokenClient)

    exporter = GCSJSONLSpanExporter(bucket="test-bucket")
    res = exporter.export([_mock_span()])
    assert res.name == "FAILURE"


def test_setup_telemetry_prefers_gcs_when_bucket_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}

    class FakeGCSExporter:
        def __init__(
            self,
            bucket: str,
            prefix: str = "telemetry/spans",
            *,
            compress: bool = True,
            timeout: float = 10.0,
        ) -> None:
            created["bucket"] = bucket
            created["prefix"] = prefix
            created["compress"] = compress
            created["timeout"] = timeout

        def export(self, spans: Any) -> Any:
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr("sqwakvox.telemetry.GCSJSONLSpanExporter", FakeGCSExporter)
    monkeypatch.setenv("SQWAKVOX_TELEMETRY_BUCKET", "env-bucket")
    monkeypatch.setenv("SQWAKVOX_TELEMETRY_PREFIX", "custom/prefix")
    monkeypatch.setenv("SQWAKVOX_TELEMETRY_GCS_COMPRESS", "false")
    monkeypatch.setenv("SQWAKVOX_TELEMETRY_GCS_TIMEOUT", "7.5")
    monkeypatch.delenv("SQWAKVOX_TELEMETRY_EXPORTER", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)

    tm = setup_telemetry(file_path=tmp_path / "unused.jsonl", force=True)
    assert tm.is_enabled is True
    assert tm.exporter_name == "gcs"
    assert created == {
        "bucket": "env-bucket",
        "prefix": "custom/prefix",
        "compress": False,
        "timeout": 7.5,
    }
    shutdown_telemetry()


def test_span_serialization_preserves_exceptions_error_status_and_audit_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    telemetry_file = tmp_path / "rich_spans.jsonl"
    monkeypatch.delenv("SQWAKVOX_TELEMETRY_BUCKET", raising=False)
    monkeypatch.delenv("SQWAKVOX_TELEMETRY_EXPORTER", raising=False)
    monkeypatch.setattr(AuditLogger, "LOG_PATH", tmp_path / "audit.jsonl")

    setup_telemetry(file_path=telemetry_file, force=True, role="test-role")

    with pytest.raises(ValueError, match="bad input"), trace_span("failing_op"):
        raise ValueError("bad input")

    # Explicit ERROR status inside a non-raising span must not be overwritten by OK.
    with trace_span("soft_error_op") as span:
        span.set_status(Status(StatusCode.ERROR, "handled failure"))
        AuditLogger.log(
            document_id="doc.pdf",
            operation="user_query",
            action="ALLOWED",
            input_text="revenue?",
        )

    flush_telemetry()
    shutdown_telemetry()

    lines = [
        json.loads(line)
        for line in telemetry_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_name = {entry["name"]: entry for entry in lines}

    failing = by_name["failing_op"]
    assert failing["status"] == "ERROR"
    assert "bad input" in failing["status_description"]
    assert any(ev["name"] == "exception" for ev in failing.get("events", []))
    assert failing["start_time"] is not None

    soft = by_name["soft_error_op"]
    assert soft["status"] == "ERROR"
    assert soft["status_description"] == "handled failure"
    audit_events = [ev for ev in soft.get("events", []) if ev["name"] == "sqwakvox.audit"]
    assert len(audit_events) == 1
    assert audit_events[0]["attributes"]["audit.document_id"] == "doc.pdf"
    assert audit_events[0]["attributes"]["audit.operation"] == "user_query"


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


def test_filtering_span_processor_drops_fastmcp_and_polling_noise_but_keeps_errors() -> None:
    from sqwakvox.telemetry import _FilteringSpanProcessor

    forwarded: list[Any] = []

    class RecordingProcessor:
        def on_start(self, span: Any, parent_context: Any = None) -> None:
            pass

        def on_end(self, span: Any) -> None:
            forwarded.append(span.name)

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    def _mk(
        name: str,
        *,
        scope: str = "sqwakvox",
        attrs: dict[str, Any] | None = None,
        status: StatusCode = StatusCode.OK,
    ) -> MagicMock:
        sp = MagicMock()
        sp.name = name
        sp.instrumentation_scope.name = scope
        sp.attributes = attrs or {}
        sp.status.status_code = status
        return sp

    proc = _FilteringSpanProcessor(RecordingProcessor(), filter_noise=True)  # type: ignore[arg-type]

    # Dropped noise: FastMCP internal spans and successful polling presenter calls.
    proc.on_end(_mk("tools/list", scope="fastmcp"))
    proc.on_end(_mk("tools/call sqwakvox_presenter_events", scope="fastmcp"))
    proc.on_end(
        _mk("sqwakvox.mcp.presenter", attrs={"tool": "sqwakvox_presenter_events"})
    )
    proc.on_end(
        _mk("sqwakvox.mcp.presenter", attrs={"tool": "sqwakvox_presenter_status"})
    )

    # Kept signal: real work AND any ERROR spans (even from fastmcp or polling).
    proc.on_end(_mk("sqwakvox.document.convert"))
    proc.on_end(
        _mk("sqwakvox.mcp.presenter", attrs={"tool": "sqwakvox_presenter_seek_page"})
    )
    proc.on_end(_mk("tools/call broken", scope="fastmcp", status=StatusCode.ERROR))
    proc.on_end(
        _mk(
            "sqwakvox.mcp.presenter",
            attrs={"tool": "sqwakvox_presenter_events"},
            status=StatusCode.ERROR,
        )
    )

    assert forwarded == [
        "sqwakvox.document.convert",
        "sqwakvox.mcp.presenter",
        "tools/call broken",
        "sqwakvox.mcp.presenter",
    ]


def test_mcp_presenter_trace_marks_ok_false_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqwakvox import mcp_presenter

    telemetry_file = tmp_path / "presenter_err.jsonl"
    monkeypatch.delenv("SQWAKVOX_TELEMETRY_BUCKET", raising=False)
    monkeypatch.delenv("SQWAKVOX_TELEMETRY_EXPORTER", raising=False)
    setup_telemetry(file_path=telemetry_file, force=True)

    out = mcp_presenter._trace(
        "sqwakvox_presenter_seek_page",
        lambda: json.dumps({"ok": False, "error": "Timed out seeking to the page."}),
    )
    assert json.loads(out)["ok"] is False

    flush_telemetry()
    shutdown_telemetry()

    lines = [
        json.loads(line)
        for line in telemetry_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seek_spans = [
        s
        for s in lines
        if s["name"] == "sqwakvox.mcp.presenter"
        and s["attributes"].get("tool") == "sqwakvox_presenter_seek_page"
    ]
    assert seek_spans
    assert seek_spans[-1]["status"] == "ERROR"
    assert "Timed out" in seek_spans[-1]["status_description"]
