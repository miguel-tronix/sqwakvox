import asyncio

import pytest
from textual.widgets import ListView, Select, Tab, Tabs

from sqwakvox.app import SqwakvoxApp
from sqwakvox.models import StructuredDocument
from sqwakvox.presenter import TaskStatus
from sqwakvox.worker_manager import DOCLING_QUEUE


def _doc1() -> StructuredDocument:
    return StructuredDocument(file_name="doc1.pdf", raw_markdown="# Document 1 Content")


def _doc2() -> StructuredDocument:
    return StructuredDocument(file_name="doc2.pdf", raw_markdown="# Document 2 Content")


@pytest.mark.asyncio
async def test_multi_document_tabs_creation_and_switching() -> None:
    app = SqwakvoxApp()
    async with app.run_test() as pilot:
        doc_a = _doc1()
        doc_b = _doc2()

        # Ingest first document
        app._on_parse_success(doc_a, "doc1.pdf")
        await pilot.pause()

        tabs = app.query_one("#document-tabs", Tabs)
        tab_ids = [t.id for t in tabs.query(Tab)]
        assert tab_ids == ["tab_0"]
        assert tabs.active == "tab_0"
        assert app.active_document_name == "doc1.pdf"

        # Ingest second document — must not raise DuplicateIds error
        app._on_parse_success(doc_b, "doc2.pdf")
        await pilot.pause()

        tab_ids = [t.id for t in tabs.query(Tab)]
        assert tab_ids == ["tab_0", "tab_1"]
        assert tabs.active == "tab_1"
        assert app.active_document_name == "doc2.pdf"

        # Switch back to first document by activating tab_0
        tabs.active = "tab_0"
        await pilot.pause()

        assert app.active_document_name == "doc1.pdf"
        assert app.doc_context == "# Document 1 Content"

        # Switch to second document via Ingest History ListView
        history_list = app.query_one("#ingest-history", ListView)
        history_list.index = 1
        history_list.action_select_cursor()
        await pilot.pause()

        assert app.active_document_name == "doc2.pdf"
        assert tabs.active == "tab_1"


def _stub_parse(
    monkeypatch: pytest.MonkeyPatch, app: SqwakvoxApp, doc: StructuredDocument
) -> list[tuple[str, str | None]]:
    """Replace presenter.parse_document with a fake that completes instantly."""
    routed: list[tuple[str, str | None]] = []

    class FakeHandle:
        status = TaskStatus.SUCCESS
        result = None
        error = None

        async def wait(self, _timeout: float | None = None) -> TaskStatus:
            return TaskStatus.SUCCESS

    async def fake_parse(source: str, **kwargs):
        routed.append((source, kwargs.get("queue")))
        if kwargs.get("on_complete") is not None:
            kwargs["on_complete"](TaskStatus.SUCCESS, doc)
        return FakeHandle()

    monkeypatch.setattr(app.presenter, "parse_document", fake_parse)
    _stub_postprocess(monkeypatch, app)
    return routed


def _stub_postprocess(monkeypatch: pytest.MonkeyPatch, app: SqwakvoxApp) -> None:
    """Replace presenter.postprocess_document with an instant, empty success."""

    class PPHandle:
        status = TaskStatus.SUCCESS

        def __init__(self) -> None:
            self.result: dict[str, object] = {}

        async def wait(self, _timeout: float | None = None) -> TaskStatus:
            return TaskStatus.SUCCESS

    async def fake_postprocess(**_kwargs) -> PPHandle:
        return PPHandle()

    monkeypatch.setattr(app.presenter, "postprocess_document", fake_postprocess)


@pytest.mark.asyncio
async def test_document_load_routes_parse_to_shared_docling_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parsing must route to the *shared* Docling queue and ensure the shared
    docling worker, while each tab still gets its own agent queue/worker."""
    app = SqwakvoxApp()
    async with app.run_test() as pilot:
        ensured: list[str] = []
        monkeypatch.setattr(
            app.worker_manager,
            "ensure_worker",
            lambda queue: ensured.append(queue) or True,
        )
        docling_ensured: list[bool] = []
        monkeypatch.setattr(
            app.worker_manager,
            "ensure_docling_worker",
            lambda: docling_ensured.append(True) or True,
        )

        routed1 = _stub_parse(monkeypatch, app, _doc1())
        await app._dispatch_parse("/tmp/doc1.pdf")
        await pilot.pause()  # flush queued tab-activation messages

        # Tab 1 gets its own agent queue AND the shared docling worker is
        # ensured; the parse task itself goes to the docling queue.
        assert ensured == ["sqwakvox.doc0"]
        assert docling_ensured == [True]
        assert routed1 == [("/tmp/doc1.pdf", DOCLING_QUEUE)]
        assert app.active_document_name == "doc1.pdf"

        routed2 = _stub_parse(monkeypatch, app, _doc2())
        await app._dispatch_parse("/tmp/doc2.pdf")
        await pilot.pause()  # flush queued tab-activation messages

        # Tab 2 gets its own agent queue; the docling worker is shared, so it
        # is only ensured (never re-spawned) — the real WorkerManager treats
        # the second ensure as a no-op.
        assert ensured == ["sqwakvox.doc0", "sqwakvox.doc1"]
        assert docling_ensured == [True, True]
        assert routed2 == [("/tmp/doc2.pdf", DOCLING_QUEUE)]
        assert app.active_document_name == "doc2.pdf"


@pytest.mark.asyncio
async def test_active_queue_follows_selected_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat/cross-validate tasks route to the active document's agent worker."""
    app = SqwakvoxApp()
    async with app.run_test() as pilot:
        ensured: list[str] = []
        monkeypatch.setattr(
            app.worker_manager,
            "ensure_worker",
            lambda queue, **_kwargs: ensured.append(queue) or True,
        )

        _stub_parse_routed1 = _stub_parse(monkeypatch, app, _doc1())
        await app._dispatch_parse("/tmp/doc1.pdf")
        _stub_parse_routed2 = _stub_parse(monkeypatch, app, _doc2())
        await app._dispatch_parse("/tmp/doc2.pdf")

        assert app._active_queue() == "sqwakvox.doc1"  # doc2 loaded last

        tabs = app.query_one("#document-tabs", Tabs)
        tabs.active = "tab_0"
        await pilot.pause()

        assert app.active_document_name == "doc1.pdf"
        assert app._active_queue() == "sqwakvox.doc0"


@pytest.mark.asyncio
async def test_managed_workers_disabled_falls_back_to_default_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQWAKVOX_MANAGED_WORKERS=0 restores bring-your-own-worker behaviour."""
    monkeypatch.setenv("SQWAKVOX_MANAGED_WORKERS", "0")
    app = SqwakvoxApp()
    async with app.run_test() as pilot:
        ensured: list[str] = []
        docling_ensured: list[bool] = []
        monkeypatch.setattr(
            app.worker_manager,
            "ensure_worker",
            lambda queue: ensured.append(queue) or True,
        )
        monkeypatch.setattr(
            app.worker_manager,
            "ensure_docling_worker",
            lambda: docling_ensured.append(True) or True,
        )

        routed = _stub_parse(monkeypatch, app, _doc1())
        await app._dispatch_parse("/tmp/doc1.pdf")
        await pilot.pause()  # flush queued tab-activation messages

        assert ensured == []  # no per-tab worker spawned
        assert docling_ensured == []  # no shared docling worker spawned
        assert routed == [("/tmp/doc1.pdf", None)]  # default Celery queue


def _stub_parse_with_domain(
    monkeypatch: pytest.MonkeyPatch, app: SqwakvoxApp, doc: StructuredDocument
) -> list[tuple[str, str | None, str]]:
    """Like _stub_parse, but also captures the domain_id passed to parse_document."""
    routed: list[tuple[str, str | None, str]] = []

    class FakeHandle:
        status = TaskStatus.SUCCESS
        result = None
        error = None

        async def wait(self, _timeout: float | None = None) -> TaskStatus:
            return TaskStatus.SUCCESS

    async def fake_parse(source: str, **kwargs):
        routed.append((source, kwargs.get("queue"), kwargs.get("domain_id", "financial")))
        if kwargs.get("on_complete") is not None:
            kwargs["on_complete"](TaskStatus.SUCCESS, doc)
        return FakeHandle()

    monkeypatch.setattr(app.presenter, "parse_document", fake_parse)
    _stub_postprocess(monkeypatch, app)
    return routed


@pytest.mark.asyncio
async def test_swe_domain_selection_routes_parse_and_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting the SWE expert type must route the parse with domain_id='swe'
    and tag the loaded document so chat/rendering use the SWE domain."""
    app = SqwakvoxApp()
    async with app.run_test() as pilot:
        ensured: list[str] = []
        monkeypatch.setattr(
            app.worker_manager,
            "ensure_worker",
            lambda queue, **_kwargs: ensured.append(queue) or True,
        )
        monkeypatch.setattr(
            app.worker_manager,
            "ensure_docling_worker",
            lambda: True,
        )

        # Select Software Engineering in the sidebar.
        selector = app.query_one("#domain-selector", Select)
        selector.value = "swe"
        await pilot.pause()

        routed = _stub_parse_with_domain(monkeypatch, app, _doc1())
        await app._dispatch_parse("/tmp/book.epub", "swe")

        assert routed == [("/tmp/book.epub", "sqwakvox.docling", "swe")]
        assert app._doc_domains["/tmp/book.epub"] == "swe"

        # The loaded document keeps its domain for chat dispatch + rendering.
        assert app._active_domain_id() == "swe"
        loaded = app.loaded_documents["/tmp/book.epub"]
        assert loaded.domain_id == "swe"
        assert loaded.file_name == "doc1.pdf"


@pytest.mark.asyncio
async def test_active_domain_defaults_to_financial() -> None:
    """Before any document loads, the active domain is financial."""
    app = SqwakvoxApp()
    async with app.run_test():
        assert app._active_domain_id() == "financial"


@pytest.mark.asyncio
async def test_parse_flow_merges_postprocess_payload_before_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After conversion, the domain post-parse payload (TOC, flags, ...) must
    be merged into the document's metadata before the tab switches, so the
    renderer/agent context see the full picture."""
    app = SqwakvoxApp()
    async with app.run_test() as pilot:
        monkeypatch.setattr(app.worker_manager, "ensure_worker", lambda _queue, **_k: True)
        monkeypatch.setattr(app.worker_manager, "ensure_docling_worker", lambda: True)

        _stub_parse(monkeypatch, app, _doc1())

        # Post-parse returns a SWE-style payload with an injection flag.
        payload = {
            "toc": [{"level": 1, "title": "Intro"}],
            "code_blocks": [],
            "injection_flags": ["ignore previous instructions"],
            "source_type": "epub",
            "needs_retrieval": True,
            "chunk_count": 12,
        }

        class PPHandle:
            status = TaskStatus.SUCCESS

            def __init__(self) -> None:
                self.result: dict[str, object] = payload

            async def wait(self, _timeout: float | None = None) -> TaskStatus:
                return TaskStatus.SUCCESS

        async def fake_postprocess(**_kwargs) -> PPHandle:
            return PPHandle()

        monkeypatch.setattr(app.presenter, "postprocess_document", fake_postprocess)

        await app._dispatch_parse("/tmp/book.epub", "swe")
        await pilot.pause()  # flush queued tab-activation messages

        # Payload merged into the stored document's metadata.
        loaded = app.loaded_documents["/tmp/book.epub"]
        assert loaded.structured.metadata["needs_retrieval"] is True
        assert loaded.structured.metadata["injection_flags"] == ["ignore previous instructions"]
        # Tab switched.
        assert app.active_document_name == "doc1.pdf"


@pytest.mark.asyncio
async def test_dispatch_parse_waits_for_async_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the real presenter fires on_complete *after* parse_document
    returns (async polling).  _dispatch_parse must wait for the handle and
    only then switch tabs — it must not report "Parse failed" immediately."""
    app = SqwakvoxApp()
    async with app.run_test() as pilot:
        monkeypatch.setattr(app.worker_manager, "ensure_worker", lambda _queue, **_k: True)
        monkeypatch.setattr(app.worker_manager, "ensure_docling_worker", lambda: True)
        _stub_postprocess(monkeypatch, app)

        class DelayedHandle:
            status = TaskStatus.PENDING

            def __init__(self, event: asyncio.Event) -> None:
                self._event = event

            async def wait(self, _timeout: float | None = None) -> TaskStatus:
                await self._event.wait()
                return self.status

        event = asyncio.Event()
        pending_tasks: list[asyncio.Task[None]] = []

        async def fake_parse(**_kwargs):
            async def _complete() -> None:
                await asyncio.sleep(0.05)  # mimic async poll resolution
                _kwargs["on_complete"](TaskStatus.SUCCESS, _doc1())
                event.set()

            pending_tasks.append(asyncio.ensure_future(_complete()))
            return DelayedHandle(event)

        monkeypatch.setattr(app.presenter, "parse_document", fake_parse)

        await app._dispatch_parse("/tmp/late.pdf", "swe")
        await pilot.pause()  # flush queued tab-activation messages

        # The document only lands after the delayed completion — no premature
        # failure, no "Parse failed." path.
        assert app.active_document_name == "doc1.pdf"
        assert app.active_error is None
        assert app.loaded_documents["/tmp/late.pdf"].file_name == "doc1.pdf"


@pytest.mark.asyncio
async def test_doc_pager_row_height_and_tab_switching() -> None:
    """Pager row must only take button height (not half the screen),
    and view-tabs switching must toggle doc-view-container."""
    app = SqwakvoxApp()
    async with app.run_test(size=(120, 40)) as pilot:
        container = app.query_one("#doc-view-container")
        render_pane = app.query_one("#render-pane")
        pager_row = app.query_one("#doc-pager-row")
        btn = app.query_one("#btn-load-more")
        agent_pane = app.query_one("#agent-response-pane")

        # Pager row must only take the height of the button (3 cells),
        # leaving the bulk of the container for the render pane.
        assert pager_row.region.height == btn.region.height
        assert pager_row.region.height < container.region.height // 2
        assert render_pane.region.height > container.region.height // 2

        # View tabs switching toggles doc-view-container and agent-response-pane
        view_tabs = app.query_one("#view-tabs", Tabs)
        view_tabs.active = "view-agent"
        await pilot.pause()

        assert container.styles.display == "none"
        assert agent_pane.styles.display == "block"

        view_tabs.active = "view-doc"
        await pilot.pause()

        assert container.styles.display == "block"
        assert agent_pane.styles.display == "none"

