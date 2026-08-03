import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter

from sqwakvox.guardrails import (
    AnyGuardrailValidator,
    AuditLogger,
    FinancialRuleEngine,
    FinancialValue,
    PIIRedactor,
    detect_unit,
    parse_financial_value,
)
from sqwakvox.models import ModelProvider, StructuredDocument, TableData
from sqwakvox.telemetry import get_telemetry, trace_span

logger = logging.getLogger(__name__)


def extract_message(error_string: str) -> str:
    if not error_string:
        return error_string

    # Try parsing the entire string as JSON
    try:
        obj = json.loads(error_string)
        msg = _walk_for_message(obj)
        if msg:
            return msg
    except json.JSONDecodeError:
        pass

    # Try finding a JSON object embedded in the string (e.g. "... {'error': {'message': '...'}}")
    for m in re.finditer(r"\{.*\}", error_string, re.DOTALL):
        try:
            obj = json.loads(m.group())
            msg = _walk_for_message(obj)
            if msg:
                return msg
        except (json.JSONDecodeError, ValueError):
            continue

    # Fall back to the last line (often the most human-readable part)
    lines = [line.strip() for line in error_string.splitlines() if line.strip()]
    return lines[-1] if lines else error_string


def _walk_for_message(obj: object) -> str | None:
    if isinstance(obj, dict):
        if "message" in obj and isinstance(obj["message"], str):
            return obj["message"]
        for v in obj.values():
            result = _walk_for_message(v)
            if result:
                return result
    return None


@dataclass
class AgentResult:
    response: str = ""
    is_blocked: bool = False
    blocked_reason: str = ""
    pii_redacted_query: bool = False
    pii_redacted_response: bool = False
    math_discrepancies: list[str] | None = None
    success: bool = True
    error_message: str = ""

    def __post_init__(self) -> None:
        if self.math_discrepancies is None:
            self.math_discrepancies = []


class AppController:
    def __init__(self, converter: DocumentConverter | None = None):
        self.converter = converter or DocumentConverter()

    def convert_document(
        self, source: str, is_cancelled: Callable[[], bool]
    ) -> StructuredDocument | None:
        tm = get_telemetry()
        start_time = time.monotonic()
        with trace_span("sqwakvox.document.convert", {"source": source}) as span:
            logger.info(f"Running Docling layout converter on {source}...")
            try:
                result = self.converter.convert(source)
                if is_cancelled():
                    logger.info("Docling parsing worker was cancelled.")
                    span.set_attribute("cancelled", True)
                    if tm.doc_ingest_counter:
                        tm.doc_ingest_counter.add(1, {"status": "cancelled"})
                    return None
                logger.info("Docling layout conversion complete. Processing tables...")

                doc_md = result.document.export_to_markdown()
                doc_name = Path(source).name if "/" in source or "\\" in source else source

                tables: list[TableData] = []
                if hasattr(result.document, "tables") and result.document.tables:
                    for tbl in result.document.tables:
                        headers = []
                        rows = []
                        if hasattr(tbl, "export_to_dataframe"):
                            try:
                                df = tbl.export_to_dataframe()
                                headers = [str(col) for col in df.columns]
                                rows = [[str(val) for val in r] for r in df.values.tolist()]
                            except Exception:
                                pass

                        if not headers and not rows:
                            headers = (
                                [cell.text for cell in tbl.header_row]
                                if hasattr(tbl, "header_row") and tbl.header_row
                                else []
                            )
                            if hasattr(tbl, "rows") and tbl.rows:
                                rows = [[cell.text for cell in row] for row in tbl.rows]

                        caption = None
                        caption_text_attr = getattr(tbl, "caption_text", None)
                        if caption_text_attr and isinstance(caption_text_attr, str):
                            caption = caption_text_attr
                        else:
                            caption_attr = getattr(tbl, "caption", None)
                            if caption_attr:
                                if callable(caption_attr):
                                    try:
                                        res = caption_attr()
                                        if isinstance(res, str):
                                            caption = res
                                    except Exception:
                                        pass
                                elif isinstance(caption_attr, str):
                                    caption = caption_attr

                        if not caption and hasattr(tbl, "captions") and tbl.captions:
                            caption = " ".join(getattr(c, "text", "") for c in tbl.captions)

                        if caption is not None:
                            caption = str(caption).strip()
                            if not caption:
                                caption = None

                        tables.append(
                            TableData(
                                headers=headers,
                                rows=rows,
                                title=caption,
                            )
                        )

                doc = StructuredDocument(
                    file_name=doc_name,
                    raw_markdown=doc_md,
                    tables=tables,
                )

                duration = time.monotonic() - start_time
                span.set_attribute("file_name", doc_name)
                span.set_attribute("tables_count", len(tables))
                span.set_attribute("markdown_length", len(doc_md))
                span.set_attribute("duration_sec", duration)

                if tm.doc_ingest_counter:
                    tm.doc_ingest_counter.add(1, {"status": "success"})
                if tm.doc_ingest_duration:
                    tm.doc_ingest_duration.record(duration, {"status": "success"})
                if tm.doc_markdown_length:
                    tm.doc_markdown_length.record(len(doc_md))
                if tm.doc_tables_count:
                    tm.doc_tables_count.record(len(tables))

                return doc
            except Exception:
                duration = time.monotonic() - start_time
                if tm.doc_ingest_counter:
                    tm.doc_ingest_counter.add(1, {"status": "failure"})
                if tm.doc_ingest_duration:
                    tm.doc_ingest_duration.record(duration, {"status": "failure"})
                raise

    def build_financial_data_store(
        self, structured_doc: StructuredDocument | None
    ) -> dict[str, FinancialValue]:
        data_store: dict[str, FinancialValue] = {}
        if not structured_doc:
            return data_store
        for table in structured_doc.tables:
            col_unit = "number"
            if table.headers and len(table.headers) >= 2:
                col_unit = detect_unit(table.headers[1])

            for row in table.rows:
                if len(row) >= 2:
                    label = row[0].strip()
                    for cell in row[1:]:
                        fv = parse_financial_value(cell, default_unit=col_unit)
                        if fv is not None and label and len(label) > 1:
                            data_store[label] = fv
        return data_store

    def cross_validate(
        self, structured_doc: StructuredDocument | None
    ) -> list[tuple[str, float, float, bool]]:
        tm = get_telemetry()
        start_time = time.monotonic()
        doc_name = structured_doc.file_name if structured_doc else "none"
        with trace_span("sqwakvox.cross_validate", {"file_name": doc_name}) as span:
            results: list[tuple[str, float, float, bool]] = []
            if not structured_doc or not structured_doc.tables:
                return results

            for table in structured_doc.tables:
                for col_idx in range(len(table.headers)):
                    values: list[FinancialValue] = []
                    col_header = table.headers[col_idx] if col_idx < len(table.headers) else ""
                    col_unit = detect_unit(col_header)

                    for row in table.rows:
                        if col_idx < len(row):
                            cell = row[col_idx]
                            fv = parse_financial_value(cell, default_unit=col_unit)
                            if fv is not None:
                                values.append(fv)
                            else:
                                break

                    if values and len(values) >= 3:
                        expected = values[-1]
                        actual = values[:-1]
                        is_valid = FinancialRuleEngine.verify_column_sum(actual, expected)
                        col_name = (
                            table.headers[col_idx]
                            if col_idx < len(table.headers)
                            else f"Column {col_idx}"
                        )
                        expected_val = float(expected)
                        actual_sum = sum(float(v) for v in actual)
                        results.append((col_name, expected_val, actual_sum, is_valid))

            duration = time.monotonic() - start_time
            valid_count = sum(1 for r in results if r[3])
            invalid_count = len(results) - valid_count
            span.set_attribute("total_checks", len(results))
            span.set_attribute("valid_count", valid_count)
            span.set_attribute("invalid_count", invalid_count)

            if tm.cross_validate_duration:
                tm.cross_validate_duration.record(duration)

            return results

    def execute_agent(
        self,
        model_id: str,
        api_key: str,
        user_query: str,
        doc_context: str,
        active_document_name: str,
        data_store: Mapping[str, float | FinancialValue],
        mcp_servers: list[Any] | None = None,
        thread_id: str | None = None,
    ) -> AgentResult:
        tm = get_telemetry()
        start_time = time.monotonic()
        with trace_span(
            "sqwakvox.agent.execute",
            {
                "model_id": model_id,
                "document": active_document_name,
                "user_query_len": len(user_query),
                "doc_context_len": len(doc_context),
            },
        ) as span:
            result = AgentResult()

            # 1. Mozilla any-guardrail Input Prompt Verification
            is_query_safe = AnyGuardrailValidator.validate_prompt(user_query)
            if not is_query_safe:
                result.is_blocked = True
                result.blocked_reason = "Mozilla any-guardrail prompt safety violation"
                result.success = False
                span.set_attribute("is_blocked", True)
                span.set_attribute("status", "blocked")

                duration = time.monotonic() - start_time
                if tm.agent_execution_counter:
                    tm.agent_execution_counter.add(1, {"model_id": model_id, "status": "blocked"})
                if tm.agent_execution_duration:
                    tm.agent_execution_duration.record(
                        duration, {"model_id": model_id, "status": "blocked"}
                    )
                return result

            # 2. Local PII Redaction
            redacted_query = PIIRedactor.redact_text(user_query)
            if redacted_query != user_query:
                result.pii_redacted_query = True

            AuditLogger.log(
                document_id=active_document_name or "unknown",
                operation="user_query",
                guardrail_checks={
                    "any_guardrail_safe": True,
                    "pii_redacted": result.pii_redacted_query,
                },
                action="ALLOWED",
                input_text=user_query,
            )

            env_var = ModelProvider.get_env_var(model_id)

            try:
                from sqwakvox.agent import AnyAgentOrchestrator

                agent_response = AnyAgentOrchestrator.execute_query(
                    model_id=model_id,
                    api_key=api_key,
                    context=doc_context,
                    prompt=redacted_query,
                    env_var=env_var,
                    mcp_servers=mcp_servers,
                    thread_id=thread_id,
                )

                logger.info("Agent raw response received: %d chars", len(agent_response))

                # 3. Output PII Redaction
                agent_redacted = PIIRedactor.redact_text(agent_response)
                if agent_redacted != agent_response:
                    result.pii_redacted_response = True

                # 4. Math Guardrail Validation
                verification = FinancialRuleEngine.cross_check_text_assertions(
                    agent_redacted, data_store
                )

                if not verification.passed:
                    result.math_discrepancies = verification.discrepancies

                result.response = agent_redacted
                logger.info(
                    "Agent result prepared: success=%s, len=%d",
                    result.success,
                    len(result.response),
                )

                AuditLogger.log(
                    document_id=active_document_name or "unknown",
                    operation="agent_response",
                    action="ALLOWED",
                    input_text=user_query,
                )

            except Exception as e:
                logger.error("Agent execution exception: %s", e, exc_info=True)
                result.success = False
                result.error_message = str(e)
                AuditLogger.log(
                    document_id=active_document_name or "unknown",
                    operation="agent_response",
                    action="FAILURE",
                    risk_score=0.5,
                )

            duration = time.monotonic() - start_time
            status_str = (
                "blocked" if result.is_blocked else ("success" if result.success else "failure")
            )
            span.set_attribute("status", status_str)
            span.set_attribute("is_blocked", result.is_blocked)
            span.set_attribute("pii_redacted_query", result.pii_redacted_query)
            span.set_attribute("pii_redacted_response", result.pii_redacted_response)
            span.set_attribute("discrepancies_count", len(result.math_discrepancies or []))
            span.set_attribute("duration_sec", duration)

            if tm.agent_execution_counter:
                tm.agent_execution_counter.add(1, {"model_id": model_id, "status": status_str})
            if tm.agent_execution_duration:
                tm.agent_execution_duration.record(
                    duration, {"model_id": model_id, "status": status_str}
                )
            if result.success and tm.agent_response_length:
                tm.agent_response_length.record(len(result.response))

            return result
