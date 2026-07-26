@regression @sparkline @charts
Feature: Chart Rendering from Financial Documents
  As a financial analyst
  I want to view sparkline and bar charts in rendered financial documents
  So that I can visually analyze trends and data patterns

  Background:
    Given a financial document with tabular data containing numeric values

  @smoke @happy-path
  Scenario: Generate sparkline chart for document with numeric trends
    Given a document with financial data containing numeric columns
    When the document render pane generates charts for its tables
    Then numeric columns should have sparkline representations rendered

  @regression
  Scenario: Render horizontal bar charts for summary comparisons
    Given a table with financial metrics {"Revenue": [1000, 1500, 2000], "Expenses": [800, 900, 1000]}
    When horizontal bar charts are generated for the metrics
    Then each bar should be proportional to its value

  @negative @ui
  Scenario Outline: Handle invalid data in chart generation
    Given a table with invalid numeric data in column <column_name>
    When the document tries to generate charts
    Then the system should handle the invalid data gracefully without crashing

    Examples:
      | column_name |
      | "invalid"   |
      | "N/A"       |
      | ""           |

  @regression
  Scenario Outline: Generate charts with boundary numeric values
    Given a table where the numeric column contains <values>
    When sparkline charts are rendered
    Then the chart should accurately represent the trend

    Examples:
      | values               |
      | [0, 0, 0]           |
      | [5, 5, 5]           |
      | [1, 5, 10, 50]      |
      | [-10, -5, 0, 5]     |

  @smoke @ui
  Scenario: Request charts when no financial data exists
    Given an empty financial document with no numeric columns
    When the system attempts to generate charts
    Then no chart representations should be generated
