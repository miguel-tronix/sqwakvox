# Implementation Plan: Docling Financial Document Renderer

This plan outlines the design and development of the custom **Docling Financial Document Renderer** for Sqwakvox. 

The goal is to extract high-fidelity layouts from financial documents (PDFs, spreadsheets, HTML tables) and render them elegantly inside a Textual terminal environment.

---

## 1. Objectives & Focus

1. **Tabular Ingestion**: Handle highly complex multi-line headers, cell spans, and numerical alignments in financial tables.
2. **Unicode Table Layouts**: Generate sleek, responsive tables using specialized box-drawing characters and alignment rules (e.g., aligning decimal points).
3. **Block-Character Plotting**: Transform tabular numerical data directly into high-fidelity terminal charts (bar charts, sparklines, line charts) using Unicode block elements (e.g. `█`, `▄`, `░`).
4. **Visual Document Structure**: Map headings, paragraphs, and list blocks into custom Textual styling tags.

---

## 2. Ingestion Pipeline

Using IBM's **Docling**, the document layout is extracted. Standard Markdown conversion loses crucial table cell coordinates and spans. We use Docling's hierarchical native object model (`docling_core`) to extract coordinates and properties.

```
[ PDF / Spreadsheet / HTML ]
            │
            ▼ (Docling Ingestion Engine)
[ Hierarchical Element Tree ]
       ├── Paragraphs ──► Textual Markdown Widget
       ├── Figures    ──► Metadata & Alt-Text log
       └── Tables     ──► Custom Ingestion Handler ──► Unicode Grid + Block Plotter
```

---

## 3. Unicode Table Formatter Specification

Standard Markdown tables wrap and look highly disjointed in the terminal. The renderer must compute column dimensions dynamically and align numbers cleanly.

### Double-Border Financial Format Example:
```
╔══════════════════════════╦═════════════╦═════════════╦═════════════╗
║ Financial Metric         ║   Q1 FY26   ║   Q2 FY26   ║   Variance  ║
╠══════════════════════════╬═════════════╬═════════════╬═════════════╣
║ Revenue ($M)             ║      124.50 ║      136.20 ║      +9.4%  ║
║ Operating Margin         ║       18.4% ║       19.1% ║      +0.7%  ║
║ Net Income ($M)          ║       22.90 ║       26.00 ║     +13.5%  ║
╚══════════════════════════╩═════════════╩═════════════╩═════════════╝
```

### Table Formatting Logic:
1. **Column Width Calculation**: The width of each column is computed dynamically:
   $$\text{width}_c = \max(\{\text{len}(cell_{r,c}) \text{ for } r \text{ in rows}\} \cup \{\text{min\_width}\})$$
2. **Alignment Rules**:
   - Right-align columns where the majority of cells are numerical values (numbers, currencies, percentages).
   - Left-align text columns (labels, descriptions).
3. **Border Styling**: Use single-line box drawing (`│`, `─`, `┌`, `┐`, `└`, `┘`, `├`, `┤`, `┬`, `┴`, `┼`) for body grids, and double-line box drawing (`║`, `═`, `╔`, `╗`, `╚`, `╝`, `╠`, `╣`, `╦`, `╩`, `╬`) to demarcate headers and final rows (totals).

---

## 4. Block-Character Chart Generator (Visual Engine)

To provide an instant visual analysis of table metrics, the renderer will include a built-in terminal chart plotter.

### A. Horizontal Bar Chart Generator
Renders numerical rows as beautifully scaled bar plots in the terminal:

```
Revenue Growth Comparison ($M):
Q1 FY26 ░░░░░░░░░░░░░░░░░░░░░░ 124.5
Q2 FY26 ░░░░░░░░░░░░░░░░░░░░░░░░ 136.2
Target  ████████████████████████████ 150.0
```

### B. Vertical Trend Plot Generator
Uses block-height characters (` `, ` `, `▂`, `▃`, `▄`, `▅`, `▆`, `▇`, `█`) to show quarterly trends:

```
Net Income Trend (Last 8 Quarters):
  30M ┤
      │           ▄   ▆   █   ▇
      │   ▄   ▂   █   █   █   █
      │   █   █   █   █   █   █
   0M ┼───░───░───░───░───░───░───
         Q1  Q2  Q3  Q4  Q1  Q2
```

### Formatting Code Contract:

```python
class TerminalChartPlotter:
    """Helper engine to render ASCII and Unicode terminal visual charts."""

    @staticmethod
    def render_horizontal_bars(labels: list[str], values: list[float], max_width: int = 40) -> str:
        """Draw a custom horizontal block-bar chart."""
        if not values:
            return ""
        max_val = max(values)
        if max_val == 0:
            return "\n".join(f"{label:<15} |" for label in labels)
            
        lines = []
        for label, val in zip(labels, values):
            bar_len = int((val / max_val) * max_width)
            bar = "█" * bar_len + "░" * (max_width - bar_len)
            lines.append(f"{label:<15} {bar} {val:>8.2f}")
        return "\n".join(lines)

    @staticmethod
    def render_sparkline(values: list[float]) -> str:
        """Generate a compact block sparkline for trends."""
        if not values:
            return ""
        blocks = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        min_v, max_v = min(values), max(values)
        rng = max_v - min_v
        if rng == 0:
            return "".join(blocks[4] for _ in values)
            
        spark = []
        for val in values:
            idx = int(((val - min_v) / rng) * (len(blocks) - 1))
            spark.append(blocks[idx])
        return "".join(spark)
```

---

## 5. Textual Custom Integration

We will wrap this rendering engine into a custom Textual widget:

```python
from textual.widgets import Static
from textual.app import ComposeResult

class DocumentRenderPane(Static):
    """Textual widget holding custom rendered content with markdown and charts."""
    
    def update_document(self, doc: StructuredDocument) -> None:
        """Parse structured document and compose rich text contents."""
        content = []
        content.append(f"# {doc.metadata.file_name}\n")
        
        # Append main text
        content.append(doc.raw_markdown)
        
        # Format and append tables beautifully
        for table in doc.tables:
            content.append(f"\n### Table: {table.title or 'Financial Data'}\n")
            content.append(table.markdown_representation)
            
            # Auto-generate visual chart if numerical data is detected
            # (e.g. if row values represent progress or metrics)
            # content.append(self.try_render_table_charts(table))
            
        self.update("".join(content))
```
