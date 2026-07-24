# Implementation Plan: Guardrail Specification for Financial Data

This plan outlines the design and integration of safety and compliance guardrails for Sqwakvox. 

Given the sensitive nature of financial documents, this plan implements strict mathematical verification, PII redaction, and audit logging using Mozilla's **any-guardrail** framework.

---

## 1. Objectives & Compliance Guardrails

1. **Numerical Cross-Verification**: Automatically cross-validate sums and balances within tables (e.g. assets vs. liabilities, sums of columns) before displaying LLM-generated assertions.
2. **PII & Sensitive Data Redaction**: Intercept, mask, or redact Personally Identifiable Information (PII), bank details, client names, and trade identifiers before sending queries to remote/external LLMs.
3. **Structured Audit Logging**: Maintain a secure, machine-readable validation ledger recording all checks, failures, and redaction metadata.

---

## 2. Guardrails Architecture

The guardrail layers sandwich the LLM call to perform sanitization on inputs and structural verification on outputs.

```
[ User Prompt ] ──► [ Input Guardrail: Block Prompt Injection & Redact PII ] ──► [ LLM Agent ]
                                                                                       │
[ Output Log ]  ◄── [ Output Guardrail: Redact PII & Numerical Verification ] ◄───────┘
```

> [!WARNING]
> **Data Privacy Mandate**: Raw financial sheets frequently contain bank account numbers, tax identifiers, or corporate secrets. Under NO circumstance should unredacted inputs containing sensitive identifiers be processed without going through the `PIIRedactor` first, ensuring compliance with GDPR, HIPAA, and CCPA guidelines.

---

## 3. Numerical Cross-Verification Specification

Large Language Models frequently hallucinate or miscalculate financial values. The output guardrail will run a Python-driven rule verification engine to cross-examine mathematical values parsed in tables vs. statements asserted in LLM text responses.

### A. Core Mathematical Assertions (Rule Engine):
- **Column Totals**: Sum of column values must match the column total within $\pm 0.01$ margin of error (accounting for rounding differences).
- **Accounting Equation**: Total Assets MUST equal Total Liabilities + Equity.
- **Percentage Summation**: Proportional values (such as shares of portfolio allocations) must sum to $100\%$ ($1.00$) unless explicitly styled as partial.

### B. Verification Code Implementation:

```python
import re
from typing import Dict, List
from pydantic import BaseModel

class VerificationResult(BaseModel):
    passed: bool
    discrepancies: List[str]

class FinancialRuleEngine:
    """Rigorous math checker for table context vs. LLM output assertions."""

    @staticmethod
    def verify_column_sum(values: List[float], expected_total: float, tolerance: float = 0.01) -> bool:
        """Verify that the sum of a list of floats matches the expected total."""
        actual_sum = sum(values)
        return abs(actual_sum - expected_total) <= tolerance

    @staticmethod
    def cross_check_text_assertions(response_text: str, data_store: Dict[str, float]) -> VerificationResult:
        """Cross-examine numbers in the LLM response text against verified table values."""
        discrepancies = []
        
        # Regex to locate currencies, percentages, and floats
        numbers_found = re.findall(r"\$?\b\d+(?:\.\d+)?%?\b", response_text)
        
        for num_str in numbers_found:
            # Clean formatting (e.g. '$100.50' -> 100.50, '15%' -> 0.15)
            val = float(num_str.replace("$", "").replace("%", "").replace(",", ""))
            if "%" in num_str:
                val /= 100.0
                
            # Attempt to cross check with known values in document
            # (e.g. if the LLM states 'operating margin was 18%' but the table says '19.1%')
            # ... custom lookup logic goes here ...
            
        passed = len(discrepancies) == 0
        return VerificationResult(passed=passed, discrepancies=discrepancies)
```

---

## 4. PII Redaction Engine

Protects client names, tax numbers (SSNs/TINs), routing numbers, and credit cards.

```python
class PIIRedactor:
    """Regex and NLP based high-fidelity PII masking engine."""

    REDACTION_PATTERNS = {
        "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "CREDIT_CARD": re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"),
        "BANK_ACCOUNT": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), # IBAN format
        "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    }

    @classmethod
    def redact_text(cls, text: str) -> str:
        """Replace all sensitive patterns with anonymized tags."""
        redacted = text
        for label, pattern in cls.REDACTION_PATTERNS.items():
            redacted = pattern.sub(f"[{label}_REDACTED]", redacted)
        return redacted
```

---

## 5. Audit Logging Architecture

Every guardrail invocation produces a standardized audit trail stored in a JSONL file in a secure local logs folder (`~/.gemini/antigravity/sqwakvox/audit_log.jsonl`).

```json
{
  "timestamp": "2026-05-22T14:39:40Z",
  "document_id": "doc_8f12a3bc",
  "operation": "output_validation",
  "input_query_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "guardrail_checks": {
    "pii_redacted": true,
    "math_verified": true,
    "safety_passed": true
  },
  "action": "ALLOWED_AND_REDACTED",
  "risk_score": 0.02
}
```

> [!NOTE]
> **Audit Traceability**: The audit log JSON fields align perfectly with Mozilla's `any-guardrail` default telemetry schemas, making it simple to pipe into larger enterprise dashboard systems or local visualization widgets inside the TUI in the future.
 
 ### Summary of Improvements  ###                                                                                                                                          
                                                                                                                                                                        
 1. Unit-Aware Financial Values (FinancialValue):                                                                                                                       
     - Implemented FinancialValue(float) subclass in src/sqwakvox/guardrails.py which wraps numeric values with unit metadata ("$", "%", or "number") and raw string    
       representations while maintaining full backward compatibility as standard Python float instances.                                                                
                                                                                                                                                                        
 2. Unit Detection & Parsing (detect_unit, parse_financial_value):                                                                                                      
     - Added unit detection and extraction helpers capable of recognizing currency indicators ($, €, £, USD, dollars), percentage indicators (%, percent, percentage,   
       pct), and unitless numbers across both tabular data and freeform response text.                                                                                  
                                                                                                                                                                        
 3. Unit Compatibility Validation (are_units_compatible):                                                                                                               
     - Defined rules preventing cross-unit math/comparisons. Incompatible pairs (such as currency $ vs percentage %) are explicitly flagged as unit mismatches.         
                                                                                                                                                                        
 4. Column Summation Guardrails (verify_column_sum & cross_validate):                                                                                                   
     - verify_column_sum now checks unit compatibility across all column entries and expected totals. Summing mixed unit entries (e.g., adding $100 and 5% to get $105) 
       is automatically rejected and flagged as invalid.                                                                                                                
     - Updated cross_validate in AppController (src/sqwakvox/controller.py) to parse table cells and column headers with unit awareness.                                
                                                                                                                                                                        
 5. Text Assertion Cross-Verification (cross_check_text_assertions):                                                                                                    
     - Text assertion extraction now captures numbers alongside their units and matches assertions to data store labels based on proximity.                             
     - If an LLM response asserts a percentage value for a currency field (e.g., "Revenue was 15%") or a currency value for a percentage field (e.g., "Growth was       
       $0.15" when growth is 15%), the guardrail flags a unit mismatch discrepancy.                                                                                     
                                                                                                                                                                        
 6. Automated Testing:                                                                                                                                                  
     - Added unit test suite in tests/test_guardrails.py covering unit detection, unit parsing, column summation guardrails, and LLM text assertion unit mismatch       
       detection.                                                                                                                                                       

