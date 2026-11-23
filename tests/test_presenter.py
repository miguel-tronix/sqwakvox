import asyncio
from unittest.mock import MagicMock, patch

import pytest
from celery.result import AsyncResult

from sqwakvox.backend import tasks as tasks_mod
from sqwakvox.backend.celery_app import celery_app
from sqwakvox.models import StructuredDocument, TableData
from sqwakvox.presenter import Presenter, TaskHandle, TaskStatus


@pytest.fixture
def eager_backend() -> None:
    """Run Celery tasks eagerly (no broker) and keep task failures on the result."""
    old_always = celery_app.conf.task_always_eager
    old_propagate = celery_app.conf.task_eager_propagates
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    yield
    celery_app.conf.task_always_eager = old_always
    celery_app.conf.task_eager_propagates = old_propagate


@pytest.fixture
def async_backend() -> None:
    """Force the production (broker) code path in :meth:`Presenter.submit_task`."""
    old_always = celery_app.conf.task_always_eager
    celery_app.conf.task_always_eager = False
    yield
    celery_app.conf.task_always_eager = old_always


@pytest.mark.asyncio
async def test_unknown_task_name_raises() -> None:
    presenter = Presenter()
    try:
        with pytest.raises(ValueError):
            await presenter.submit_task("does_not_exist")
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("eager_backend")
async def test_parse_document_success_rehydrates() -> None:
    doc = StructuredDocument(
        file_name="a.pdf",
        raw_markdown="# Hello",
        tables=[TableData(headers=["A"], rows=[["1"]])],
    )
    fake_controller = MagicMock()
    fake_controller.convert_document.return_value = doc

    callbacks: list[tuple[TaskStatus, object]] = []
    presenter = Presenter()
    try:
        with patch.object(tasks_mod, "_get_controller", return_value=fake_controller):
            handle = await presenter.parse_document(
                source="a.pdf",
                on_complete=lambda status, payload: callbacks.append((status, payload)),
            )
        await handle.wait()

        status, payload = callbacks[-1]
        assert status == TaskStatus.SUCCESS
        assert isinstance(payload, StructuredDocument)
        assert payload.file_name == "a.pdf"
        assert payload.tables[0].headers == ["A"]
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("eager_backend")
async def test_parse_document_failure_passes_error_message() -> None:
    """Regression: the real error string must reach the view, not ``None``."""
    fake_controller = MagicMock()
    fake_controller.convert_document.side_effect = RuntimeError("parse exploded")

    callbacks: list[tuple[TaskStatus, object]] = []
    presenter = Presenter()
    try:
        with patch.object(tasks_mod, "_get_controller", return_value=fake_controller):
            handle = await presenter.parse_document(
                source="missing.pdf",
                on_complete=lambda status, payload: callbacks.append((status, payload)),
            )
        await handle.wait()

        assert handle.status == TaskStatus.FAILURE
        assert handle.error == "parse exploded"
        assert callbacks[-1] == (TaskStatus.FAILURE, "parse exploded")
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("eager_backend")
async def test_parse_document_cancelled_before_start_passes_none() -> None:
    """A revoke-before-start parse surfaces as SUCCESS-with-None, not a crash."""
    fake_controller = MagicMock()
    fake_controller.convert_document.return_value = None

    callbacks: list[tuple[TaskStatus, object]] = []
    presenter = Presenter()
    try:
        with patch.object(tasks_mod, "_get_controller", return_value=fake_controller):
            handle = await presenter.parse_document(
                source="a.pdf",
                on_complete=lambda status, payload: callbacks.append((status, payload)),
            )
        await handle.wait()

        assert callbacks[-1] == (TaskStatus.SUCCESS, None)
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("async_backend")
async def test_submit_task_backend_unavailable() -> None:
    """A broker that refuses the submission resolves to FAILURE with a hint."""
    errors: list[str] = []
    completions: list[tuple[TaskStatus, object]] = []
    presenter = Presenter()
    try:
        with patch.object(
            celery_app,
            "send_task",
            side_effect=ConnectionError("broker down"),
        ):
            handle = await presenter.submit_task(
                "convert_document",
                args=["x.pdf"],
                on_error=errors.append,
                on_complete=lambda status, payload: completions.append((status, payload)),
            )
        await handle.wait()

        assert handle.status == TaskStatus.FAILURE
        assert "Backend unavailable" in handle.error
        assert "run_worker" in handle.error
        assert errors == [handle.error]
        assert completions == [(TaskStatus.FAILURE, handle.error)]
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("async_backend")
async def test_submit_task_routes_to_requested_queue() -> None:
    """Per-document-tab routing: ``queue=`` must reach ``send_task``."""
    captured: dict[str, object] = {}

    def fake_send_task(_name: str, *_rest: object, **extra: object):
        captured["name"] = _name
        captured["queue"] = extra.get("queue")
        result = MagicMock(spec=AsyncResult)
        result.id = "task-123"
        result.ready.return_value = True
        result.state = "SUCCESS"
        result.get.return_value = {"ok": True}
        return result

    presenter = Presenter()
    try:
        with patch.object(celery_app, "send_task", side_effect=fake_send_task):
            handle = await presenter.submit_task(
                "convert_document",
                args=["x.pdf"],
                queue="sqwakvox.doc0",
            )
        await handle.wait()

        assert captured["name"] == "sqwakvox.backend.tasks.convert_document"
        assert captured["queue"] == "sqwakvox.doc0"
        assert handle.task_id == "task-123"
        assert handle.status == TaskStatus.SUCCESS
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("async_backend")
async def test_submit_task_without_queue_uses_default_routing() -> None:
    captured: dict[str, object] = {}

    def fake_send_task(_name: str, *_rest: object, **extra: object):
        captured["queue"] = extra.get("queue")
        result = MagicMock(spec=AsyncResult)
        result.id = "task-456"
        result.ready.return_value = True
        result.state = "SUCCESS"
        result.get.return_value = {"ok": True}
        return result

    presenter = Presenter()
    try:
        with patch.object(celery_app, "send_task", side_effect=fake_send_task):
            handle = await presenter.submit_task("convert_document", args=["x.pdf"])
        await handle.wait()

        assert captured["queue"] is None  # Celery default queue applies
        assert handle.status == TaskStatus.SUCCESS
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("eager_backend")
async def test_handle_wait_returns_terminal_status() -> None:
    doc = StructuredDocument(file_name="a.pdf", raw_markdown="# Hello")
    fake_controller = MagicMock()
    fake_controller.convert_document.return_value = doc

    presenter = Presenter()
    try:
        with patch.object(tasks_mod, "_get_controller", return_value=fake_controller):
            handle = await presenter.parse_document(source="a.pdf")
        status = await asyncio.wait_for(handle.wait(), timeout=5.0)
        assert status == TaskStatus.SUCCESS
        assert handle.status == TaskStatus.SUCCESS
    finally:
        await presenter.close()


@pytest.mark.asyncio
@patch("sqwakvox.presenter.DEFAULT_POLL_TIMEOUT", 0.5)
async def test_poll_timeout_marks_failure_without_hanging() -> None:
    """If a task never reaches a terminal state, the poll loop must resolve
    to FAILURE after DEFAULT_POLL_TIMEOUT instead of looping forever.
    """
    presenter = Presenter()
    try:
        result = MagicMock(spec=AsyncResult)
        result.id = "nonexistent-task-id"
        result.ready.return_value = False
        result.state = "PENDING"

        handle = TaskHandle(task_id="nonexistent-task-id", task_name="convert_document")
        errors: list[str] = []
        await presenter._poll(
            result,
            handle=handle,
            on_progress=None,
            on_complete=None,
            on_error=errors.append,
            poll_timeout=0.5,
        )
        assert handle.status == TaskStatus.FAILURE
        assert "Timed out waiting for task" in handle.error
        assert "after " in handle.error
        assert "s. " in handle.error
        assert len(errors) == 1
        assert "Timed out waiting for task" in errors[0]
    finally:
        await presenter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("eager_backend")
async def test_execute_agent_rehydrates_agent_result_with_filtered_keys() -> None:
    from sqwakvox.controller import AgentResult

    fake_controller = MagicMock()
    res = AgentResult(response="Hello chart", success=True)
    res_dict = res.__dict__.copy()
    res_dict["extra_unrecognized_key"] = "extra_value"
    fake_controller.execute_agent.return_value = res_dict

    callbacks: list[tuple[TaskStatus, object]] = []
    presenter = Presenter()
    try:
        with patch.object(tasks_mod, "_get_controller", return_value=fake_controller):
            handle = await presenter.execute_agent(
                model_id="gpt-4o",
                api_key="key",
                user_query="hi",
                doc_context="",
                active_document_name="",
                data_store={},
                on_complete=lambda status, payload: callbacks.append((status, payload)),
            )
        await handle.wait()

        assert len(callbacks) == 1
        status, payload = callbacks[0]
        assert status == TaskStatus.SUCCESS
        assert isinstance(payload, AgentResult)
        assert payload.response == "Hello chart"
        assert not hasattr(payload, "extra_unrecognized_key")
    finally:
        await presenter.close()


@pytest.mark.asyncio
async def test_poll_emits_started_progress_only_once() -> None:
    """_poll should notify TaskStatus.STARTED once, not on every 0.5s tick."""
    presenter = Presenter()
    try:
        result = MagicMock(spec=AsyncResult)
        result.id = "task-progress-once"
        polls = 0

        def is_ready() -> bool:
            nonlocal polls
            polls += 1
            if polls > 3:
                result.state = "SUCCESS"
                return True
            result.state = "STARTED"
            return False

        result.ready.side_effect = is_ready
        result.get.return_value = {"ok": True}

        handle = TaskHandle(task_id="task-progress-once", task_name="convert_document")
        progress_events: list[TaskStatus] = []
        with patch("sqwakvox.presenter.POLL_INTERVAL", 0.01):
            await presenter._poll(
                result,
                handle=handle,
                on_progress=lambda status, _payload: progress_events.append(status),
                on_complete=None,
                on_error=None,
            )
        assert progress_events == [TaskStatus.STARTED]
    finally:
        await presenter.close()
