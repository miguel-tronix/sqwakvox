"""View-agnostic document session — the shared presenter surface.

:class:`DocSession` owns everything the Textual TUI used to own inline in
``SqwakvoxApp``: the Celery :class:`~sqwakvox.presenter.Presenter`, the managed
worker pool, the MCP server configs, the per-document tab registry, the chat
history, and the page-slice bookkeeping for large PDFs.

Nothing in this module touches a widget.  Views attach to the
:attr:`DocSession.events` bus and re-render whatever arrives, which is how the
TUI (``sqwakvox.app``), the web presenter (``sqwakvox.web_presenter``), and the
MCP presenter server (``sqwakvox.mcp_presenter``) all drive the exact same
code path.

Threading model
---------------
The session owns a private asyncio loop running on a daemon thread.  Every
public method is therefore synchronous and callable from a Textual worker, an
MCP stdio thread, or a Starlette worker thread, while the async work
(``parse_document`` → ``postprocess_document`` → ``execute_agent``) runs on
that loop.  Long operations return a ``job_id`` immediately and report progress
through :class:`SessionEvent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqwakvox import session_registry
from sqwakvox.controller import AgentResult, extract_message
from sqwakvox.domains import get_domain, list_domains
from sqwakvox.domains.base import LoadedDocument
from sqwakvox.domains.swe import skills as swe_skills
from sqwakvox.guardrails import AuditLogger
from sqwakvox.models import ModelProvider, StructuredDocument
from sqwakvox.presenter import Presenter, TaskStatus
from sqwakvox.worker_manager import DOCLING_QUEUE, WorkerManager, managed_workers_enabled

logger = logging.getLogger(__name__)
chat_logger = logging.getLogger("sqwakvox.chat")

#: Pages fetched per slice when a PDF is loaded incrementally.  Mirrors the
#: TUI's ``SQWAKVOX_PDF_BATCH_SIZE``.
PDF_BATCH_SIZE = int(os.environ.get("SQWAKVOX_PDF_BATCH_SIZE", "10"))

#: Default model used when a caller does not pick one.
DEFAULT_MODEL_ID = "openai:gpt-5.5-high"

#: Terminal job states.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_FAILURE = "failure"
JOB_CANCELLED = "cancelled"

_render_console_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionEvent:
    """One entry on the session event bus.

    ``kind`` drives view routing; ``data`` carries the payload.  ``seq`` is
    monotonic per session so a reconnecting SSE client can resume with
    ``since_seq`` instead of losing events.
    """

    seq: int
    kind: str
    data: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "ts": self.ts, "data": self.data}


#: A sink is any callable accepting a :class:`SessionEvent`.
EventSink = Callable[[SessionEvent], None]

# Event kinds emitted by :class:`DocSession`.
EVENT_STATUS = "status"
EVENT_PROGRESS = "progress"
EVENT_DOCUMENT = "document"
EVENT_CHAT = "chat"
EVENT_RENDER = "render"
EVENT_JOB = "job"
EVENT_ERROR = "error"
EVENT_LOG = "log"


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    """A long-running background operation the session is tracking."""

    job_id: str
    kind: str
    source: str
    created_at: float = field(default_factory=time.time)
    state: str = JOB_PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    future: Future[Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "source": self.source,
            "state": self.state,
            "created_at": self.created_at,
            "result": self.result,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _strip_markup(markup: str) -> str:
    """Best-effort Rich-markup -> plain-text conversion."""
    try:
        from rich.text import Text

        return Text.from_markup(markup).plain
    except Exception:  # pragma: no cover — markup is best effort
        return re.sub(r"\[/?[a-zA-Z0-9_#=\s.,()]*\]", "", markup)


def render_document_text(doc: StructuredDocument, domain_id: str, width: int = 110) -> str:
    """Render *doc* with its domain renderer and return plain terminal text.

    The domain renderers emit Rich markup (see
    :meth:`sqwakvox.renderer.DocumentRenderPane.update_document`); rendering it
    through a headless Rich console gives the web view exactly what the TUI
    shows, minus colour.
    """
    domain = get_domain(domain_id)
    markup = (
        domain.render(doc)
        if domain.render is not None
        else f"{doc.file_name}\n\n{doc.raw_markdown}"
    )
    try:
        from rich.console import Console

        buffer = io.StringIO()
        console = Console(
            file=buffer,
            width=width,
            no_color=True,
            highlight=False,
            legacy_windows=False,
            emoji=False,
        )
        with console.capture() as capture, _render_console_lock:
            console.print(markup)
        return capture.get().rstrip("\n")
    except Exception:  # pragma: no cover — fall back to plain text
        return _strip_markup(markup)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


class DocSession:
    """Shared, view-agnostic document session.

    Usage::

        session = DocSession()
        job = session.open_document("book.epub", "swe")
        session.wait_job(job)          # blocks; raises on failure
        reply = session.ask_and_wait("summarise chapter 1", "openai:gpt-5.5-high")
    """

    def __init__(
        self,
        presenter: Presenter | None = None,
        worker_manager: WorkerManager | None = None,
        *,
        chat_log_dir: Path | None = None,
        pdf_batch_size: int = PDF_BATCH_SIZE,
        owns_presenter: bool | None = None,
    ) -> None:
        self.presenter = presenter if presenter is not None else Presenter()
        self._owns_presenter = owns_presenter if owns_presenter is not None else presenter is None
        self.worker_manager = worker_manager if worker_manager is not None else WorkerManager()
        self.pdf_batch_size = pdf_batch_size

        # --- document registry (one "tab" per source) ---
        self.loaded_documents: dict[str, LoadedDocument] = {}
        self.ingestion_history: list[str] = []
        self._active_source: str | None = None
        self._doc_domains: dict[str, str] = {}
        self._doc_queues: dict[str, str] = {}
        self.doc_context: str = ""

        # --- chat ---
        self.chat_histories: dict[str, list[dict[str, Any]]] = {}
        self._chat_log_dir = chat_log_dir or (Path.home() / ".sqwakvox_chat_logs")
        with contextlib.suppress(OSError):
            self._chat_log_dir.mkdir(parents=True, exist_ok=True)

        # --- credentials (in memory only, never persisted) ---
        self.model_id: str = DEFAULT_MODEL_ID
        self._api_key: str = ""

        # --- jobs / events ---
        self._jobs: dict[str, Job] = {}
        self._sinks: list[EventSink] = []
        self._seq = 0
        self._events_log: list[SessionEvent] = []
        self._closed = False

        self.mcp_configs: list[tuple[str, Any, list[str] | None]] = []
        self.load_mcp_servers()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="sqwakvox-session", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    # Event loop plumbing
    # ------------------------------------------------------------------ #
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Any, *, kind: str, source: str = "") -> Job:
        """Schedule *coro* on the session loop and return its :class:`Job`."""
        if self._closed:
            coro.close()
            raise RuntimeError("DocSession is closed")
        job = Job(job_id=f"{kind}-{uuid.uuid4().hex[:8]}", kind=kind, source=source)
        job.state = JOB_RUNNING
        future = asyncio.run_coroutine_threadsafe(self._guard(coro, job), self._loop)
        job.future = future
        self._jobs[job.job_id] = job
        self._emit(EVENT_JOB, {"job": job.as_dict()})
        return job

    async def _guard(self, coro: Any, job: Job) -> dict[str, Any]:
        """Wrap a job coroutine so state and events are always recorded."""
        try:
            result = await coro
        except asyncio.CancelledError:
            job.state = JOB_CANCELLED
            job.error = "Cancelled."
            self._emit(EVENT_JOB, {"job": job.as_dict()})
            raise
        except Exception as exc:
            job.state = JOB_FAILURE
            job.error = extract_message(str(exc)) or str(exc)
            job.result = {"ok": False, "error": job.error}
            logger.error("Session job %s failed: %s", job.job_id, exc, exc_info=True)
            self._emit(EVENT_JOB, {"job": job.as_dict()})
            self._emit(EVENT_ERROR, {"job_id": job.job_id, "message": job.error})
            return dict(job.result)
        job.state = JOB_SUCCESS
        job.result = result
        self._emit(EVENT_JOB, {"job": job.as_dict()})
        return dict(result)

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def subscribe(self, sink: EventSink) -> Callable[[], None]:
        """Register *sink*; returns an unsubscribe callable."""
        self._sinks.append(sink)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._sinks.remove(sink)

        return _unsubscribe

    def _emit(self, kind: str, data: dict[str, Any]) -> SessionEvent:
        self._seq += 1
        event = SessionEvent(seq=self._seq, kind=kind, data=data)
        self._events_log.append(event)
        # Bound the replay buffer so a long session cannot grow without limit.
        if len(self._events_log) > 2000:
            del self._events_log[:1000]
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:  # pragma: no cover — a broken view must not kill the bus
                logger.debug("Event sink failed for %s", kind, exc_info=True)
        return event

    def events_since(self, since_seq: int = 0, limit: int = 500) -> list[SessionEvent]:
        """Replay buffered events with ``seq > since_seq`` (oldest first)."""
        return [e for e in self._events_log if e.seq > since_seq][:limit]

    # ------------------------------------------------------------------ #
    # MCP server configs (shared by every view)
    # ------------------------------------------------------------------ #
    def load_mcp_servers(self) -> list[tuple[str, Any, list[str] | None]]:
        """Load ``mcp_servers.json`` from the standard locations.

        Supports stdio servers (``command``/``args``) and HTTP servers
        (``url`` + ``transport`` of ``sse``/``http``).
        """
        from sqwakvox.mcp import MCPSse, MCPStdio, MCPStreamableHttp

        configs: list[tuple[str, Any, list[str] | None]] = []
        paths = [
            Path("mcp_servers.json"),
            Path.home() / ".sqwakvox" / "mcp_servers.json",
            Path.home() / ".config" / "sqwakvox" / "mcp_servers.json",
        ]
        for path in paths:
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8") as fh:
                    config = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Error loading MCP servers from %s: %s", path, exc)
                continue

            servers = config.get("mcpServers", config)
            if not isinstance(servers, dict):
                continue
            for name, srv in servers.items():
                if not isinstance(srv, dict):
                    continue
                timeout_seconds = srv.get("client_session_timeout_seconds", 300.0)
                mcp_opt: Any
                if "url" in srv:
                    headers = srv.get("headers")
                    if srv.get("transport", "sse") == "http":
                        mcp_opt = MCPStreamableHttp(
                            url=srv["url"],
                            headers=headers,
                            client_session_timeout_seconds=timeout_seconds,
                        )
                    else:
                        mcp_opt = MCPSse(
                            url=srv["url"],
                            headers=headers,
                            client_session_timeout_seconds=timeout_seconds,
                        )
                elif "command" in srv:
                    env = srv.get("env")
                    if env:
                        env = {str(k): str(v) for k, v in env.items()}
                    mcp_opt = MCPStdio(
                        command=srv["command"],
                        args=srv.get("args", []),
                        env=env,
                        tools=srv.get("tools", None),
                        client_session_timeout_seconds=timeout_seconds,
                    )
                else:
                    continue
                configs.append((name, mcp_opt, srv.get("domains")))
            break
        self.mcp_configs = configs
        return configs

    def mcp_configs_for(self, domain_id: str) -> list[Any]:
        """MCP configs available to *domain_id* (untagged servers are global)."""
        return [
            cfg for _n, cfg, domains in self.mcp_configs if domains is None or domain_id in domains
        ]

    def mcp_server_names(self, domain_id: str | None = None) -> list[dict[str, Any]]:
        """Serialisable description of the MCP servers attached to the session."""
        out: list[dict[str, Any]] = []
        for name, cfg, domains in self.mcp_configs:
            if domain_id and domains is not None and domain_id not in domains:
                continue
            out.append(
                {
                    "name": name,
                    "command": getattr(cfg, "command", None),
                    "url": getattr(cfg, "url", None),
                    "domains": domains,
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Queues / workers
    # ------------------------------------------------------------------ #
    def queue_for_source(self, source: str) -> str | None:
        """Dedicated agent queue for *source*, assigned on first use.

        ``None`` when managed workers are disabled — tasks fall through to the
        default ``sqwakvox`` queue served by an externally started worker.
        """
        if not managed_workers_enabled():
            return None
        if source not in self._doc_queues:
            self._doc_queues[source] = f"sqwakvox.doc{len(self._doc_queues)}"
        return self._doc_queues[source]

    def docling_queue(self) -> str | None:
        """Shared Docling conversion queue, or ``None`` for the default."""
        if not managed_workers_enabled():
            return None
        return DOCLING_QUEUE

    def queue_of(self, source: str | None) -> str | None:
        if not source:
            return None
        return self._doc_queues.get(source)

    def domain_of(self, source: str | None) -> str:
        """Domain of *source*, defaulting to the active document's."""
        source = source or self._active_source
        if source is None:
            return "financial"
        return self._doc_domains.get(source, "financial")

    # ------------------------------------------------------------------ #
    # Static catalogs
    # ------------------------------------------------------------------ #
    def list_domains(self) -> list[dict[str, Any]]:
        """Every registered document assistant (id, label, capabilities)."""
        return [
            {
                "domain_id": d.domain_id,
                "display_name": d.display_name,
                "description": d.description,
                "skills_enabled": d.skills_enabled,
            }
            for d in list_domains()
        ]

    def list_models(self) -> list[dict[str, Any]]:
        """Selectable ``provider:model`` ids and where their key comes from."""
        return [
            {
                "model_id": model_id,
                "friendly_name": str(meta.get("friendly_name", model_id)),
                "env_var": str(meta.get("env_var", "")),
                "configured": bool(os.environ.get(str(meta.get("env_var", "")), "").strip()),
            }
            for model_id, meta in ModelProvider.MAP.items()
        ]

    def list_skills(self, domain_id: str | None = None) -> list[dict[str, Any]]:
        """Stored skills for *domain_id* (empty when the domain has none)."""
        domain_id = domain_id or self.domain_of(None)
        domain = get_domain(domain_id)
        if not domain.skills_enabled:
            return []
        return swe_skills.list_skills(domain_id)

    def search_chunks(self, query: str, doc_id: str = "", k: int = 5) -> list[dict[str, Any]]:
        """Search the SWE FTS5 chunk index (see :mod:`sqwakvox.domains.swe.retrieval`)."""
        from sqwakvox.domains.swe import retrieval

        target = doc_id.strip() or self.active_source() or ""
        if not target:
            active = session_registry.get_active_session()
            if active and active.get("domain_id") == "swe":
                target = str(active.get("active_document_name", ""))
        if not target:
            indexed = retrieval.list_documents()
            if not indexed:
                return []
            target = indexed[0][0]
        return retrieval.search_document(target, query, k=max(1, min(k, 50)))

    def indexed_documents(self) -> list[dict[str, Any]]:
        """Documents present in the SQLite FTS5 retrieval index."""
        from sqwakvox.domains.swe import retrieval

        return [{"doc_id": doc_id, "chunks": count} for doc_id, count in retrieval.list_documents()]

    # ------------------------------------------------------------------ #
    # Document registry
    # ------------------------------------------------------------------ #
    def active_source(self) -> str | None:
        return self._active_source

    def active_document_name(self) -> str:
        loaded = self.loaded_documents.get(self._active_source or "")
        return loaded.file_name if loaded else ""

    def documents(self) -> list[dict[str, Any]]:
        """Serialisable description of every loaded document (tab)."""
        out: list[dict[str, Any]] = []
        for source in self.ingestion_history:
            loaded = self.loaded_documents.get(source)
            if loaded is None:
                continue
            out.append(
                {
                    "source": source,
                    "file_name": loaded.file_name,
                    "domain_id": loaded.domain_id,
                    "queue": self.queue_of(source),
                    "table_count": len(loaded.structured.tables),
                    "char_count": len(loaded.structured.raw_markdown or ""),
                    "page_range": self.page_info(source),
                    "active": source == self._active_source,
                }
            )
        return out

    def page_info(self, source: str | None = None) -> dict[str, Any]:
        """Page-slice bookkeeping for incrementally loaded PDFs."""
        loaded = self.loaded_documents.get(source or self._active_source or "")
        if loaded is None or not loaded.is_paged:
            return {"paged": False}
        done = False
        if loaded.total_pages:
            done = loaded.rendered_pages >= loaded.total_pages
        else:
            cached = loaded.batch_cache.get(loaded.next_batch)
            done = cached is not None and not (cached.raw_markdown or "").strip()
        return {
            "paged": True,
            "batch_size": loaded.batch_size,
            "rendered_pages": loaded.rendered_pages,
            "total_pages": loaded.total_pages,
            "next_batch": loaded.next_batch,
            "complete": done,
        }

    def snapshot(self) -> dict[str, Any]:
        """One-shot view state: what the TUI status bar and the web sidebar show."""
        return {
            "model_id": self.model_id,
            "api_key_configured": bool(self._api_key),
            "active_source": self._active_source,
            "active_document_name": self.active_document_name(),
            "active_domain_id": self.domain_of(None),
            "context_chars": len(self.doc_context),
            "documents": self.documents(),
            "jobs": [j.as_dict() for j in self._jobs.values() if j.state == JOB_RUNNING],
            "mcp_servers": self.mcp_server_names(),
            "workers": self.worker_manager.queues,
            "seq": self._seq,
            "pdf_batch_size": self.pdf_batch_size,
        }

    def document_detail(
        self, source: str | None = None, *, include_text: bool = True
    ) -> dict[str, Any]:
        """Full view model for one document: metadata, tables, rendered text."""
        source = source or self._active_source
        loaded = self.loaded_documents.get(source or "")
        if loaded is None:
            raise ValueError(f"No loaded document for source {source!r}")
        doc = loaded.structured
        detail: dict[str, Any] = {
            "source": loaded.source,
            "file_name": loaded.file_name,
            "domain_id": loaded.domain_id,
            "queue": self.queue_of(loaded.source),
            "metadata": doc.metadata,
            "tables": [t.model_dump() for t in doc.tables],
            "page_range": self.page_info(loaded.source),
            "active": loaded.source == self._active_source,
        }
        if include_text:
            detail["rendered"] = render_document_text(doc, loaded.domain_id)
            detail["markdown"] = doc.raw_markdown
        return detail

    def data_store(self, source: str | None = None) -> dict[str, str]:
        """Financial data store recorded for *source* (empty for other domains)."""
        source = source or self._active_source
        loaded = self.loaded_documents.get(source or "")
        if loaded is None:
            return {}
        store = loaded.structured.metadata.get("data_store")
        return dict(store) if isinstance(store, dict) else {}

    # ------------------------------------------------------------------ #
    # Chat history
    # ------------------------------------------------------------------ #
    def _chat_log_path(self, doc_name: str) -> Path:
        safe = re.sub(r"[^\w.\-]", "_", doc_name)
        return self._chat_log_dir / f"{safe}.jsonl"

    def chat(self, source: str | None = None) -> list[dict[str, Any]]:
        """Chat messages for *source*, loading them from disk on first access."""
        loaded = self.loaded_documents.get(source or self._active_source or "")
        if loaded is None:
            return []
        name = loaded.file_name
        if name not in self.chat_histories:
            self.chat_histories[name] = self._load_chat_log(name)
        return list(self.chat_histories[name])

    def _save_chat_log(self, doc_name: str) -> None:
        path = self._chat_log_path(doc_name)
        try:
            with path.open("w", encoding="utf-8") as fh:
                for entry in self.chat_histories.get(doc_name, []):
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("Could not write chat log to %s", path)

    def _load_chat_log(self, doc_name: str) -> list[dict[str, Any]]:
        """Load a saved chat log, tolerating TUI-written plain-string entries."""
        path = self._chat_log_path(doc_name)
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        item = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        entries.append(item)
                    elif isinstance(item, str):
                        entries.append({"role": "system", "text": _strip_markup(item)})
        except OSError:
            logger.warning("Could not read chat log to %s", path)
        return entries

    def append_chat(self, source: str, role: str, text: str, **extra: Any) -> dict[str, Any]:
        """Append a chat message, persist it, and notify views."""
        entry: dict[str, Any] = {"role": role, "text": text, "ts": time.time(), **extra}
        name = self.loaded_documents[source].file_name
        self.chat_histories.setdefault(name, []).append(entry)
        self._save_chat_log(name)
        self._emit(EVENT_CHAT, {"source": source, "message": entry})
        return entry

    def clear_chat(self, source: str | None = None) -> bool:
        """Drop the chat history for *source* (and its saved log)."""
        loaded = self.loaded_documents.get(source or self._active_source or "")
        if loaded is None:
            return False
        name = loaded.file_name
        self.chat_histories[name] = []
        self._save_chat_log(name)
        self._emit(EVENT_CHAT, {"source": loaded.source, "cleared": True, "message": None})
        return True

    def set_credentials(self, model_id: str, api_key: str) -> None:
        """Set the model used for agent queries (the key stays in memory)."""
        if model_id:
            self.model_id = model_id
        if api_key:
            self._api_key = api_key.strip()

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #
    def jobs(self) -> list[dict[str, Any]]:
        return [j.as_dict() for j in self._jobs.values()]

    def job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def wait_job(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until *job_id* settles; returns the job's result dict."""
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job id: {job_id}")
        if job.future is None:
            return job.result or {}
        try:
            return job.future.result(timeout=timeout) or {}
        except TimeoutError:
            raise TimeoutError(f"Timed out waiting for job {job_id}") from None
        except asyncio.CancelledError:
            raise RuntimeError(f"Job {job_id} was cancelled") from None
        except Exception as exc:
            raise RuntimeError(extract_message(str(exc)) or str(exc)) from exc
        finally:
            if job.state in (JOB_SUCCESS, JOB_FAILURE, JOB_CANCELLED):
                self._jobs.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        """Cancel a running job; True when a job was found."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.future is not None and job.state == JOB_RUNNING:
            job.future.cancel()
            job.state = JOB_CANCELLED
            job.error = "Cancelled."
            self._emit(EVENT_JOB, {"job": job.as_dict()})
            self._jobs.pop(job_id, None)
            return True
        self._jobs.pop(job_id, None)
        return False

    def cancel_source_jobs(self, source: str) -> int:
        """Cancel every running job belonging to *source*."""
        targets = [
            jid for jid, j in self._jobs.items() if j.source == source and j.state == JOB_RUNNING
        ]
        for jid in targets:
            self.cancel(jid)
        return len(targets)

    # ------------------------------------------------------------------ #
    # Document opening
    # ------------------------------------------------------------------ #
    def open_document(
        self,
        source: str,
        domain_id: str = "financial",
        options: dict[str, Any] | None = None,
    ) -> Job:
        """Start ingesting *source*; returns a :class:`Job` immediately.

        ``options`` reaches the domain's ingest plan (e.g. ``{"crawl": True}``
        for SWE docs sites).
        """
        source = source.strip()
        if not source:
            raise ValueError("source must not be empty")
        self._doc_domains[source] = domain_id
        return self._submit(
            self._open_document(source, domain_id, options or {}),
            kind="open_document",
            source=source,
        )

    def open_document_and_wait(
        self,
        source: str,
        domain_id: str = "financial",
        options: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        job = self.open_document(source, domain_id, options)
        return self.wait_job(job.job_id, timeout=timeout)

    async def _open_document(
        self, source: str, domain_id: str, options: dict[str, Any]
    ) -> dict[str, Any]:
        """Parse → domain post-parse → publish → activate, emitting events."""
        domain = get_domain(domain_id)
        self._emit(
            EVENT_PROGRESS,
            {"source": source, "message": f"Starting {domain.display_name} ingestion..."},
        )

        doc_queue = self.queue_for_source(source)
        if doc_queue is not None:
            self.worker_manager.ensure_worker(doc_queue)
        docling_queue = self.docling_queue()
        if docling_queue is not None:
            self.worker_manager.ensure_docling_worker()

        page_range: tuple[int, int] | None = None
        if Path(source).suffix.lower() == ".pdf" and Path(source).is_file():
            page_range = (1, self.pdf_batch_size)

        def on_progress(status: TaskStatus, _payload: Any) -> None:
            if status == TaskStatus.STARTED:
                self._emit(
                    EVENT_PROGRESS,
                    {"source": source, "message": "Docling parser is running..."},
                )

        parse_failed: str | None = None
        parsed: StructuredDocument | None = None

        def on_complete(status: TaskStatus, payload: Any) -> None:
            # ``parse_document`` deserialises the broker dict for us here, so
            # this callback (not ``handle.result``) carries the document.
            nonlocal parse_failed, parsed
            if status == TaskStatus.SUCCESS and isinstance(payload, StructuredDocument):
                parsed = payload
            elif status == TaskStatus.FAILURE:
                parse_failed = payload if isinstance(payload, str) else str(payload)
            elif status in (TaskStatus.REVOKED, TaskStatus.CANCELLED):
                parse_failed = "Parse was cancelled."

        handle = await self.presenter.parse_document(
            source=source,
            domain_id=domain_id,
            options=options or None,
            page_range=page_range,
            queue=docling_queue,
            on_progress=on_progress,
            on_complete=on_complete,
        )
        await handle.wait()

        if parsed is None:
            message = extract_message(parse_failed or handle.error or "Parse failed.")
            self._emit(EVENT_ERROR, {"source": source, "message": message})
            AuditLogger.log(
                document_id="unknown",
                operation="document_ingested",
                action="FAILURE",
                risk_score=1.0,
            )
            return {"ok": False, "error": message}

        # --- domain post-parse (data store / TOC+index / retrieval) ---
        payload: dict[str, Any] = {}
        try:
            pp = await self.presenter.postprocess_document(
                domain_id=domain_id, document=parsed, queue=doc_queue
            )
            await pp.wait()
            if pp.status == TaskStatus.SUCCESS:
                payload = pp.result or {}
        except Exception as exc:
            logger.warning("Post-parse step failed for %s: %s", source, exc)

        if payload:
            parsed.metadata.update(payload)

        self._register_document(source, parsed, domain_id)
        self.activate(source)
        self._append_ingest_notices(source, domain_id, payload)
        AuditLogger.log(
            document_id=parsed.file_name, operation="document_ingested", action="SUCCESS"
        )

        # Keep one PDF slice ahead of what the user has revealed.
        if self.loaded_documents[source].is_paged:
            self._submit(self._prefetch(source, 1), kind="prefetch", source=source)

        return {
            "ok": True,
            "source": source,
            "file_name": parsed.file_name,
            "domain_id": domain_id,
            "table_count": len(parsed.tables),
            "char_count": len(parsed.raw_markdown or ""),
            "page_range": self.page_info(source),
        }

    def _register_document(self, source: str, doc: StructuredDocument, domain_id: str) -> None:
        """Record a parsed document in the tab registry."""
        loaded = LoadedDocument(domain_id=domain_id, structured=doc, source=source)
        if doc.metadata.get("page_range") is not None:
            loaded.batch_size = int(doc.metadata.get("pages_in_batch") or self.pdf_batch_size)
            loaded.total_pages = doc.metadata.get("total_pages")
            loaded.rendered_pages = loaded.batch_size
            loaded.next_batch = 1
        self.loaded_documents[source] = loaded
        if source not in self.ingestion_history:
            self.ingestion_history.append(source)
        if loaded.file_name not in self.chat_histories:
            self.chat_histories[loaded.file_name] = self._load_chat_log(loaded.file_name)
        from sqwakvox.telemetry import get_telemetry

        tm = get_telemetry()
        if tm.active_documents_counter:
            tm.active_documents_counter.add(1)
        self._emit(EVENT_DOCUMENT, {"event": "loaded", "document": self.documents()})

    def _append_ingest_notices(self, source: str, domain_id: str, payload: dict[str, Any]) -> None:
        """Surface post-parse analysis (SWE: TOC/code/injection/retrieval)."""
        notes: list[str] = []
        if domain_id == "swe":
            toc = payload.get("toc") or []
            code_blocks = payload.get("code_blocks") or []
            source_type = payload.get("source_type", "file")
            pages = payload.get("pages_converted")
            notes.append(
                f"Parsed as {source_type}: {len(toc)} section(s), {len(code_blocks)} code "
                f"block(s), {payload.get('chunk_count') or 0} search chunk(s)."
            )
            if pages:
                notes.append(f"{pages} docs-site pages converted.")
            for flag in payload.get("injection_flags") or []:
                notes.append(f"WARNING: potential prompt-injection text in document: {flag}")
            if payload.get("needs_retrieval"):
                notes.append(
                    "Large document: the agent will search sections with the retrieval "
                    "tool instead of reading it whole."
                )
        loaded = self.loaded_documents.get(source)
        char_count = len(loaded.structured.raw_markdown or "") if loaded else 0
        notes.insert(0, f"Document loaded successfully. Character count: {char_count}")
        for note in notes:
            self._emit(EVENT_LOG, {"source": source, "message": note})
        chat_logger.info("Document loaded: %s (%d chars)", source, char_count)

    def activate(self, source: str) -> bool:
        """Make *source* the active document (what both views render)."""
        loaded = self.loaded_documents.get(source)
        if loaded is None:
            return False
        self._active_source = source
        self.doc_context = get_domain(loaded.domain_id).context_for(loaded.structured)
        with contextlib.suppress(Exception):
            session_registry.publish_active_document(
                doc_name=loaded.file_name,
                source=source,
                domain_id=loaded.domain_id,
                queue=self.queue_of(source),
                doc_context=self.doc_context,
                data_store=self.data_store(source),
                table_count=len(loaded.structured.tables),
                model_id=self.model_id,
                thread_id=loaded.file_name,
            )
        self._emit(EVENT_DOCUMENT, {"event": "activated", "document": self.documents()})
        return True

    def close_document(self, source: str | None = None) -> bool:
        """Forget a loaded document, cancelling its running jobs."""
        source = source or self._active_source
        loaded = self.loaded_documents.pop(source or "", None)
        if loaded is None:
            return False
        self._doc_domains.pop(loaded.source, None)
        self._doc_queues.pop(loaded.source, None)
        with contextlib.suppress(ValueError):
            self.ingestion_history.remove(loaded.source)
        self.cancel_source_jobs(loaded.source)
        if self._active_source == loaded.source:
            self._active_source = None
            self.doc_context = ""
            nxt = next(iter(self.loaded_documents), None)
            if nxt:
                self.activate(nxt)
        self._emit(EVENT_DOCUMENT, {"event": "closed", "document": self.documents()})
        return True

    # ------------------------------------------------------------------ #
    # Incremental PDF paging
    # ------------------------------------------------------------------ #
    def load_more(self, source: str | None = None) -> Job:
        """Append the next PDF page slice to *source*."""
        source = source or self._active_source
        if source is None:
            raise ValueError("No active document loaded")
        return self._submit(self._load_more(source), kind="load_more", source=source)

    def load_more_and_wait(
        self, source: str | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        job = self.load_more(source)
        return self.wait_job(job.job_id, timeout=timeout)

    async def _prefetch(self, source: str, batch_index: int) -> dict[str, Any]:
        """Cache PDF slice *batch_index* in the background (one slice ahead)."""
        loaded = self.loaded_documents.get(source)
        if loaded is None or not loaded.is_paged:
            return {"ok": False, "error": "Document is not paged."}
        if batch_index in loaded.pending or batch_index in loaded.batch_cache:
            return {"ok": True, "cached": True}
        if loaded.total_pages and batch_index * loaded.batch_size >= loaded.total_pages:
            return {"ok": True, "cached": True}

        start = batch_index * loaded.batch_size + 1
        end = start + loaded.batch_size - 1
        if loaded.total_pages:
            end = min(end, loaded.total_pages)

        def on_complete(status: TaskStatus, payload: Any) -> None:
            if status == TaskStatus.SUCCESS and isinstance(payload, StructuredDocument):
                loaded.batch_cache[batch_index] = payload
            loaded.pending.pop(batch_index, None)

        handle = await self.presenter.parse_document(
            source=source,
            domain_id=loaded.domain_id,
            page_range=(start, end),
            queue=self.docling_queue(),
            on_complete=on_complete,
        )
        loaded.pending[batch_index] = handle
        await handle.wait()
        if batch_index in loaded.batch_cache:
            # Chain the next prefetch so the cache stays one slice ahead.
            self._submit(self._prefetch(source, batch_index + 1), kind="prefetch", source=source)
        return {"ok": True, "batch_index": batch_index, "pages": [start, end]}

    async def _load_more(self, source: str) -> dict[str, Any]:
        """Reveal the next prefetched (or freshly fetched) PDF slice."""
        loaded = self.loaded_documents.get(source)
        if loaded is None or not loaded.is_paged:
            return {"ok": False, "error": "Document is not paged."}

        batch_index = loaded.next_batch
        if batch_index not in loaded.batch_cache:
            if batch_index in loaded.pending:
                await loaded.pending[batch_index].wait()
            else:
                prefetch = await self._prefetch(source, batch_index)
                if not prefetch.get("ok"):
                    return prefetch

        batch = loaded.batch_cache.pop(batch_index, None)
        if batch is None:
            return {"ok": True, "page_range": self.page_info(source)}
        if not (batch.raw_markdown or "").strip():
            loaded.next_batch = batch_index + 1
            self._emit(EVENT_RENDER, {"source": source, "page_range": self.page_info(source)})
            return {"ok": True, "page_range": self.page_info(source)}

        loaded.structured.raw_markdown += "\n\n" + batch.raw_markdown
        loaded.structured.tables.extend(batch.tables)
        loaded.rendered_pages += int(batch.metadata.get("pages_in_batch") or loaded.batch_size)
        loaded.next_batch = batch_index + 1

        if source == self._active_source:
            self.doc_context = get_domain(loaded.domain_id).context_for(loaded.structured)
        self._emit(EVENT_RENDER, {"source": source, "page_range": self.page_info(source)})
        return {
            "ok": True,
            "pages_added": loaded.rendered_pages,
            "page_range": self.page_info(source),
        }

    # ------------------------------------------------------------------ #
    # Cross-validation
    # ------------------------------------------------------------------ #
    def cross_validate(self, source: str | None = None) -> Job:
        """Run the financial rule engine over *source*'s tables."""
        source = source or self._active_source
        if source is None:
            raise ValueError("No active document loaded")
        return self._submit(self._cross_validate(source), kind="cross_validate", source=source)

    def cross_validate_and_wait(
        self, source: str | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """Run cross-validation and wait; returns ``{"ok", "results"}``."""
        return self.wait_job(self.cross_validate(source).job_id, timeout=timeout)

    async def _cross_validate(self, source: str) -> dict[str, Any]:
        loaded = self.loaded_documents.get(source)
        if loaded is None:
            return {"ok": False, "error": "Document not loaded."}

        rows: list[tuple[str, float, float, bool]] = []

        def on_complete(status: TaskStatus, results: Any) -> None:
            if status != TaskStatus.SUCCESS:
                return
            rows.extend(results or [])
            for label, computed, expected, ok in rows:
                verdict = "OK" if ok else "MISMATCH"
                self._emit(
                    EVENT_LOG,
                    {
                        "source": source,
                        "message": (
                            f"Cross-validation: {label}: computed {computed:.4f} vs "
                            f"document {expected:.4f} ({verdict})"
                        ),
                    },
                )

        handle = await self.presenter.cross_validate(
            document=loaded.structured,
            queue=self.queue_of(source),
            on_complete=on_complete,
        )
        await handle.wait()
        results = rows or (handle.result or [])
        return {
            "ok": handle.status == TaskStatus.SUCCESS,
            "results": [
                {"label": label, "computed": computed, "expected": expected, "ok": ok}
                for label, computed, expected, ok in results
            ],
        }

    # ------------------------------------------------------------------ #
    # Chat
    # ------------------------------------------------------------------ #
    def ask(
        self,
        query: str,
        model_id: str | None = None,
        api_key: str | None = None,
        source: str | None = None,
    ) -> Job:
        """Send *query* to the active document's agent; returns a :class:`Job`."""
        source = source or self._active_source
        if source is None:
            raise ValueError("No active document loaded")
        if model_id:
            self.model_id = model_id
        if api_key:
            self._api_key = api_key.strip()
        return self._submit(self._ask(source, query), kind="ask", source=source)

    def ask_and_wait(
        self,
        query: str,
        model_id: str | None = None,
        api_key: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        return self.wait_job(self.ask(query, model_id, api_key, source).job_id)

    async def _ask(self, source: str, user_query: str) -> dict[str, Any]:
        """Post-parse → publish → execute_agent, streaming results as events."""
        loaded = self.loaded_documents.get(source)
        if loaded is None:
            return {"ok": False, "error": "Document not loaded."}
        domain_id = loaded.domain_id
        model_id = self.model_id
        chat_logger.info("User query (%s): %s", domain_id, user_query)
        self.append_chat(source, "user", user_query)

        # The worker's own environment is the fallback (see ModelProvider).
        key = self._api_key or ModelProvider.resolve_key(model_id)[1]
        if not key:
            message = (
                "No API key available. Provide one in the sidebar or set the "
                f"{ModelProvider.get_env_var(model_id)} environment variable on the worker."
            )
            self.append_chat(source, "system", message, level="error")
            self._emit(EVENT_ERROR, {"source": source, "message": message})
            return {"ok": False, "error": message}

        self._emit(EVENT_PROGRESS, {"source": source, "message": "Agent is thinking..."})

        # --- Step 1: domain post-parse (fresh data store / index) ---
        data_store: dict[str, str] = {}
        extras: dict[str, Any] = {}
        try:
            pp = await self.presenter.postprocess_document(
                domain_id=domain_id, document=loaded.structured, queue=self.queue_of(source)
            )
            await pp.wait()
            if pp.status == TaskStatus.SUCCESS:
                payload = pp.result or {}
                data_store = dict(payload.get("data_store", {}))
                extras = {k: v for k, v in payload.items() if k != "data_store"}
                if extras:
                    loaded.structured.metadata.update(extras)
        except Exception as exc:
            message = extract_message(str(exc))
            self._emit(EVENT_ERROR, {"source": source, "message": message})
            return {"ok": False, "error": message}

        if data_store:
            loaded.structured.metadata["data_store"] = data_store
        with contextlib.suppress(Exception):
            session_registry.publish_active_document(
                doc_name=loaded.file_name,
                source=source,
                domain_id=domain_id,
                queue=self.queue_of(source),
                doc_context=self.doc_context,
                data_store=data_store,
                table_count=len(loaded.structured.tables),
                model_id=model_id,
                thread_id=loaded.file_name,
            )

        # --- Step 2: MCP configs for this domain, broker-safe ---
        mcp_servers = [cfg.model_dump() for cfg in self.mcp_configs_for(domain_id)]

        def on_progress(status: TaskStatus, _payload: Any) -> None:
            if status == TaskStatus.STARTED:
                self._emit(EVENT_PROGRESS, {"source": source, "message": "Agent is running..."})

        def on_complete(status: TaskStatus, result: Any) -> None:
            if status == TaskStatus.SUCCESS and isinstance(result, AgentResult):
                self._record_agent_result(source, result, user_query)
            elif status in (TaskStatus.REVOKED, TaskStatus.CANCELLED):
                self.append_chat(source, "system", "Agent task was cancelled.")

        handle = await self.presenter.execute_agent(
            model_id=model_id,
            api_key=key,
            user_query=user_query,
            doc_context=self.doc_context,
            active_document_name=loaded.file_name,
            data_store=data_store,
            mcp_servers=mcp_servers,
            thread_id=loaded.file_name,
            domain_id=domain_id,
            queue=self.queue_of(source),
            on_progress=on_progress,
            on_complete=on_complete,
        )
        await handle.wait()
        return {"ok": handle.status == TaskStatus.SUCCESS, "source": source, "model_id": model_id}

    def _record_agent_result(self, source: str, result: AgentResult, query: str) -> None:
        """Turn an :class:`AgentResult` into chat events."""
        if result.is_blocked:
            self.append_chat(
                source,
                "system",
                f"Input blocked by guardrail: {extract_message(result.blocked_reason)}",
                level="error",
            )
            return
        if result.pii_redacted_query:
            self.append_chat(
                source, "system", "PII detected and redacted from query.", level="warn"
            )
        if not result.success:
            self.append_chat(source, "system", result.error_message, level="error")
            return
        for discrepancy in result.math_discrepancies or []:
            self.append_chat(source, "system", f"Guardrail flagged: {discrepancy}", level="warn")
        self.append_chat(source, "agent", result.response)
        chat_logger.info("Agent response (%d chars)", len(result.response))
        AuditLogger.log(
            document_id=self.active_document_name() or "unknown",
            operation="agent_response",
            action="ALLOWED",
            input_text=query,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def publish_session_clear(self) -> bool:
        """Clear the Redis session registry (call on view shutdown)."""
        return session_registry.clear_session()

    def close(self) -> None:
        """Cancel jobs, stop the loop, and release resources."""
        if self._closed:
            return
        self._closed = True
        for job in list(self._jobs.values()):
            if job.future is not None and job.state == JOB_RUNNING:
                job.future.cancel()
        self._jobs.clear()

        async def _shutdown() -> None:
            with contextlib.suppress(Exception):
                await self.presenter.close()
            self.worker_manager.stop_all()

        with contextlib.suppress(Exception):
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            future.result(timeout=10)
        with contextlib.suppress(Exception):
            self._loop.call_soon_threadsafe(self._loop.stop)
        with contextlib.suppress(Exception):
            self._thread.join(timeout=5)
        with contextlib.suppress(Exception):
            self._loop.close()
        if self._owns_presenter:
            logger.debug("Presenter closed with the session")


# --------------------------------------------------------------------------- #
# Process-wide default session
# --------------------------------------------------------------------------- #

_default_session: DocSession | None = None
_default_lock = threading.Lock()


def get_session() -> DocSession:
    """Return the process-wide :class:`DocSession`, creating it on first use.

    The MCP presenter server, the web presenter, and any embedding tool all
    share one session so a document opened through one surface is visible from
    the others.
    """
    global _default_session
    with _default_lock:
        if _default_session is None:
            _default_session = DocSession()
        return _default_session


def set_session(session: DocSession | None) -> None:
    """Replace (or clear, with ``None``) the process-wide session."""
    global _default_session
    with _default_lock:
        _default_session = session


def as_text_lines(events: Iterable[SessionEvent]) -> str:
    """Render events as plain lines (used by the MCP ``events`` tool)."""
    out: list[str] = []
    for event in events:
        message = event.data.get("message") or event.data.get("job", {}).get("state", "")
        out.append(f"[{event.seq}] {event.kind}: {message}")
    return "\n".join(out) if out else "No events."
