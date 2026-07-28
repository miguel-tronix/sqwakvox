@regression @financial @cross-validation
Feature: Financial Data Cross-Validation and Assertion Checking
  As a financial audit supervisor
  I want table numerical totals and model assertions verified against extracted table figures
  So that mathematical errors, discrepancies, and hallucinated financial figures are flagged

  Background:
    Given the Financial Rule Engine is initialized with active validation rules

  @smoke @happy-path
  Scenario: Verify valid column sum in financial tables
    Given a financial table column containing numeric values [100.0, 200.0, 300.0] and total 600.0
    When column sum verification is executed by the rule engine
    Then the verification check should pass successfully with zero discrepancies

  @regression @audit
  Scenario: Flag discrepancy when column total does not match individual row items
    Given a financial table column with items [150.0, 250.0] and recorded total 500.0
    When column sum verification is executed
    Then the verification result should be marked as invalid
    And the telemetry system should record an invalid check count

  @happy-path @assertion
  Scenario: Cross-check LLM response text assertions against document data store
    Given an extracted financial data store with "Revenue" value 5000000.0
    And an agent response stating "Revenue reached 6000000.0 dollars in Q4"
    When text assertions are cross-checked against the financial data store
    Then a mathematical assertion discrepancy should be detected
    And the discrepancy list should contain details for "Revenue" mismatch

  @negative @units
  Scenario: Reject mathematical comparisons between incompatible financial units
    Given a numeric value "100" with unit "$"
    And a comparison value "100" with unit "%"
    When unit compatibility check is performed
    Then the rule engine should declare the units incompatible
    And unit mismatch error should prevent invalid cross-validation

  @regression
  Scenario Outline: Validate column totals across diverse unit types and boundary values
    Given a table column with item values <row_values> and expected total <expected_total> with unit "<unit_type>"
    When column total cross-validation is performed
    Then the mathematical check outcome should be "<validation_result>"

    Examples:
      | row_values                   | expected_total | unit_type | validation_result |
      | [1000.50, 2000.25, 3000.25]  | 6001.00        | $         | valid             |
      | [12.5, 15.0, 22.5]           | 50.0           | %         | valid             |
      | [5, 10, 15]                  | 40             | number    | invalid           |
      | [-500.0, 1000.0, -200.0]     | 300.0          | $         | valid             |
      | [0.0, 0.0, 0.0]              | 0.0            | number    | valid             |
