from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.errors import MarkupError
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip

from sqwakvox.formatting import (
    TerminalChartPlotter as TerminalChartPlotter,
)
from sqwakvox.formatting import (
    UnicodeTableFormatter as UnicodeTableFormatter,
)
from sqwakvox.models import StructuredDocument


def _wrap_single_width(line: str, width: int) -> list[str]:
    """Word-wrap *line* assuming every character occupies 1 terminal cell."""
    if not line or len(line) <= width:
        return [line]
    out: list[str] = []
    pos = 0
    length = len(line)
    while pos < length:
        if length - pos <= width:
            out.append(line[pos:])
            break
        cut = line.rfind(" ", pos, pos + width + 1)
        if cut <= pos:
            cut = pos + width
            out.append(line[pos:cut])
            pos = cut
        else:
            out.append(line[pos:cut])
            pos = cut + 1
    return out


class DocumentRenderPane(ScrollView):
    """Virtualized scrollable pane for rendered documents.

    Subclasses :class:`~textual.scroll_view.ScrollView` directly instead of
    mounting a giant :class:`~textual.widgets.Static` inside a
    ``VerticalScroll``.  Only the visible viewport rows are converted to
    ``Strip`` objects on each paint, so scrolling, chat-panel resizing, and
    multi-page updates stay responsive even on large documents.
    """

    DEFAULT_CSS = """
    DocumentRenderPane {
        overflow-x: hidden;
        overflow-y: auto;
    }
    """

    can_focus = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._parsed_lines: list[str | Text] = []
        self._wrapped_lines: list[str | Text] = []
        self._wrapped_width: int = 0
        self._strip_cache: dict[int, Strip] = {}
        self._cached_style: Style | None = None

    def update_document(self, doc: StructuredDocument, domain_id: str = "financial") -> None:
        """Render *doc* using its domain's renderer (see :mod:`sqwakvox.domains`)."""
        from sqwakvox.domains import get_domain

        domain = get_domain(domain_id)
        render_fn = domain.render
        content = render_fn(doc) if render_fn is not None else self._fallback_render(doc)
        self._set_markup(content)

    def _set_markup(self, content: str) -> None:
        parsed: list[str | Text] = []
        for raw_line in content.splitlines():
            # Domain renderers emit closed Rich markup tags ([bold]...[/bold],
            # [dim]...[/dim], etc.).  Only parse lines carrying closing tags so
            # raw markdown/code brackets (e.g. ``arr[i]`` or ``[1]``) stay fast
            # and unmangled.
            if "[/" in raw_line:
                try:
                    parsed.append(Text.from_markup(raw_line))
                except MarkupError:
                    parsed.append(raw_line)
            else:
                parsed.append(raw_line)
        self._parsed_lines = parsed
        self._wrapped_width = 0
        self._strip_cache.clear()
        width = self.scrollable_content_region.width
        if width > 0:
            self._rewrap(width)
        else:
            self._wrapped_lines = list(parsed)
            self.virtual_size = Size(0, len(self._wrapped_lines))
            self.refresh()

    def _rewrap(self, width: int) -> None:
        if width <= 0:
            return
        self._wrapped_width = width
        self._strip_cache.clear()
        wrapped: list[str | Text] = []
        console = self.app.console if self.is_attached else None

        for item in self._parsed_lines:
            if isinstance(item, str):
                if item.isascii():
                    wrapped.extend(_wrap_single_width(item, width))
                else:
                    cells = cell_len(item)
                    if cells <= width:
                        wrapped.append(item)
                    elif cells == len(item):
                        wrapped.extend(_wrap_single_width(item, width))
                    elif console is not None:
                        wrapped.extend(Text(item).wrap(console, width, overflow="fold"))
                    else:
                        wrapped.extend(_wrap_single_width(item, width))
            elif item.cell_len <= width or console is None:
                wrapped.append(item)
            else:
                wrapped.extend(item.wrap(console, width, overflow="fold"))

        self._wrapped_lines = wrapped
        self.virtual_size = Size(width, len(wrapped))
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        del event
        width = self.scrollable_content_region.width
        if width > 0 and width != self._wrapped_width and self._parsed_lines:
            self._rewrap(width)

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        if width <= 0:
            return Strip.blank(0, self.rich_style)
        if self._parsed_lines and self._wrapped_width != width:
            self._rewrap(width)

        if y == 0 or self._cached_style is None:
            style = self.rich_style
            if self._cached_style != style:
                self._cached_style = style
                self._strip_cache.clear()
        else:
            style = self._cached_style

        _, scroll_y = self.scroll_offset
        line_idx = scroll_y + y
        if line_idx < 0 or line_idx >= len(self._wrapped_lines):
            return Strip.blank(width, style)

        cached = self._strip_cache.get(line_idx)
        if cached is not None:
            return cached

        item = self._wrapped_lines[line_idx]
        if isinstance(item, str):
            strip = Strip([Segment(item, style)]).adjust_cell_length(width, style)
        else:
            segments = list(item.render(self.app.console, end=""))
            strip = Strip(segments).apply_style(style).adjust_cell_length(width, style)

        if len(self._strip_cache) > 512:
            self._strip_cache.clear()
        self._strip_cache[line_idx] = strip
        return strip

    @staticmethod
    def _fallback_render(doc: StructuredDocument) -> str:
        return f"[bold]{doc.file_name}[/bold]\n\n{doc.raw_markdown or ''}"

    def clear_document(self) -> None:
        self._parsed_lines = []
        self._wrapped_lines = []
        self._wrapped_width = 0
        self._strip_cache.clear()
        self.virtual_size = Size(0, 0)
        self.scroll_home(animate=False)
        self.refresh()
