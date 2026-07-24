import pytest
from sqwakvox.guardrails import (
    FinancialRuleEngine,
    FinancialValue,
    are_units_compatible,
    detect_unit,
    parse_financial_value,
)
from sqwakvox.controller import AppController
from sqwakvox.models import StructuredDocument, TableData


def test_detect_unit():
    assert detect_unit("$1,000.50") == "$"
    assert detect_unit("1,000 USD") == "$"
    assert detect_unit("100 dollars") == "$"
    assert detect_unit("15%") == "%"
    assert detect_unit("15.5 percent") == "%"
    assert detect_unit("15 pct") == "%"
    assert detect_unit("1000.50") == "number"


def test_parse_financial_value():
    fv_curr = parse_financial_value("$1,000.50")
    assert fv_curr == 1000.50
    assert fv_curr.unit == "$"
    assert fv_curr.raw_str == "$1,000.50"

    fv_pct = parse_financial_value("15%")
    assert fv_pct == 0.15
    assert fv_pct.unit == "%"
    assert fv_pct.raw_str == "15%"

    fv_pct_word = parse_financial_value("15 percent")
    assert fv_pct_word == 0.15
    assert fv_pct_word.unit == "%"

    fv_small_curr = parse_financial_value("$0.15")
    assert fv_small_curr == 0.15
    assert fv_small_curr.unit == "$"


def test_are_units_compatible():
    assert are_units_compatible("$", "$") is True
    assert are_units_compatible("%", "%") is True
    assert are_units_compatible("number", "number") is True
    assert are_units_compatible("$", "number") is True
    assert are_units_compatible("%", "number") is True

    # Incompatible pairs: currency vs percent
    assert are_units_compatible("$", "%") is False
    assert are_units_compatible("%", "$") is False
    assert are_units_compatible("USD", "percent") is False


def test_verify_column_sum_apples_to_apples():
    # Dollars only
    val_100_dollars = FinancialValue(100.0, unit="$")
    val_150_dollars = FinancialValue(150.0, unit="$")
    val_250_dollars = FinancialValue(250.0, unit="$")
    assert FinancialRuleEngine.verify_column_sum(
        [val_100_dollars, val_150_dollars], val_250_dollars
    ) is True

    # Percents only
    val_10_pct = FinancialValue(0.10, unit="%")
    val_20_pct = FinancialValue(0.20, unit="%")
    val_30_pct = FinancialValue(0.30, unit="%")
    assert FinancialRuleEngine.verify_column_sum(
        [val_10_pct, val_20_pct], val_30_pct
    ) is True

    # Mixed units: $100 + 5% = $105 -> must fail because % and $ are mixed
    val_5_pct = FinancialValue(0.05, unit="%")
    val_105_dollars = FinancialValue(105.0, unit="$")
    assert FinancialRuleEngine.verify_column_sum(
        [val_100_dollars, val_5_pct], val_105_dollars
    ) is False


def test_cross_check_text_assertions_unit_awareness():
    data_store = {
        "Revenue": FinancialValue(1000.50, unit="$", raw_str="$1,000.50"),
        "Growth": FinancialValue(0.15, unit="%", raw_str="15%"),
    }

    # Case 1: Matching value & unit
    res_valid = FinancialRuleEngine.cross_check_text_assertions(
        "Revenue was $1,000.50 and Growth was 15%.", data_store
    )
    assert res_valid.passed is True
    assert res_valid.discrepancies == []

    # Case 2: LLM compares $ value for % field ($0.15 for Growth)
    res_unit_mismatch_growth = FinancialRuleEngine.cross_check_text_assertions(
        "Growth was $0.15.", data_store
    )
    assert res_unit_mismatch_growth.passed is False
    assert any("Unit mismatch" in d for d in res_unit_mismatch_growth.discrepancies)

    # Case 3: LLM compares % value for $ field (15% for Revenue)
    res_unit_mismatch_revenue = FinancialRuleEngine.cross_check_text_assertions(
        "Revenue was 15%.", data_store
    )
    assert res_unit_mismatch_revenue.passed is False
    assert any("Unit mismatch" in d for d in res_unit_mismatch_revenue.discrepancies)


def test_cross_validate_table_mixed_units():
    controller = AppController()

    doc_mixed = StructuredDocument(
        file_name="mixed.md",
        raw_markdown="",
        tables=[
            TableData(
                headers=["Financials"],
                rows=[
                    ["$100"],
                    ["5%"],
                    ["$105"],
                ],
            )
        ],
    )

    results = controller.cross_validate(doc_mixed)
    assert len(results) == 1
    col_name, expected, actual, is_valid = results[0]
    assert col_name == "Financials"
    assert is_valid is False
