from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.widgets import Static

from sqwakvox.models import StructuredDocument, TableData


class TerminalChartPlotter:
    @staticmethod
    def render_horizontal_bars(
        labels: list[str], values: list[float], max_width: int = 40
    ) -> str:
        if not values:
            return ""
        max_val = max(values)
        if max_val == 0:
            return "\n".join(f"{label:<15} |" for label in labels)

        lines: list[str] = []
        for label, val in zip(labels, values):
            bar_len = int((val / max_val) * max_width)
            bar = "█" * bar_len + "░" * (max_width - bar_len)
            lines.append(f"{label:<15} {bar} {val:>8.2f}")
        return "\n".join(lines)

    @staticmethod
    def render_sparkline(values: list[float]) -> str:
        if not values:
            return ""
        blocks = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        min_v, max_v = min(values), max(values)
        rng = max_v - min_v
        if rng == 0:
            return "".join(blocks[4] for _ in values)

        spark: list[str] = []
        for val in values:
            idx = int(((val - min_v) / rng) * (len(blocks) - 1))
            spark.append(blocks[idx])
        return "".join(spark)


class UnicodeTableFormatter:
    @staticmethod
    def _detect_alignment(rows: List[List[str]], col_idx: int) -> str:
        numeric_count = 0
        for row in rows:
            cell = row[col_idx].strip()
            cleaned = cell.replace("$", "").replace(",", "").replace("%", "")
            try:
                float(cleaned)
                numeric_count += 1
            except ValueError:
                pass
        return "right" if numeric_count > len(rows) // 2 else "left"

    @staticmethod
    def format_table(table: TableData, header_border: bool = True) -> str:
        if not table.headers and not table.rows:
            return ""

        headers = table.headers
        rows = table.rows
        num_cols = max(len(headers), max((len(r) for r in rows), default=0))
        if num_cols == 0:
            return ""

        padded_headers = [h or "" for h in headers] + [""] * (num_cols - len(headers))
        padded_rows = [
            [r[i] if i < len(r) else "" for i in range(num_cols)] for r in rows
        ]

        alignments = [
            UnicodeTableFormatter._detect_alignment(padded_rows, i)
            for i in range(num_cols)
        ]

        col_widths: list[int] = []
        for i in range(num_cols):
            widths = [len(padded_headers[i])]
            widths.extend(len(r[i]) for r in padded_rows)
            col_widths.append(max(widths) + 2)

        sep_h = "═"
        sep_v = "║"
        sep_t = "╤"
        sep_m = "╪"
        sep_b = "╧"

        top = "╔" + sep_h.join(sep_h * w for w in col_widths) + "╗"
        header_sep = (
            "╠" + sep_t.join(sep_h * w for w in col_widths) + "╣"
            if header_border
            else ""
        )
        body_sep = "╟" + sep_m.join("─" * w for w in col_widths) + "╢"
        bottom = "╚" + sep_b.join(sep_h * w for w in col_widths) + "╝"

        def _format_row(cells: list[str], alignments: list[str]) -> str:
            parts: list[str] = []
            for cell, w, align in zip(cells, col_widths, alignments):
                text = cell.center(w)
                parts.append(text)
            return sep_v + sep_v.join(parts) + sep_v

        lines: list[str] = [top]

        if padded_headers[0]:
            lines.append(_format_row(padded_headers, ["center"] * num_cols))
            if header_border:
                lines.append(header_sep)

        for i, row in enumerate(padded_rows):
            lines.append(_format_row(row, alignments))
            if i < len(padded_rows) - 1:
                lines.append(body_sep)

        lines.append(bottom)
        return "\n".join(lines)


class DocumentRenderPane(Static):
    def update_document(self, doc: StructuredDocument) -> None:
        content: list[str] = []
        content.append(f"[bold]{doc.file_name}[/bold]\n")

        if doc.raw_markdown:
            content.append(doc.raw_markdown)

        for table in doc.tables:
            title = table.title or "Financial Data"
            content.append(f"\n[bold underline]{title}[/bold underline]\n")
            content.append(UnicodeTableFormatter.format_table(table))

            numeric_values = self._extract_numeric_column(table)
            if numeric_values:
                labels = [row[0] for row in table.rows if row]
                spark = TerminalChartPlotter.render_sparkline(numeric_values)
                if spark:
                    content.append(f"\nTrend: {spark}")

        self.update("\n".join(content))

    @staticmethod
    def _extract_numeric_column(table: TableData) -> list[float]:
        if not table.rows:
            return []
        for col_idx in range(min(len(table.rows[0]), len(table.headers))):
            values: list[float] = []
            for row in table.rows:
                if col_idx < len(row):
                    cleaned = row[col_idx].replace("$", "").replace(",", "").replace("%", "")
                    try:
                        values.append(float(cleaned))
                    except ValueError:
                        break
            else:
                if values:
                    return values
        return []

    def clear_document(self) -> None:
        self.update("")
