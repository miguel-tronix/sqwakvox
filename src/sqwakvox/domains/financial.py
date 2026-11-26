"""Financial document domain — the original Sqwakvox assistant behaviour.

Moves the hard-coded financial prompts, guardrails, post-parse processing,
and rendering out of the core so they live behind the
:class:`~sqwakvox.domains.base.DocumentDomain` profile.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from jinja2 import Template

from sqwakvox import guardrails as gr
from sqwakvox.domains.base import (
    DocumentDomain,
    GuardrailPipeline,
    OutputGuardrailResult,
    standard_validate_input,
)
from sqwakvox.guardrails import extract_data_store as extract_data_store
from sqwakvox.models import StructuredDocument

STANDARD_SYSTEM_INSTRUCTIONS_TEMPLATE = Template(
    "You are a helpful Financial Document Assistant.\n"
    "Always ground your answers in the document context provided below.\n\n"
    "--- DOCUMENT CONTEXT ---\n"
    "{{ context }}\n"
    "------------------------"
)

UNIFIED_USER_PROMPT_TEMPLATE = Template(
    "You are a helpful Financial Document Assistant.\n"
    "Always ground your answers in the document context provided below.\n\n"
    "--- DOCUMENT CONTEXT ---\n"
    "{{ context }}\n"
    "------------------------\n\n"
    "{{ prompt }}"
)


def _validate_output(text: str, data_store: dict[str, object]) -> OutputGuardrailResult:
    """Output PII redaction plus financial math cross-checking."""
    redacted = gr.PIIRedactor.redact_text(text)
    verification = gr.FinancialRuleEngine.cross_check_text_assertions(
        redacted, cast(Mapping[str, float | gr.FinancialValue], data_store)
    )
    warnings: list[str] = [] if verification.passed else list(verification.discrepancies or [])
    return OutputGuardrailResult(text=redacted, warnings=warnings)


def _guardrails() -> GuardrailPipeline:
    return GuardrailPipeline(
        validate_input=standard_validate_input, validate_output=_validate_output
    )


def _postprocess(doc: StructuredDocument, source: str = "") -> dict[str, object]:
    """Build the financial data store from parsed tables (broker-safe strings)."""
    del source  # hook contract: domain postprocess signature
    data_store: dict[str, str] = {}
    for label, fv in extract_data_store(doc).items():
        data_store[label] = str(fv.raw_str if hasattr(fv, "raw_str") else fv)
    return {"data_store": data_store}


def _extract_numeric_column(table: Any) -> list[float]:
    """Return the first all-numeric column of a table (for sparklines)."""
    if not table.rows:
        return []
    for col_idx in range(min(len(table.rows[0]), len(table.headers))):
        try:
            values = [
                float(row[col_idx].replace("$", "").replace(",", "").replace("%", ""))
                for row in table.rows
                if col_idx < len(row)
            ]
        except ValueError:
            continue
        if values:
            return values
    return []


def _render(doc: StructuredDocument) -> str:
    """Render a financial document: unicode tables + sparkline trends."""
    from sqwakvox.formatting import TerminalChartPlotter, UnicodeTableFormatter

    parts: list[str] = [f"[bold]{doc.file_name}[/bold]\n"]
    if doc.raw_markdown:
        parts.append(doc.raw_markdown)

    for table in doc.tables:
        title = table.title or "Financial Data"
        parts.append(f"\n[bold underline]{title}[/bold underline]\n")
        parts.append(UnicodeTableFormatter.format_table(table))

        numeric_values = _extract_numeric_column(table)
        if numeric_values:
            spark = TerminalChartPlotter.render_sparkline(numeric_values)
            if spark:
                parts.append(f"\nTrend: {spark}")

    return "\n".join(parts)


FINANCIAL_DOMAIN = DocumentDomain(
    domain_id="financial",
    display_name="Financial",
    description="Financial document analysis: tables, math cross-validation, charts.",
    system_instructions=STANDARD_SYSTEM_INSTRUCTIONS_TEMPLATE,
    user_prompt_template=UNIFIED_USER_PROMPT_TEMPLATE,
    postprocess=_postprocess,
    guardrail_pipeline=_guardrails,
    render=_render,
)
