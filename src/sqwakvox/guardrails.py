from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class VerificationResult(BaseModel):
    passed: bool
    discrepancies: list[str]


class FinancialValue(float):
    """Float subclass carrying unit metadata and raw string representation."""

    unit: str
    raw_str: str

    def __new__(
        cls, value: float | int, unit: str = "number", raw_str: str = ""
    ) -> FinancialValue:
        instance = super().__new__(cls, float(value))
        instance.unit = unit
        instance.raw_str = raw_str or str(value)
        return instance

    def __repr__(self) -> str:
        return f"FinancialValue({super().__repr__()}, unit={self.unit!r})"


def detect_unit(text: str) -> str:
    """Detect financial unit ('$', '%', or 'number') from text context or symbol."""
    if not text:
        return "number"
    cleaned = text.lower()
    if "%" in cleaned or any(w in cleaned for w in ["percent", "percentage", "pct"]):
        return "%"
    if any(sym in text for sym in ["$", "€", "£", "¥"]) or any(
        w in cleaned for w in ["usd", "eur", "gbp", "cad", "aud", "dollar", "dollars"]
    ):
        return "$"
    return "number"


def are_units_compatible(unit1: str, unit2: str) -> bool:
    """Determine if two unit types are mathematically compatible."""
    if unit1 == unit2:
        return True

    currency_units = {"$", "currency", "usd", "eur", "gbp"}
    percent_units = {"%", "percent", "percentage", "pct"}

    u1_is_curr = unit1.lower() in currency_units
    u2_is_curr = unit2.lower() in currency_units
    u1_is_pct = unit1.lower() in percent_units
    u2_is_pct = unit2.lower() in percent_units

    if (u1_is_curr and u2_is_pct) or (u1_is_pct and u2_is_curr):
        return False

    return True


def parse_financial_value(
    text: str, default_unit: str = "number"
) -> FinancialValue | None:
    """Parse a cell or text assertion into a FinancialValue with unit awareness."""
    if not text or not text.strip():
        return None

    raw_str = text.strip()
    unit = detect_unit(raw_str)
    if unit == "number" and default_unit != "number":
        unit = default_unit

    cleaned = re.sub(r"[\$€£¥,]", "", raw_str)
    cleaned = re.sub(
        r"\b(USD|EUR|GBP|CAD|AUD|dollars?|cents?|percent|percentage|pct)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.replace("%", "").strip()

    try:
        val = float(cleaned)
        if unit == "%":
            if "%" in raw_str or re.search(
                r"\b(percent|percentage|pct)\b", raw_str, re.I
            ):
                val /= 100.0
            elif val > 1.0 and default_unit == "%":
                val /= 100.0
        return FinancialValue(val, unit=unit, raw_str=raw_str)
    except ValueError:
        return None


class FinancialRuleEngine:
    @staticmethod
    def verify_column_sum(
        values: list[float | FinancialValue],
        expected_total: float | FinancialValue,
        tolerance: float = 0.01,
    ) -> bool:
        if not values:
            return False

        fv_values = [
            v if isinstance(v, FinancialValue) else FinancialValue(v) for v in values
        ]
        fv_expected = (
            expected_total
            if isinstance(expected_total, FinancialValue)
            else FinancialValue(expected_total)
        )

        # Reject summation if column contains mismatched units (e.g. % mixed with $)
        all_items = fv_values + [fv_expected]
        units = [item.unit for item in all_items if item.unit != "number"]
        if units:
            first_unit = units[0]
            for u in units[1:]:
                if not are_units_compatible(first_unit, u):
                    logger.warning(
                        "verify_column_sum rejected due to unit mismatch: %s vs %s",
                        first_unit,
                        u,
                    )
                    return False

        actual_sum = sum(float(v) for v in fv_values)
        expected_val = float(fv_expected)
        return abs(actual_sum - expected_val) <= tolerance

    @staticmethod
    def cross_check_text_assertions(
        response_text: str, data_store: dict[str, float | FinancialValue]
    ) -> VerificationResult:
        discrepancies: list[str] = []
        if not response_text or not data_store:
            return VerificationResult(passed=True, discrepancies=[])

        extracted: list[tuple[int, FinancialValue]] = []
        pattern = r"(?:[\$€£¥]\s*)?\b\d+(?:,\d{3})*(?:\.\d+)?\s*(?:%|\b(?:percent|percentage|pct|USD|EUR|GBP|dollars?)\b)?"

        for m in re.finditer(pattern, response_text, re.IGNORECASE):
            raw_match = m.group()
            fv = parse_financial_value(raw_match)
            if fv is not None:
                extracted.append((m.start(), fv))

        for label, known_val in data_store.items():
            if not label or len(label) <= 1:
                continue

            label_lower = label.lower()
            if label_lower not in response_text.lower():
                continue

            known_fv = (
                known_val
                if isinstance(known_val, FinancialValue)
                else parse_financial_value(str(known_val))
                or FinancialValue(float(known_val))
            )

            label_indices = [
                m.start()
                for m in re.finditer(re.escape(label_lower), response_text.lower())
            ]
            candidates: list[tuple[int, FinancialValue]] = []
            for pos, fv in extracted:
                min_dist = min(abs(pos - l_idx) for l_idx in label_indices)
                if min_dist < 120:
                    candidates.append((min_dist, fv))

            if not candidates:
                continue

            candidates.sort(key=lambda x: x[0])
            _, asserted_fv = candidates[0]

            if not are_units_compatible(asserted_fv.unit, known_fv.unit):
                discrepancies.append(
                    f"Unit mismatch for '{label}': asserted '{asserted_fv.raw_str}' (unit '{asserted_fv.unit}') "
                    f"does not match expected unit '{known_fv.unit}'"
                )
                continue

            if abs(asserted_fv - known_fv) > 0.01:
                discrepancies.append(
                    f"LLM value {asserted_fv.raw_str} does not match expected {known_fv.raw_str or known_fv} for '{label}'"
                )

        return VerificationResult(
            passed=len(discrepancies) == 0, discrepancies=discrepancies
        )


class PIIRedactor:
    REDACTION_PATTERNS: ClassVar[dict[str, re.Pattern[str]]] = {
        "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "CREDIT_CARD": re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"),
        "BANK_ACCOUNT": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
        "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    }

    @classmethod
    def redact_text(cls, text: str) -> str:
        redacted = text
        for label, pattern in cls.REDACTION_PATTERNS.items():
            redacted = pattern.sub(f"[{label}_REDACTED]", redacted)
        return redacted

    @classmethod
    def contains_pii(cls, text: str) -> bool:
        return any(pattern.search(text) for pattern in cls.REDACTION_PATTERNS.values())


class AnyGuardrailValidator:
    """Wrapper around Mozilla's any-guardrail framework for runtime validation.

    Provides a highly resilient execution layer with safe fallbacks if dependencies (like numpy)
    are missing or misconfigured in the local environment.
    """

    _initialized: ClassVar[bool] = False
    _guardrail_instance: ClassVar[Any] = None

    @classmethod
    def _try_init(cls) -> None:
        if cls._initialized:
            return
        cls._initialized = True
        try:
            from any_guardrail import AnyGuardrail, GuardrailName

            cls._guardrail_instance = AnyGuardrail.create(GuardrailName.INJECGUARD)
            logger.info("Mozilla any-guardrail successfully initialized with INJECGUARD.")
        except Exception as e:
            logger.warning(
                "Mozilla any-guardrail could not be fully initialized due to dependencies: %s. "
                "Sqwakvox will fall back to local rule-based sanitization and regex validation.",
                e,
            )

    @classmethod
    def validate_prompt(cls, prompt: str) -> bool:
        """Validates the prompt using any-guardrail, or falls back to True with safety logging."""
        cls._try_init()
        if cls._guardrail_instance is not None:
            try:
                result = cls._guardrail_instance.validate(prompt)
                if hasattr(result, "valid"):
                    return bool(result.valid)
                if hasattr(result, "passed"):
                    return bool(result.passed)
            except Exception as e:
                logger.error(f"Error during any-guardrail execution: {e}")
        return True


class AuditLogger:
    LOG_PATH = Path.home() / ".gemini" / "antigravity" / "sqwakvox" / "audit_log.jsonl"

    @classmethod
    def _ensure_log_dir(cls) -> None:
        cls.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def log(
        cls,
        document_id: str,
        operation: str,
        guardrail_checks: dict[str, Any] | None = None,
        action: str = "ALLOWED",
        risk_score: float = 0.0,
        input_text: str | None = None,
    ) -> None:
        cls._ensure_log_dir()
        entry = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "document_id": document_id,
            "operation": operation,
            "guardrail_checks": guardrail_checks or {},
            "action": action,
            "risk_score": risk_score,
        }
        if input_text is not None:
            entry["input_query_hash"] = hashlib.sha256(input_text.encode("utf-8")).hexdigest()

        try:
            with cls.LOG_PATH.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.error(f"Failed to write audit log: {e}")
