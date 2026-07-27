@regression @privacy @pii
Feature: Personally Identifiable Information (PII) Redaction
  As a data privacy officer
  I want sensitive financial and personal information redacted from queries and model responses
  So that confidential user data is never exposed or logged in plain text

  Background:
    Given the PII redactor engine is configured with active pattern rules

  @smoke @happy-path
  Scenario: Redact sensitive PII from incoming user query before LLM processing
    Given a user query "Check account balance for SSN 000-12-3456 and email user@example.com"
    When the query passes through the local PII redactor
    Then sensitive pattern matches should be replaced with redaction placeholders
    And the query passed to the agent should not contain plain text PII

  @happy-path @privacy
  Scenario: Automatically redact PII returned in model generated responses
    Given a raw model response containing credit card number "4532-1100-8899-1234"
    When the system prepares the agent result for display
    Then the response text should be redacted before rendering
    And the agent result should indicate that response PII redaction occurred

  @regression
  Scenario: Pass non-sensitive financial queries without modifying text
    Given a user prompt "Calculate the net profit margin for Q3 2025"
    When the prompt is processed by the PII redactor
    Then the prompt text should remain identical to the original input
    And the query PII redacted flag should be false

  @negative @privacy
  Scenario Outline: Redact various categories of PII from text inputs
    Given an input text containing <input_text>
    When PII redaction is executed on the text
    Then the redacted text should contain <redacted_pattern>
    And plain text <pii_category> details should be removed

    Examples:
      | input_text                                     | redacted_pattern       | pii_category     |
      | "SSN: 123-45-6789"                            | "[REDACTED-SSN]"       | social security  |
      | "Card: 4111-2222-3333-4444"                    | "[REDACTED-CC]"        | credit card      |
      | "IBAN: US12345678901234567890"                 | "[REDACTED-BANK]"      | bank account     |
      | "Contact author at john.doe@financialfirm.org" | "[REDACTED-EMAIL]"     | email address    |
      | "Call customer service at 1-800-555-0199"      | "[REDACTED-PHONE]"     | phone number     |
