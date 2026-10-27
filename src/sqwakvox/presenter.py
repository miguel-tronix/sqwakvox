"""MVP Presenter layer for Sqwakvox.

The presenter sits between the **View** (Textual TUI — currently
``sqwakvox.app``, or a future HTML/Flask presenter) and the **Backend**
(Celery worker running ``sqwakvox.backend.tasks``).

Responsibilities
----------------
* Submit Celery tasks and hand the caller a :class:`TaskHandle`.
* Asynchronously poll ``AsyncResult`` for state, delivering progress and
  completion callbacks to the view.
* Deserialise broker-safe dicts back into domain objects.
* Revoke / cancel in-flight tasks on demand.
* Provide an :meth:`asyncio.Event`-style ``wait()`` on every handle so views
  can ``await handle.wait()`` before proceeding.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

from celery.result import AsyncResult

from sqwakvox.backend.celery_app import celery_app
from sqwakvox.controller import AgentResult, extract_message
from sqwakvox.models import StructuredDocument

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # seconds
# Absolute ceiling on how long the presenter will poll a single task for,
# matching the Celery ``task_time_limit`` (1860 s hard kill, see
# ``backend/celery_app.py``) plus a grace margin.  Override via
# ``SQWAKVOX_PRESENTER_POLL_TIMEOUT`` for longer documents or to tune for a
# different environment.
DEFAULT_POLL_TIMEOUT = float(os.environ.get("SQWAKVOX_PRESENTER_POLL_TIMEOUT", 1920))


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    REVOKED = "REVOKED"
    CANCELLED = "CANCELLED"  # presenter-side alias for REVOKED


@dataclass
class TaskHandle:
    """Opaque handle returned by the presenter for an async task.

    Call :meth:`wait` (awaitable) to block the caller until the task reaches
    a terminal state, or inspect :attr:`status` / :attr:`result` directly.
    """

    task_id: str
    task_name: str
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str | None = None

    async def wait(self, timeout: float | None = None) -> TaskStatus:
        """Block until the task completes or *timeout* elapses."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        return self.status


class Presenter:
    """Async facade over the Celery backend.

    Instantiate once per view session and call :meth:`close` on shutdown.
    All public methods are coroutines designed to be called from an async
    event loop (e.g. inside a Textual ``run_worker`` coroutine).
    """

    def __init__(self) -> None:
        self._active_polls: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ #
    # Task submission
    # ------------------------------------------------------------------ #
    async def submit_task(
        self,
        task_name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        *,
        queue: str | None = None,
        on_progress: Callable[[TaskStatus, Any], None] | None = None,
        on_complete: Callable[[TaskStatus, Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> TaskHandle:
        """Submit *task_name* and return a :class:`TaskHandle`.

        ``queue`` optionally routes the task to a specific Celery queue
        (the shared ``sqwakvox.docling`` queue for document conversion, or a
        per-document-tab queue for agent work — see
        :mod:`sqwakvox.worker_manager`); ``None`` uses the app default.

        ``on_progress`` fires with every poll (status + raw payload).
        ``on_complete`` fires once with the final status + result.
        ``on_error`` fires with a human-readable error string on failure.

        If the broker/result-backend is unreachable the task is never
        submitted; the returned handle resolves immediately to
        :attr:`TaskStatus.FAILURE` and both ``on_error`` and ``on_complete``
        are invoked with a human-readable message.
        """
        from sqwakvox.backend import tasks as tasks_mod

        fn = getattr(tasks_mod, task_name, None)
        if fn is None:
            raise ValueError(f"Unknown task name: {task_name}")

        loop = asyncio.get_event_loop()

        try:
            # In eager mode (no broker running), apply_async runs synchronously
            # and the task body's own exceptions propagate to the caller.
            # In production, defer the (potentially blocking) broker call to a
            # thread executor so the asyncio loop isn't blocked, and treat a
            # failure to submit as a backend-unavailable signal.
            #
            # Submit via ``celery_app.send_task`` (not the shared task's
            # ``apply_async``): celery's current_app is thread-local, and in
            # the executor thread the shared task resolves to the broker-less
            # default app (pyamqp on port 5672) -> Connection refused.  The
            # explicit app always carries our Redis broker URL.
            if celery_app.conf.task_always_eager:
                result = fn.apply_async(args=args or [], kwargs=kwargs or {})
            else:
                # Use the task's registered name (``fn.name``), not the short
                # lookup key: the worker registers tasks under their full
                # ``sqwakvox.backend.tasks.*`` names.
                send_kwargs: dict[str, Any] = {}
                if queue is not None:
                    send_kwargs["queue"] = queue
                result = await loop.run_in_executor(
                    None,
                    lambda: celery_app.send_task(
                        fn.name, args=args or [], kwargs=kwargs or {}, **send_kwargs
                    ),
                )
        except Exception as exc:
            if celery_app.conf.task_always_eager:
                raise
            logger.error("Failed to submit Celery task %s: %s", task_name, exc)
            return self._failed_handle(
                task_name,
                on_complete=on_complete,
                on_error=on_error,
            )

        handle = TaskHandle(task_id=result.id, task_name=task_name)
        poll_task = asyncio.ensure_future(
            self._poll(
                result,
                handle=handle,
                on_progress=on_progress,
                on_complete=on_complete,
                on_error=on_error,
            )
        )
        self._active_polls[result.id] = poll_task
        return handle

    def _failed_handle(
        self,
        task_name: str,
        *,
        on_complete: Callable[[TaskStatus, Any], None] | None,
        on_error: Callable[[str], None] | None,
    ) -> TaskHandle:
        """Build a handle that resolves immediately to FAILURE.

        Used when a task could not be submitted (e.g. the broker is down) so
        callers awaiting ``handle.wait()`` and their callbacks still fire.
        """
        message = (
            f"Backend unavailable: could not submit task '{task_name}'. "
            "Is Redis running, and is a Celery worker available? "
            "The TUI normally spawns its own workers per document tab; "
            "to run one manually instead set SQWAKVOX_MANAGED_WORKERS=0 "
            "and start it with: python -m sqwakvox.run_worker"
        )
        handle = TaskHandle(task_id="", task_name=task_name)
        handle.status = TaskStatus.FAILURE
        handle.error = message
        if on_error is not None:
            on_error(message)
        if on_complete is not None:
            on_complete(TaskStatus.FAILURE, message)
        handle._event.set()
        return handle

    # ------------------------------------------------------------------ #
    # Convenience wrappers (typed, deserialise broker results)
    # ------------------------------------------------------------------ #
    async def parse_document(
        self,
        source: str,
        *,
        domain_id: str = "financial",
        options: dict[str, Any] | None = None,
        page_range: tuple[int, int] | None = None,
        queue: str | None = None,
        on_progress: Callable[[TaskStatus, Any], None] | None = None,
        on_complete: Callable[[TaskStatus, StructuredDocument | None], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> TaskHandle:
        """Submit a document parse and deserialise the result into a
        :class:`StructuredDocument`.

        ``options`` is passed to the domain's ingest plan (e.g. ``crawl`` /
        ``max_pages`` for SWE docs sites).  ``page_range`` restricts a PDF
        conversion to a 1-based ``[start, end]`` slice so big documents load
        incrementally.
        """

        def _on_complete(status: TaskStatus, payload: Any) -> None:
            if on_complete is not None:
                if status == TaskStatus.SUCCESS and isinstance(payload, dict):
                    on_complete(
                        TaskStatus.SUCCESS,
                        StructuredDocument.model_validate(payload),
                    )
                else:
                    on_complete(status, payload)

        kwargs: dict[str, Any] = {"domain_id": domain_id}
        if options:
            kwargs["options"] = options
        if page_range is not None:
            kwargs["page_range"] = list(page_range)
        return await self.submit_task(
            "convert_document",
            args=[source],
            kwargs=kwargs,
            queue=queue,
            on_progress=on_progress,
            on_complete=_on_complete,
            on_error=on_error,
        )

    async def postprocess_document(
        self,
        domain_id: str,
        document: StructuredDocument,
        *,
        queue: str | None = None,
        on_complete: Callable[[TaskStatus, dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> TaskHandle:
        """Run the active domain's post-parse step (data store, TOC, ...)."""
        return await self.submit_task(
            "domain_postprocess",
            args=[domain_id, document.model_dump()],
            queue=queue,
            on_complete=on_complete,
            on_error=on_error,
        )

    async def build_data_store(
        self,
        document: StructuredDocument,
        *,
        queue: str | None = None,
        on_complete: Callable[[TaskStatus, dict[str, str]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> TaskHandle:
        """Financial-domain data store (kept for backward compatibility)."""
        return await self.submit_task(
            "build_financial_data_store",
            args=[document.model_dump()],
            queue=queue,
            on_complete=on_complete,
            on_error=on_error,
        )

    # Type alias for cross-validation results.
    CVResult = list[tuple[str, float, float, bool]]

    async def cross_validate(
        self,
        document: StructuredDocument,
        *,
        queue: str | None = None,
        on_complete: Callable[[TaskStatus, CVResult], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> TaskHandle:
        return await self.submit_task(
            "cross_validate",
            args=[document.model_dump()],
            queue=queue,
            on_complete=on_complete,
            on_error=on_error,
        )

    async def execute_agent(
        self,
        model_id: str,
        api_key: str,
        user_query: str,
        doc_context: str,
        active_document_name: str,
        data_store: dict[str, str],
        mcp_servers: list[dict[str, Any]] | None = None,
        thread_id: str | None = None,
        domain_id: str = "financial",
        *,
        queue: str | None = None,
        on_progress: Callable[[TaskStatus, Any], None] | None = None,
        on_complete: Callable[[TaskStatus, AgentResult], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> TaskHandle:
        def _on_complete(status: TaskStatus, payload: Any) -> None:
            if on_complete is not None:
                if status == TaskStatus.SUCCESS and isinstance(payload, dict):
                    valid_keys = {f.name for f in fields(AgentResult)}
                    filtered = {k: v for k, v in payload.items() if k in valid_keys}
                    on_complete(TaskStatus.SUCCESS, AgentResult(**filtered))
                else:
                    on_complete(status, payload)

        return await self.submit_task(
            "execute_agent",
            args=[
                model_id,
                api_key,
                user_query,
                doc_context,
                active_document_name,
                data_store,
                mcp_servers,
                thread_id,
            ],
            kwargs={"domain_id": domain_id},
            queue=queue,
            on_progress=on_progress,
            on_complete=_on_complete,
            on_error=on_error,
        )

    # ------------------------------------------------------------------ #
    # Cancellation
    # ------------------------------------------------------------------ #
    async def cancel_task(self, task_id: str) -> bool:
        """Revoke a running task.  Returns True if the revoke was sent."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: celery_app.control.revoke(task_id))
        except Exception as exc:
            logger.warning("Failed to revoke task %s: %s", task_id, exc)
            return False
        if task_id in self._active_polls:
            self._active_polls[task_id].cancel()
            self._active_polls.pop(task_id, None)
        return True

    # ------------------------------------------------------------------ #
    # Polling engine
    # ------------------------------------------------------------------ #
    async def _poll(
        self,
        result: AsyncResult,
        *,
        handle: TaskHandle,
        on_progress: Callable[[TaskStatus, Any], None] | None,
        on_complete: Callable[[TaskStatus, Any], None] | None,
        on_error: Callable[[str], None] | None,
        poll_timeout: float | None = None,
    ) -> None:
        """Poll *result* until it reaches a terminal state, then resolve the
        handle's event so callers awaiting ``handle.wait()`` wake up.

        ``poll_timeout`` is an absolute ceiling on polling duration (seconds).
        It defaults to :data:`DEFAULT_POLL_TIMEOUT` (660 s — Celery's 600 s
        hard timeout plus a grace margin).  If the task never reaches a
        terminal state within this window, the handle is marked as FAILURE
        with a timeout message so the view doesn't hang forever.
        """
        deadline = asyncio.get_event_loop().time() + (
            poll_timeout if poll_timeout is not None else DEFAULT_POLL_TIMEOUT
        )
        try:
            while True:
                if result.ready() or result.state in (
                    "SUCCESS",
                    "FAILURE",
                    "REVOKED",
                ):
                    status = self._map_state(result.state)
                    handle.status = status

                    if status == TaskStatus.SUCCESS:
                        loop = asyncio.get_event_loop()
                        payload = await loop.run_in_executor(
                            None, lambda: result.get(propagate=False)
                        )
                        handle.result = payload
                        if on_complete is not None:
                            on_complete(status, payload)

                    elif status == TaskStatus.FAILURE:
                        loop = asyncio.get_event_loop()
                        exc = await loop.run_in_executor(
                            None,
                            lambda: result.result if result.failed() else None,
                        )
                        err_msg = (
                            extract_message(str(exc))
                            if exc
                            else result.traceback or "Unknown failure"
                        )
                        handle.status = TaskStatus.FAILURE
                        handle.error = err_msg
                        if on_progress is not None:
                            on_progress(TaskStatus.FAILURE, None)
                        if on_error is not None:
                            on_error(err_msg)
                        if on_complete is not None:
                            on_complete(TaskStatus.FAILURE, err_msg)

                    elif status == TaskStatus.REVOKED:
                        handle.status = TaskStatus.REVOKED
                        if on_complete is not None:
                            on_complete(TaskStatus.REVOKED, None)

                    break

                # Still running — check the absolute deadline before sleeping.
                if asyncio.get_event_loop().time() > deadline:
                    elapsed = DEFAULT_POLL_TIMEOUT if poll_timeout is None else poll_timeout
                    msg = (
                        f"Timed out waiting for task {handle.task_name} "
                        f"({handle.task_id}) after {elapsed:.0f}s. "
                        "The backend worker may have been killed by its "
                        "hard time limit, or the result backend is unreachable."
                    )
                    logger.error(msg)
                    handle.status = TaskStatus.FAILURE
                    handle.error = msg
                    if on_error is not None:
                        on_error(msg)
                    if on_complete is not None:
                        on_complete(TaskStatus.FAILURE, msg)
                    break

                if on_progress is not None:
                    on_progress(TaskStatus.STARTED, None)
                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            handle.status = TaskStatus.CANCELLED
            if on_complete is not None:
                on_complete(TaskStatus.CANCELLED, None)
            raise
        except Exception as exc:
            logger.warning("Polling loop for %s errored: %s", handle.task_id, exc)
            handle.status = TaskStatus.FAILURE
            handle.error = str(exc)
            if on_error is not None:
                on_error(str(exc))
        finally:
            self._active_polls.pop(handle.task_id, None)
            handle._event.set()

    @staticmethod
    def _map_state(state: str) -> TaskStatus:
        """Map a Celery state string to a :class:`TaskStatus`."""
        mapping = {
            "PENDING": TaskStatus.PENDING,
            "STARTED": TaskStatus.STARTED,
            "SUCCESS": TaskStatus.SUCCESS,
            "FAILURE": TaskStatus.FAILURE,
            "REVOKED": TaskStatus.REVOKED,
            "RETRY": TaskStatus.PENDING,
        }
        return mapping.get(state, TaskStatus.PENDING)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        """Cancel all active polls and release resources."""
        polls = list(self._active_polls.values())
        self._active_polls.clear()
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None

        for poll in polls:
            try:
                owner = poll.get_loop()
            except Exception:
                owner = None
            if current is not None and owner is current:
                poll.cancel()
            else:
                logger.warning(
                        "cancelling polling task %s from a dead loop",
                        getattr(poll,"get_name",lambda: "?")() if hasattr(poll,"get_name") else "?",
                )
                with contextlib.suppress(RuntimeError):
                    poll.cancel()
