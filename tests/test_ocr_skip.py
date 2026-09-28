"""Tests for skipping Docling's OCR on PDFs that already carry a text layer.

OCR is roughly a third of CPU-only conversion time and is pure waste on a
digital PDF, so :func:`~sqwakvox.controller.pdf_has_text_layer` gates
:meth:`~sqwakvox.controller.AppController.convert_document` onto a converter
built with ``do_ocr=False``.  These tests cover the gate itself and the
converter selection, using fakes so no Docling model is ever loaded.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqwakvox.controller import AppController, pdf_has_text_layer

_SAMPLE = "samples/fs023-sample-financial-statements.pdf"


class _FakeResult:
    def __init__(self, md: str) -> None:
        self.document = MagicMock()
        self.document.export_to_markdown.return_value = md
        self.document.tables = []


class _FakeConverter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def convert(self, source, **kwargs):
        self.calls.append((source, kwargs))
        return _FakeResult("# doc\n")


# --------------------------------------------------------------------------- #
# pdf_has_text_layer
# --------------------------------------------------------------------------- #


def test_text_layer_detected_on_real_sample() -> None:
    """The bundled sample PDF is digital, so the probe must find text."""
    assert pdf_has_text_layer(_SAMPLE) is True


def test_non_pdf_source_is_never_probed() -> None:
    """Non-PDF inputs fall through to OCR without touching pdfium."""
    assert pdf_has_text_layer("chapter5.html") is False
    assert pdf_has_text_layer("book.chm") is False


def test_missing_file_is_false() -> None:
    """A nonexistent path is not a reason to skip OCR."""
    assert pdf_has_text_layer("/nonexistent/definitely-not-here.pdf") is False


def test_probe_failure_falls_back_to_ocr() -> None:
    """A pdfium error must return False, never raise into the parse path."""

    with patch("sqwakvox.controller.pdfium.PdfDocument", side_effect=RuntimeError("corrupt")):
        assert pdf_has_text_layer("whatever.pdf") is False


def test_page_without_text_reports_false() -> None:
    """A scan (image-only pages) has no text layer, so OCR still runs."""

    class _EmptyTextpage:
        def get_text_range(self) -> str:
            return ""

        def close(self) -> None:
            return None

    class _Page:
        def get_textpage(self) -> _EmptyTextpage:
            return _EmptyTextpage()

    class _Pdf:
        def __init__(self, _source: str) -> None:
            pass

        def __len__(self) -> int:
            return 5

        def __getitem__(self, _index: int) -> _Page:
            return _Page()

        def __enter__(self) -> _Pdf:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    with patch("sqwakvox.controller.pdfium.PdfDocument", _Pdf):
        assert pdf_has_text_layer("scan.pdf") is False


# --------------------------------------------------------------------------- #
# converter selection inside convert_document
# --------------------------------------------------------------------------- #


def test_ocr_skipped_for_text_layer_pdf() -> None:
    """A text-layer PDF routes to the no-OCR converter."""
    controller = AppController()
    # Assigned directly, not through the setter: injecting marks the converter
    # as authoritative (see test_injected_converter_is_never_replaced), which
    # is exactly the behaviour this test needs to sidestep.
    default = _FakeConverter()
    controller._converter = default  # type: ignore[assignment]
    no_ocr = _FakeConverter()
    controller._no_ocr_converter = no_ocr  # type: ignore[assignment]

    with patch("sqwakvox.controller.pdf_has_text_layer", return_value=True):
        doc = controller.convert_document("digital.pdf", lambda: False)

    assert doc is not None
    assert no_ocr.calls, "expected the no-OCR converter to be used"
    assert default.calls == []


def test_ocr_kept_when_no_text_layer() -> None:
    """A scan keeps the default converter, so OCR still runs."""
    controller = AppController()
    default = _FakeConverter()
    controller._converter = default  # type: ignore[assignment]
    no_ocr = _FakeConverter()
    controller._no_ocr_converter = no_ocr  # type: ignore[assignment]

    with patch("sqwakvox.controller.pdf_has_text_layer", return_value=False):
        doc = controller.convert_document("scan.pdf", lambda: False)

    assert doc is not None
    assert default.calls, "expected the OCR converter to be used"
    assert no_ocr.calls == []


def test_injected_converter_is_never_replaced() -> None:
    """An explicitly injected converter wins over the OCR-skip toggle.

    Swapping in a real Docling instance here would run the slow path a test or
    custom pipeline deliberately opted out of.
    """
    controller = AppController()
    fake = _FakeConverter()
    controller.converter = fake

    with patch("sqwakvox.controller.pdf_has_text_layer", return_value=True):
        doc = controller.convert_document("digital.pdf", lambda: False)

    assert doc is not None
    assert fake.calls, "injected converter must be used verbatim"


def test_page_range_still_forwarded_on_ocr_skip() -> None:
    """The OCR skip must not interfere with incremental page slicing."""
    controller = AppController()
    default = _FakeConverter()
    controller._converter = default  # type: ignore[assignment]
    no_ocr = _FakeConverter()
    controller._no_ocr_converter = no_ocr  # type: ignore[assignment]

    with patch("sqwakvox.controller.pdf_has_text_layer", return_value=True):
        doc = controller.convert_document("digital.pdf", lambda: False, page_range=(1, 10))

    assert doc is not None
    assert no_ocr.calls[0][1].get("page_range") == (1, 10)
