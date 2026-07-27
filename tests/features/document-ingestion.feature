@regression @ingestion @docling
Feature: Document Ingestion and Layout Parsing
  As a financial analyst
  I want to ingest PDF documents and extract structured text and tables
  So that I can interactively query document contents and financial figures

  Background:
    Given the Sqwakvox document conversion engine is initialized

  @smoke @happy-path
  Scenario: Successfully parse local PDF document with layout and tables
    Given a local PDF document "quarterly_report.pdf" containing text and tables
    When the user requests to ingest the document
    Then the document should be parsed into structured markdown
    And all contained financial tables should be extracted into tabular data structures

  @happy-path @tables
  Scenario: Ingest financial tables and extract tabular dataframe structures
    Given a financial statement document with balance sheet tables
    When table conversion is executed on the document
    Then table column headers and data rows should be accurately captured
    And a structured document model should be returned containing all extracted tables

  @edge-case
  Scenario: Handle empty or single-page PDF document without tables
    Given a PDF document containing text content but no financial tables
    When the document is ingested into the system
    Then the system should process the document text without error
    And the extracted tables list should be empty

  @negative @error-handling
  Scenario: Attempt to ingest a non-existent or corrupted document file
    Given a file path "non_existent_file.pdf" that does not exist on disk
    When the user attempts to convert the document
    Then the conversion engine should raise a document processing error
    And the failure metric should be recorded in the telemetry system

  @regression
  Scenario Outline: Parse financial documents from diverse file sources
    Given a financial document from source "<source_location>" of type "<document_type>"
    When the document ingestion process runs
    Then the system should extract "<expected_output>" from the document

    Examples:
      | source_location                 | document_type     | expected_output            |
      | "samples/banks.txt"            | plain text        | plain text markdown        |
      | "samples/financial-stmt.pdf"    | local PDF file    | markdown text and tables   |
      | "https://example.com/report.pdf"| remote URL        | fetched markdown content   |
      | "samples/multi-page.pdf"        | multi-page report | multi-page document model  |
