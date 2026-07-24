"""OpenTelemetry Instrumentation and Performance Monitoring for Sqwakvox.

Provides distributed tracing, performance metrics collection, and diagnostic logging
to monitor and optimize document ingestion, agent reasoning, guardrail evaluations,
cross-validation, and MCP tool execution.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from opentelemetry import metrics, trace
from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode, Tracer

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

SERVICE_NAME = "sqwakvox"
SERVICE_VERSION = "0.1.2"


class JSONFileSpanExporter(SpanExporter):
    """Export OpenTelemetry spans as JSON Lines to a local file.

    Enables local performance analysis and trace inspection without requiring
    an external OTLP collector infrastructure.
    """

    def __init__(self, file_path: Path | str = "sqwakvox_telemetry.jsonl"):
        self.file_path = Path(file_path)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with self.file_path.open("a", encoding="utf-8") as f:
                for span in spans:
                    span_dict: dict[str, Any] = {
                        "name": span.name,
                        "trace_id": f"{span.context.trace_id:032x}",
                        "span_id": f"{span.context.span_id:016x}",
                        "parent_span_id": (
                            f"{span.parent.span_id:016x}" if span.parent else None
                        ),
                        "start_time_ns": span.start_time,
                        "end_time_ns": span.end_time,
                        "duration_ms": (
                            (span.end_time - span.start_time) / 1e6
                            if span.end_time and span.start_time
                            else 0.0
                        ),
                        "attributes": dict(span.attributes) if span.attributes else {},
                        "status": span.status.status_code.name,
                    }
                    f.write(json.dumps(span_dict) + "\n")
            return SpanExportResult.SUCCESS
        except Exception as exc:
            logger.warning("JSONFileSpanExporter failed to export spans: %s", exc)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass


@dataclass
class TelemetryManager:
    """Centralized manager for Sqwakvox OpenTelemetry tracers, meters, and metric instruments."""

    tracer: Tracer
    meter: Meter
    is_enabled: bool = True

    # Metric Instruments
    doc_ingest_counter: Counter | None = None
    doc_ingest_duration: Histogram | None = None
    doc_markdown_length: Histogram | None = None
    doc_tables_count: Histogram | None = None

    agent_execution_counter: Counter | None = None
    agent_execution_duration: Histogram | None = None
    agent_response_length: Histogram | None = None

    guardrail_violation_counter: Counter | None = None
    guardrail_duration: Histogram | None = None

    cross_validate_duration: Histogram | None = None

    mcp_tool_counter: Counter | None = None
    mcp_tool_duration: Histogram | None = None

    active_documents_counter: UpDownCounter | None = None


_telemetry_manager: TelemetryManager | None = None
_is_setup: bool = False


def setup_telemetry(
    service_name: str = SERVICE_NAME,
    service_version: str = SERVICE_VERSION,
    file_path: Path | str = "sqwakvox_telemetry.jsonl",
    force: bool = False,
) -> TelemetryManager:
    """Initialize OpenTelemetry tracer and meter providers for Sqwakvox.

    Reads environment variables:
      - SQWAKVOX_TELEMETRY_ENABLED (default: 'true')
      - SQWAKVOX_TELEMETRY_EXPORTER ('otlp', 'file', 'console', 'none')
      - OTEL_EXPORTER_OTLP_ENDPOINT (e.g., 'http://localhost:4318')
    """
    global _telemetry_manager, _is_setup

    if _is_setup and _telemetry_manager is not None and not force:
        return _telemetry_manager

    enabled_str = os.environ.get("SQWAKVOX_TELEMETRY_ENABLED", "true").lower()
    enabled = enabled_str in ("true", "1", "yes", "on")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "telemetry.sdk.language": "python",
        }
    )

    current_tp = trace.get_tracer_provider()
    if isinstance(current_tp, TracerProvider):
        tracer_provider = current_tp
    else:
        tracer_provider = TracerProvider(resource=resource)
        with suppress(Exception):
            trace.set_tracer_provider(tracer_provider)

    current_mp = metrics.get_meter_provider()
    if isinstance(current_mp, MeterProvider):
        meter_provider = current_mp
    else:
        meter_provider = MeterProvider(resource=resource)
        with suppress(Exception):
            metrics.set_meter_provider(meter_provider)

    if enabled:
        exporter_type = os.environ.get("SQWAKVOX_TELEMETRY_EXPORTER", "").lower()
        otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
        )

        if not exporter_type:
            exporter_type = "otlp" if otlp_endpoint else "file"

        # Configure Trace Exporters
        if exporter_type == "otlp":
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as OTLPHttpSpanExporter,
                )

                otlp_span_exporter = OTLPHttpSpanExporter(
                    endpoint=otlp_endpoint or "http://localhost:4318/v1/traces"
                )
                tracer_provider.add_span_processor(BatchSpanProcessor(otlp_span_exporter))
                logger.info("OTLP Trace Exporter initialized with endpoint: %s", otlp_endpoint)
            except Exception as exc:
                logger.warning(
                    "Failed to initialize OTLP exporter (%s), falling back to file exporter.", exc
                )
                exporter_type = "file"

        if exporter_type == "file":
            json_exporter = JSONFileSpanExporter(file_path=file_path)
            tracer_provider.add_span_processor(BatchSpanProcessor(json_exporter))
            logger.info("JSON File Trace Exporter initialized: %s", file_path)

        elif exporter_type == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            logger.info("Console Trace Exporter initialized.")

        # Configure Metrics Exporters
        try:
            if otlp_endpoint or exporter_type == "otlp":
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter as OTLPHttpMetricExporter,
                )

                metric_exporter = OTLPHttpMetricExporter(
                    endpoint=otlp_endpoint or "http://localhost:4318/v1/metrics"
                )
                reader = PeriodicExportingMetricReader(metric_exporter)
                meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        except Exception as exc:
            logger.debug("OTLP Metrics Exporter not initialized: %s", exc)

    trace.get_tracer_provider()
    metrics.get_meter_provider()

    tracer = trace.get_tracer(service_name, service_version)
    meter = metrics.get_meter(service_name, service_version)

    # Instantiate Metric Instruments
    doc_ingest_counter = meter.create_counter(
        "sqwakvox.document.ingest.count",
        description="Total number of document conversion requests",
        unit="1",
    )
    doc_ingest_duration = meter.create_histogram(
        "sqwakvox.document.ingest.duration",
        description="Duration of document conversion and parsing",
        unit="s",
    )
    doc_markdown_length = meter.create_histogram(
        "sqwakvox.document.markdown_length",
        description="Length of exported document markdown in characters",
        unit="chars",
    )
    doc_tables_count = meter.create_histogram(
        "sqwakvox.document.tables_count",
        description="Number of tables extracted per document",
        unit="tables",
    )

    agent_execution_counter = meter.create_counter(
        "sqwakvox.agent.execution.count",
        description="Total number of agent execution requests",
        unit="1",
    )
    agent_execution_duration = meter.create_histogram(
        "sqwakvox.agent.execution.duration",
        description="Total agent execution time from query to response",
        unit="s",
    )
    agent_response_length = meter.create_histogram(
        "sqwakvox.agent.response.length",
        description="Length of generated agent response in characters",
        unit="chars",
    )

    guardrail_violation_counter = meter.create_counter(
        "sqwakvox.guardrail.violations.count",
        description="Total guardrail checks triggered or failed",
        unit="1",
    )
    guardrail_duration = meter.create_histogram(
        "sqwakvox.guardrail.duration",
        description="Duration of guardrail validation checks",
        unit="s",
    )

    cross_validate_duration = meter.create_histogram(
        "sqwakvox.cross_validate.duration",
        description="Duration of financial cross-validation operations",
        unit="s",
    )

    mcp_tool_counter = meter.create_counter(
        "sqwakvox.mcp.tool.count",
        description="Total invocations of MCP tools",
        unit="1",
    )
    mcp_tool_duration = meter.create_histogram(
        "sqwakvox.mcp.tool.duration",
        description="Execution duration of MCP tool calls",
        unit="s",
    )

    active_documents_counter = meter.create_up_down_counter(
        "sqwakvox.active_documents.count",
        description="Number of currently loaded active documents",
        unit="1",
    )

    _telemetry_manager = TelemetryManager(
        tracer=tracer,
        meter=meter,
        is_enabled=enabled,
        doc_ingest_counter=doc_ingest_counter,
        doc_ingest_duration=doc_ingest_duration,
        doc_markdown_length=doc_markdown_length,
        doc_tables_count=doc_tables_count,
        agent_execution_counter=agent_execution_counter,
        agent_execution_duration=agent_execution_duration,
        agent_response_length=agent_response_length,
        guardrail_violation_counter=guardrail_violation_counter,
        guardrail_duration=guardrail_duration,
        cross_validate_duration=cross_validate_duration,
        mcp_tool_counter=mcp_tool_counter,
        mcp_tool_duration=mcp_tool_duration,
        active_documents_counter=active_documents_counter,
    )
    _is_setup = True
    return _telemetry_manager


def get_telemetry() -> TelemetryManager:
    """Get or auto-initialize the central TelemetryManager."""
    if _telemetry_manager is None:
        return setup_telemetry()
    return _telemetry_manager


def get_tracer(name: str = SERVICE_NAME) -> Tracer:
    """Get an OpenTelemetry Tracer instance."""
    return trace.get_tracer(name, SERVICE_VERSION)


def get_meter(name: str = SERVICE_NAME) -> Meter:
    """Get an OpenTelemetry Meter instance."""
    return metrics.get_meter(name, SERVICE_VERSION)


@contextmanager
def trace_span(
    span_name: str,
    attributes: dict[str, Any] | None = None,
) -> Generator[trace.Span, None, None]:
    """Context manager for creating a child span with trace/error recording."""
    tm = get_telemetry()
    with tm.tracer.start_as_current_span(span_name) as span:
        if attributes:
            for key, val in attributes.items():
                if val is not None:
                    attr_val = str(val) if not isinstance(val, (int, float, bool)) else val
                    span.set_attribute(key, attr_val)
        start = time.monotonic()
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            elapsed = time.monotonic() - start
            span.set_attribute("execution_duration_sec", elapsed)


def instrument_function(
    span_name: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator to automatically wrap a function with an OpenTelemetry trace span."""

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        name = span_name or f"{fn.__module__}.{fn.__qualname__}"

        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with trace_span(name):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


def shutdown_telemetry() -> None:
    """Flush pending spans and shutdown tracer providers gracefully on application exit."""
    global _telemetry_manager, _is_setup
    try:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception as exc:
        logger.debug("Error shutting down tracer provider: %s", exc)
    finally:
        _telemetry_manager = None
        _is_setup = False
