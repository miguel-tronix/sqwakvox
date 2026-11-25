from sqwakvox.controller import AppController
from sqwakvox.guardrails import (
    FinancialRuleEngine,
    FinancialValue,
    are_units_compatible,
    detect_unit,
    parse_financial_value,
)
from sqwakvox.models import StructuredDocument, TableData


def test_detect_unit() -> None:
    assert detect_unit("$1,000.50") == "$"
    assert detect_unit("1,000 USD") == "$"
    assert detect_unit("100 dollars") == "$"
    assert detect_unit("15%") == "%"
    assert detect_unit("15.5 percent") == "%"
    assert detect_unit("15 pct") == "%"
    assert detect_unit("1000.50") == "number"


def test_parse_financial_value() -> None:
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


def test_are_units_compatible() -> None:
    assert are_units_compatible("$", "$") is True
    assert are_units_compatible("%", "%") is True
    assert are_units_compatible("number", "number") is True
    assert are_units_compatible("$", "number") is True
    assert are_units_compatible("%", "number") is True

    # Incompatible pairs: currency vs percent
    assert are_units_compatible("$", "%") is False
    assert are_units_compatible("%", "$") is False
    assert are_units_compatible("USD", "percent") is False


def test_verify_column_sum_apples_to_apples() -> None:
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


def test_cross_check_text_assertions_unit_awareness() -> None:
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


def test_cross_validate_table_mixed_units() -> None:
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
    col_name, _expected, _actual, is_valid = results[0]
    assert col_name == "Financials"
    assert is_valid is False


def test_any_guardrail_validator_prefers_local_cache_when_present(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from sqwakvox.guardrails import AnyGuardrailValidator

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **kwargs: object) -> None:
            captured["provider_kwargs"] = kwargs

    fake_guardrail = MagicMock()

    def fake_create(name: object, provider: object = None) -> MagicMock:
        captured["name"] = name
        captured["provider"] = provider
        return fake_guardrail

    monkeypatch.setattr(AnyGuardrailValidator, "_initialized", False)
    monkeypatch.setattr(AnyGuardrailValidator, "_guardrail_instance", None)
    monkeypatch.setattr(
        "huggingface_hub.snapshot_download",
        lambda repo_id, local_files_only=False: "/app/.cache/huggingface/injecguard"
        if repo_id == "leolee99/InjecGuard" and local_files_only
        else (_ for _ in ()).throw(AssertionError("unexpected call")),
    )
    monkeypatch.setattr("any_guardrail.providers.huggingface.HuggingFaceProvider", FakeProvider)
    monkeypatch.setattr("any_guardrail.AnyGuardrail.create", fake_create)

    AnyGuardrailValidator._try_init()

    assert AnyGuardrailValidator._guardrail_instance is fake_guardrail
    assert isinstance(captured["provider"], FakeProvider)
    assert captured["provider_kwargs"] == {
        "trust_remote_code": True,
        "tokenizer_id": "/app/.cache/huggingface/injecguard",
        "model_kwargs": {"local_files_only": True},
    }
