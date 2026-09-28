from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

from sqwakvox.domains import get_domain
from sqwakvox.domains.base import GuardrailPipeline, InputGuardrailResult, OutputGuardrailResult
from sqwakvox.domains.financial import extract_data_store
from sqwakvox.guardrails import (
    AuditLogger,
    FinancialRuleEngine,
    FinancialValue,
    detect_unit,
    parse_financial_value,
)
from sqwakvox.models import ModelProvider, StructuredDocument, TableData
from sqwakvox.telemetry import get_telemetry, trace_span

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

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


def pdf_page_count(source: str) -> int | None:
    """Cheap total page count for a local PDF via pypdfium2 (no OCR).

    Used to drive incremental page-batch rendering: the TUI loads the first
    N pages immediately and shows a "Load more" control until every page has
    been fetched.  Returns ``None`` for non-PDF / remote sources where a quick
    count isn't available — the UI then falls back to detecting the end of the
    document when a fetched batch comes back empty.
    """

    if not source.lower().endswith(".pdf"):
        return None
    if not Path(source).is_file():
        return None
    try:
        with pdfium.PdfDocument(source) as pdf:
            return len(pdf)
    except Exception as exc:  # counting must never break parsing
        logger.warning("pdf_page_count failed for %s: %s", source, exc)
        return None


#: Number of leading pages sampled by :func:`pdf_has_text_layer`.  A handful is
#: enough: a PDF is either born digital (text on every page) or born scanned
#: (no text anywhere), so sampling a few pages distinguishes the two cheaply.
_TEXT_LAYER_PROBE_PAGES = 3


def pdf_has_text_layer(source: str) -> bool:
    """Return True when *source* is a local PDF with extractable text.

    A digital PDF already carries a text layer, so running Docling's OCR over
    it is redundant work — on a CPU-only machine OCR is roughly a third of the
    total conversion time.  A scan has no text layer and genuinely needs OCR.

    Deliberately conservative, because the cost of a wrong ``True`` is a
    garbage document while the cost of a wrong ``False`` is merely slow:

    * non-PDF, non-local, and unreadable sources return ``False`` (use OCR);
    * only the first :data:`_TEXT_LAYER_PROBE_PAGES` pages are read, and any
      one of them carrying text is enough to skip OCR.

    A malformed text layer (wrong encoding) is not detected here; it decodes to
    mojibake rather than raising.  Such a document is the one case where this
    shortcut loses accuracy, and it is a deliberate trade for the speedup.
    """
    if not source.lower().endswith(".pdf"):
        return False
    if not Path(source).is_file():
        return False
    try:
        with pdfium.PdfDocument(source) as pdf:
            for index in range(min(_TEXT_LAYER_PROBE_PAGES, len(pdf))):
                textpage = pdf[index].get_textpage()
                try:
                    if textpage.get_text_range().strip():
                        return True
                finally:
                    textpage.close()
    except Exception as exc:  # probing must never break parsing
        logger.warning("pdf_has_text_layer failed for %s: %s", source, exc)
        return False
    return False


def _unwrap_timeout_cause() -> SoftTimeLimitExceeded | None:
    """Inspect the current exception chain for a wrapped timeout.

    Docling's ``BasePipeline.execute`` (see
    ``docling/pipeline/base_pipeline.py``) catches
    :class:`billiard.exceptions.SoftTimeLimitExceeded` — raised by Celery's
    worker when the soft time limit fires while blocked inside the OCR
    thread — and rewraps it as a generic ``RuntimeError``.  This helper walks
    ``__cause__`` / ``__context__`` of the *currently* handled exception
    (called from inside an ``except Exception:`` block) and returns the
    ``SoftTimeLimitExceeded`` if one is found, so callers can re-raise the
    real cause instead of the opaque wrapper.
    """
    import sys

    exc = sys.exc_info()[1]
    while exc is not None:
        if isinstance(exc, SoftTimeLimitExceeded):
            return exc
        exc = exc.__cause__ or exc.__context__
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
    def __init__(self, converter: DocumentConverter | None = None) -> None:
        """Create a controller; the Docling converter is built lazily.

        ``DocumentConverter`` is heavyweight (it loads OCR/layout models and
        carries open file handles), so it is only constructed on the first
        call to :meth:`convert_document`.  Workers that only run agent,
        data-store, or cross-validation tasks therefore never pay the Docling
        init cost — per-document agent workers never touch it at all.
        """
        self._converter = converter
        self._no_ocr_converter: DocumentConverter | None = None
        # A converter handed in explicitly (tests, custom pipelines) is used
        # verbatim: honouring the OCR-skip toggle would silently swap it for a
        # real Docling instance and run the slow path the caller opted out of.
        self._converter_injected = converter is not None

    @property
    def converter(self) -> DocumentConverter:
        """The Docling converter, constructed on first use."""
        if self._converter is None:
            # Imported lazily so processes that never parse a document (the
            # per-tab agent workers) don't even load the docling package.
            from docling.document_converter import DocumentConverter

            self._converter = DocumentConverter()
        return self._converter

    @converter.setter
    def converter(self, value: DocumentConverter) -> None:
        """Allow injecting a converter (tests / custom pipelines)."""
        self._converter = value
        self._converter_injected = True

    @property
    def no_ocr_converter(self) -> DocumentConverter:
        """A :class:`DocumentConverter` with OCR disabled, built on first use.

        Used for PDFs that already carry a text layer, where OCR would only
        re-derive text Docling can read directly.  Table structure detection
        stays on — the financial cross-validation depends on the tables.  Built
        lazily and cached separately from :attr:`converter` so that OCR-based
        ingestion never pays to construct it, and vice versa.
        """
        if self._no_ocr_converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption

            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_ocr = False
            self._no_ocr_converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
            )
        return self._no_ocr_converter

    def convert_document(
        self,
        source: str,
        is_cancelled: Callable[[], bool],
        domain_id: str = "financial",
        page_range: tuple[int, int] | None = None,
    ) -> StructuredDocument | None:
        tm = get_telemetry()
        start_time = time.monotonic()
        with trace_span("sqwakvox.document.convert", {"source": source}) as span:
            logger.info(f"Running Docling layout converter on {source}...")
            try:
                convert_kwargs: dict[str, Any] = {}
                if page_range is not None:
                    # Docling only OCRs/lays-out the requested slice, so a
                    # 100+ page PDF can be loaded a few pages at a time instead
                    # of blocking on one monolithic conversion that blows the
                    # Celery time limit.
                    convert_kwargs["page_range"] = (int(page_range[0]), int(page_range[1]))

                # Skip OCR when the PDF already carries a text layer; OCR is
                # roughly a third of CPU-only conversion time and is pure
                # waste on a digital document.  Scans fall through to the
                # default converter, which OCRs as before.
                skip_ocr = pdf_has_text_layer(source) and not self._converter_injected
                converter = self.no_ocr_converter if skip_ocr else self.converter
                span.set_attribute("ocr_skipped", skip_ocr)
                if skip_ocr:
                    logger.info("PDF has a text layer; skipping OCR for %s", source)

                result = converter.convert(source, **convert_kwargs)
                if is_cancelled():
                    logger.info("Docling parsing worker was cancelled.")
                    span.set_attribute("cancelled", True)
                    if tm.doc_ingest_counter:
                        tm.doc_ingest_counter.add(1, {"status": "cancelled", "domain": domain_id})
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

                total_pages = pdf_page_count(source)
                if page_range is not None:
                    start, end = convert_kwargs["page_range"]
                    # ``result.pages`` holds exactly the converted slice, so its
                    # length is the authoritative page count for this batch.
                    pages_in_batch = (
                        len(result.pages)
                        if hasattr(result, "pages") and result.pages is not None
                        else max(0, min(end, total_pages or end) - start + 1)
                    )
                else:
                    pages_in_batch = total_pages

                doc = StructuredDocument(
                    file_name=doc_name,
                    raw_markdown=doc_md,
                    tables=tables,
                    metadata={
                        "domain_id": domain_id,
                        "page_range": list(convert_kwargs["page_range"])
                        if page_range is not None
                        else None,
                        "total_pages": total_pages,
                        "pages_in_batch": pages_in_batch,
                    },
                )

                duration = time.monotonic() - start_time
                span.set_attribute("file_name", doc_name)
                span.set_attribute("tables_count", len(tables))
                span.set_attribute("markdown_length", len(doc_md))
                span.set_attribute("duration_sec", duration)
                span.set_attribute("domain_id", domain_id)

                if tm.doc_ingest_counter:
                    tm.doc_ingest_counter.add(1, {"status": "success", "domain": domain_id})
                if tm.doc_ingest_duration:
                    tm.doc_ingest_duration.record(
                        duration, {"status": "success", "domain": domain_id}
                    )
                if tm.doc_markdown_length:
                    tm.doc_markdown_length.record(len(doc_md))
                if tm.doc_tables_count:
                    tm.doc_tables_count.record(len(tables))

                return doc
            except Exception:
                # Docling's base_pipeline catches SoftTimeLimitExceeded
                # (raised by billiard when the Celery soft time limit fires
                # while blocked in the OCR thread) and rewraps it as a
                # generic RuntimeError("Pipeline ... failed").  Unwrap to
                # surface the real timeout cause so the presenter and TUI
                # can report it accurately instead of a misleading failure.
                timeout_exc = _unwrap_timeout_cause()
                if timeout_exc is not None:
                    logger.warning(
                        "Docling pipeline timed out after %.0fs on %s",
                        time.monotonic() - start_time,
                        source,
                    )
                    # Re-raise the real cause; suppress the docling wrapper's
                    # context so the reported traceback isn't polluted by the
                    # generic RuntimeError("Pipeline ... failed").
                    raise timeout_exc from None
                duration = time.monotonic() - start_time
                if tm.doc_ingest_counter:
                    tm.doc_ingest_counter.add(1, {"status": "failure", "domain": domain_id})
                if tm.doc_ingest_duration:
                    tm.doc_ingest_duration.record(
                        duration, {"status": "failure", "domain": domain_id}
                    )
                raise

    def convert_html_string(
        self,
        content: str,
        name: str,
        is_cancelled: Callable[[], bool],
    ) -> str:
        """Convert an HTML string (e.g. an EPUB chapter) to markdown.

        Used by domains that assemble multi-part sources (EPUB chapters,
        crawled docs pages) inside the shared docling worker.
        """
        from docling.datamodel.base_models import InputFormat

        with trace_span("sqwakvox.document.convert_html", {"name": name}):
            result = self.converter.convert_string(content, format=InputFormat.HTML, name=name)
            if is_cancelled():
                logger.info("Docling HTML conversion was cancelled.")
                return ""
            return result.document.export_to_markdown()

    def build_financial_data_store(
        self, structured_doc: StructuredDocument | None
    ) -> dict[str, FinancialValue]:
        """Financial data store of parsed table values.

        Delegates to the financial domain's ``extract_data_store`` (the
        single implementation of the table-extraction loop) instead of
        keeping a private copy; the broker-safe string variant lives in the
        domain's post-parse step (``financial._postprocess``).
        """
        if structured_doc is None:
            return {}
        return extract_data_store(structured_doc)

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
        domain_id: str = "financial",
    ) -> AgentResult:
        """Run the agent for *domain_id*'s guardrail pipeline and prompts."""
        domain = get_domain(domain_id)
        pipeline: GuardrailPipeline = (
            domain.guardrail_pipeline() if domain.guardrail_pipeline else GuardrailPipeline()
        )
        tm = get_telemetry()
        start_time = time.monotonic()
        with trace_span(
            "sqwakvox.agent.execute",
            {
                "model_id": model_id,
                "document": active_document_name,
                "user_query_len": len(user_query),
                "doc_context_len": len(doc_context),
                "domain_id": domain_id,
            },
        ) as span:
            result = AgentResult()

            # 1. Domain input guardrails (prompt safety + PII redaction).
            input_res: InputGuardrailResult = (
                pipeline.validate_input(user_query)
                if pipeline.validate_input
                else InputGuardrailResult()
            )
            if not input_res.safe:
                result.is_blocked = True
                result.blocked_reason = (
                    input_res.blocked_reason or "Input guardrail blocked the query"
                )
                result.success = False
                span.set_attribute("is_blocked", True)
                span.set_attribute("status", "blocked")

                duration = time.monotonic() - start_time
                if tm.agent_execution_counter:
                    tm.agent_execution_counter.add(
                        1, {"model_id": model_id, "status": "blocked", "domain": domain_id}
                    )
                if tm.agent_execution_duration:
                    tm.agent_execution_duration.record(
                        duration, {"model_id": model_id, "status": "blocked", "domain": domain_id}
                    )
                return result

            redacted_query = input_res.text if input_res.text is not None else user_query
            if redacted_query != user_query:
                result.pii_redacted_query = True

            AuditLogger.log(
                document_id=active_document_name or "unknown",
                operation="user_query",
                guardrail_checks={
                    "any_guardrail_safe": True,
                    "pii_redacted": result.pii_redacted_query,
                    "domain_id": domain_id,
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
                    domain_id=domain_id,
                )

                logger.info("Agent raw response received: %d chars", len(agent_response))

                # 2. Domain output guardrails (PII redaction, math checks,
                #    secret redaction — depends on the domain's pipeline).
                output_res: OutputGuardrailResult = (
                    pipeline.validate_output(agent_response, dict(data_store))
                    if pipeline.validate_output
                    else OutputGuardrailResult()
                )
                final_text = output_res.text if output_res.text is not None else agent_response
                if final_text != agent_response:
                    result.pii_redacted_response = True
                result.math_discrepancies = output_res.warnings

                result.response = final_text
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
                tm.agent_execution_counter.add(
                    1, {"model_id": model_id, "status": status_str, "domain": domain_id}
                )
            if tm.agent_execution_duration:
                tm.agent_execution_duration.record(
                    duration, {"model_id": model_id, "status": status_str, "domain": domain_id}
                )
            if result.success and tm.agent_response_length:
                tm.agent_response_length.record(len(result.response))

            return result
