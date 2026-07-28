@regression @agent @multi-model
Feature: Multi-Provider LLM Agent Execution and Chat
  As a financial researcher
  I want to chat with ingested documents using various LLM model providers
  So that I can leverage different reasoning models with secure API key management

  Background:
    Given an ingested financial document context is available in the workspace

  @smoke @happy-path
  Scenario: Execute document query using model with system role support
    Given a selected model provider "anthropic:claude-4.6" that supports system roles
    And a valid API key for "ANTHROPIC_API_KEY"
    When the user submits a financial query "What is the net profit margin?"
    Then system instructions and context should be separated into system prompt
    And the agent should return a grounded response from the document

  @happy-path @compatibility
  Scenario: System role fallback for models lacking system role support
    Given a selected model provider "gemini:gemini-3.6-flash" that does not support system roles
    And a valid API key for "GEMINI_API_KEY"
    When the user submits a query against the document context
    Then system instructions and document context should be merged into unified user prompt
    And instructions parameter should be set to None for the API call

  @security @credentials
  Scenario: Temporarily inject API credentials into environment during query execution
    Given an API key provided at runtime in the UI
    When the AnyAgent orchestrator executes the model query
    Then the API key should be temporarily injected into the environment
    And after query completion the environment variable should be restored to its original state

  @negative @resilience
  Scenario: Handle agent execution timeouts and recursion limits gracefully
    Given a complex multi-tool reasoning query that exceeds iteration limits
    When the agent execution loop hits max recursion limit 20 or timeout 180 seconds
    Then the execution should terminate gracefully without crashing the TUI
    And an informative error message should be returned in the agent result

  @regression
  Scenario Outline: Route financial queries across supported LLM model providers
    Given selected model provider "<model_id>" requiring environment key "<env_var>"
    And system role support is "<supports_system_role>"
    When the user executes a query with a valid model API key
    Then the orchestrator should invoke "<model_id>" with proper prompt template formatting

    Examples:
      | model_id                  | env_var           | supports_system_role |
      | openai:gpt-5.5-high       | OPENAI_API_KEY    | True                 |
      | anthropic:claude-4.6      | ANTHROPIC_API_KEY | True                 |
      | gemini:gemini-3.6-flash   | GEMINI_API_KEY    | False                |
      | gemini:gemini-3.5-pro     | GEMINI_API_KEY    | True                 |
      | deepseek:deepseek-v4-flash| DEEPSEEK_API_KEY  | True                 |
