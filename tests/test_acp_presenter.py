"""Unit and protocol tests for :mod:`sqwakvox.acp_presenter`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    EnvVariable,
    McpServerStdio,
    ResourceContentBlock,
    SessionConfigOptionSelect,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)

from sqwakvox.acp_presenter import SqwakvoxACPAgent, _resolve_source
from sqwakvox.doc_session import DocSession
from sqwakvox.models import AgentResult, StructuredDocument, TableData
from sqwakvox.presenter import TaskStatus


class _RecordingClient:
    """In-memory stand-in for :class:`acp.Client` capturing all session updates."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any, **_kwargs: Any) -> None:
        self.updates.append((session_id, update))

    def agent_texts(self, session_id: str | None = None) -> list[str]:
        out: list[str] = []
        for sid, upd in self.updates:
            if session_id is not None and sid != session_id:
                continue
            if isinstance(upd, AgentMessageChunk) and isinstance(upd.content, TextContentBlock):
                out.append(upd.content.text)
        return out

    def thoughts(self, session_id: str | None = None) -> list[str]:
        out: list[str] = []
        for sid, upd in self.updates:
            if session_id is not None and sid != session_id:
                continue
            if isinstance(upd, AgentThoughtChunk) and isinstance(upd.content, TextContentBlock):
                out.append(upd.content.text)
        return out

    def tool_updates(self, session_id: str | None = None) -> list[ToolCallStart | ToolCallProgress]:
        return [
            upd
            for sid, upd in self.updates
            if (session_id is None or sid == session_id)
            and isinstance(upd, ToolCallStart | ToolCallProgress)
        ]


class _FakePresenter:
    """Fast in-memory Presenter double for deterministic DocSession tests."""

    def __init__(self, *, slow_agent: bool = False) -> None:
        self.slow_agent = slow_agent
        self.cancelled: list[str] = []

    async def parse_document(
        self,
        source: str,
        *,
        domain_id: str = "financial",
        page_range: tuple[int, int] | None = None,
        on_progress: Any = None,
        on_complete: Any = None,
        **_kwargs: Any,
    ) -> Any:
        if on_progress is not None:
            on_progress(TaskStatus.STARTED, None)
        start, end = page_range if page_range else (1, 10)
        is_pdf = source.lower().endswith(".pdf")
        meta: dict[str, Any] = {"domain_id": domain_id}
        if is_pdf:
            meta.update(
                {
                    "page_range": [start, end],
                    "pages_in_batch": end - start + 1,
                    "total_pages": 30,
                }
            )
        doc = StructuredDocument(
            file_name=Path(source).name,
            raw_markdown=f"# {Path(source).name} pages {start}-{end}",
            tables=[TableData(headers=["Item", "Amount"], rows=[["A", "$10"], ["Total", "$10"]])],
            metadata=meta,
        )
        if on_complete is not None:
            on_complete(TaskStatus.SUCCESS, doc)
        return _DoneHandle(doc)

    async def postprocess_document(self, **_kwargs: Any) -> Any:
        return _DoneHandle({"data_store": {"Total": "$10"}})

    async def cross_validate(self, *, on_complete: Any = None, **_kwargs: Any) -> Any:
        rows = [("Amount", 10.0, 10.0, True)]
        if on_complete is not None:
            on_complete(TaskStatus.SUCCESS, rows)
        return _DoneHandle(rows)

    async def execute_agent(
        self,
        *,
        user_query: str,
        on_progress: Any = None,
        on_complete: Any = None,
        **_kwargs: Any,
    ) -> Any:
        if on_progress is not None:
            on_progress(TaskStatus.STARTED, None)
        if self.slow_agent:
            await asyncio.sleep(5.0)
        result = AgentResult(
            response=f"Answered: {user_query}",
            success=True,
            pii_redacted_query="ssn" in user_query.lower(),
        )
        if on_complete is not None:
            on_complete(TaskStatus.SUCCESS, result)
        return _DoneHandle(result)

    async def cancel_task(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True

    async def close(self) -> None:
        return None


class _DoneHandle:
    def __init__(self, result: Any) -> None:
        self.task_id = "fake-task"
        self.status = TaskStatus.SUCCESS
        self.result = result
        self.error = None

    async def wait(self, _timeout: float | None = None) -> TaskStatus:
        return self.status


class _NoOpWorkerManager:
    def __init__(self) -> None:
        self.queues: list[str] = []

    def ensure_worker(self, _queue: str) -> bool:
        return False

    def ensure_docling_worker(self) -> bool:
        return False

    def stop_all(self) -> None:
        return None


@pytest.fixture
def acp_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a ``(SqwakvoxACPAgent, _RecordingClient, DocSession)`` harness."""
    monkeypatch.setenv("SQWAKVOX_MANAGED_WORKERS", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-acp-key-123456")

    def _make(*, slow_agent: bool = False) -> tuple[SqwakvoxACPAgent, _RecordingClient, DocSession]:
        session = DocSession(
            presenter=_FakePresenter(slow_agent=slow_agent),  # type: ignore[arg-type]
            worker_manager=_NoOpWorkerManager(),  # type: ignore[arg-type]
            chat_log_dir=tmp_path / "chats",
        )
        client = _RecordingClient()
        agent = SqwakvoxACPAgent(session=session)
        agent.on_connect(client)  # type: ignore[arg-type]
        return agent, client, session

    created: list[DocSession] = []

    def _factory(
        *, slow_agent: bool = False
    ) -> tuple[SqwakvoxACPAgent, _RecordingClient, DocSession]:
        agent, client, session = _make(slow_agent=slow_agent)
        created.append(session)
        return agent, client, session

    yield _factory
    for sess in created:
        sess.close()


@pytest.mark.asyncio
async def test_initialize_and_new_session_expose_modes_models_and_commands(
    acp_harness: Any, tmp_path: Path
) -> None:
    agent, client, _session = acp_harness()

    init = await agent.initialize(protocol_version=1)
    assert init.protocol_version == 1
    assert init.agent_info is not None
    assert init.agent_info.name == "sqwakvox"
    assert init.agent_capabilities is not None
    assert init.agent_capabilities.load_session is True
    assert init.agent_capabilities.prompt_capabilities is not None
    assert init.agent_capabilities.prompt_capabilities.embedded_context is True

    created = await agent.new_session(cwd=str(tmp_path))
    assert created.session_id.startswith("sqwakvox-")
    assert created.modes is not None
    assert created.modes.current_mode_id == "financial"
    assert {m.id for m in created.modes.available_modes} >= {"financial", "swe"}
    assert created.config_options is not None
    by_id = {
        opt.id: opt
        for opt in created.config_options
        if isinstance(opt, SessionConfigOptionSelect)
    }
    assert by_id["mode"].category == "mode"
    assert by_id["model"].category == "model"

    # Let the scheduled available_commands_update task deliver.
    await asyncio.sleep(0)
    cmd_updates = [u for _sid, u in client.updates if isinstance(u, AvailableCommandsUpdate)]
    assert len(cmd_updates) == 1
    names = {c.name for c in cmd_updates[0].available_commands}
    expected = {
        "open",
        "docs",
        "switch",
        "close",
        "page",
        "more",
        "validate",
        "search",
        "skills",
        "clear",
        "status",
    }
    assert expected <= names


@pytest.mark.asyncio
async def test_set_mode_and_config_option_updates_state(
    acp_harness: Any, tmp_path: Path
) -> None:
    agent, client, session = acp_harness()
    created = await agent.new_session(cwd=str(tmp_path))
    sid = created.session_id

    await agent.set_session_mode(session_id=sid, mode_id="swe")
    assert any(
        isinstance(u, CurrentModeUpdate) and u.current_mode_id == "swe" for _, u in client.updates
    )

    resp = await agent.set_config_option(
        config_id="model", session_id=sid, value="anthropic:claude-4.6"
    )
    assert session.model_id == "anthropic:claude-4.6"
    assert any(isinstance(u, ConfigOptionUpdate) for _, u in client.updates)
    assert any(
        isinstance(o, SessionConfigOptionSelect)
        and o.id == "model"
        and o.current_value == "anthropic:claude-4.6"
        for o in resp.config_options
    )

    with pytest.raises(RequestError):
        await agent.set_session_mode(session_id=sid, mode_id="nonexistent")
    with pytest.raises(RequestError):
        await agent.set_config_option(config_id="unknown", session_id=sid, value="x")


@pytest.mark.asyncio
async def test_slash_commands_and_chat_flow(acp_harness: Any, tmp_path: Path) -> None:
    agent, client, session = acp_harness()
    pdf = tmp_path / "q4_report.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    created = await agent.new_session(cwd=str(tmp_path))
    sid = created.session_id

    # Asking before opening a document gives guidance instead of failing.
    r0 = await agent.prompt(sid, [TextContentBlock(text="What is revenue?")])
    assert r0.stop_reason == "end_turn"
    assert any("/open" in t for t in client.agent_texts(sid))

    # /open resolves relative paths against cwd and emits ToolCallStart + ToolCallProgress.
    client.updates.clear()
    r1 = await agent.prompt(sid, [TextContentBlock(text="/open q4_report.pdf")])
    assert r1.stop_reason == "end_turn"
    assert session.active_document_name() == "q4_report.pdf"
    tool_events = client.tool_updates(sid)
    assert any(isinstance(e, ToolCallStart) for e in tool_events)
    assert any(isinstance(e, ToolCallProgress) and e.status == "completed" for e in tool_events)

    # /docs and /status
    client.updates.clear()
    await agent.prompt(sid, [TextContentBlock(text="/docs")])
    assert any("q4_report.pdf" in t for t in client.agent_texts(sid))

    client.updates.clear()
    await agent.prompt(sid, [TextContentBlock(text="/status")])
    assert any("Sqwakvox Session Status" in t for t in client.agent_texts(sid))

    # /page and /more
    client.updates.clear()
    await agent.prompt(sid, [TextContentBlock(text="/page 15")])
    assert any("15-24" in t for t in client.agent_texts(sid))

    client.updates.clear()
    await agent.prompt(sid, [TextContentBlock(text="/more up")])
    assert any("5-24" in t for t in client.agent_texts(sid))

    # /validate
    client.updates.clear()
    await agent.prompt(sid, [TextContentBlock(text="/validate")])
    assert any("Financial Cross-Validation" in t for t in client.agent_texts(sid))

    # Natural-language Q&A streams agent reply + PII thought notice.
    client.updates.clear()
    r_ask = await agent.prompt(sid, [TextContentBlock(text="Summarize revenue and ssn")])
    assert r_ask.stop_reason == "end_turn"
    assert any("Answered: Summarize revenue and ssn" in t for t in client.agent_texts(sid))
    assert any("PII detected" in t for t in client.thoughts(sid))

    # load_session replays conversation history.
    client.updates.clear()
    loaded = await agent.load_session(cwd=str(tmp_path), session_id=sid)
    assert loaded.modes is not None
    assert any(isinstance(u, UserMessageChunk) for _, u in client.updates)
    assert any(isinstance(u, AgentMessageChunk) for _, u in client.updates)

    # /clear and /close
    client.updates.clear()
    await agent.prompt(sid, [TextContentBlock(text="/clear")])
    assert session.chat() == []

    await agent.prompt(sid, [TextContentBlock(text="/close")])
    assert session.active_source() is None


@pytest.mark.asyncio
async def test_resource_attachment_auto_ingests_and_answers(
    acp_harness: Any, tmp_path: Path
) -> None:
    agent, client, session = acp_harness()
    doc_path = tmp_path / "notes.md"
    doc_path.write_text("# Notes\nhello", encoding="utf-8")

    created = await agent.new_session(cwd=str(tmp_path))
    sid = created.session_id

    resp = await agent.prompt(
        sid,
        [
            ResourceContentBlock(name="notes.md", uri=doc_path.as_uri()),
            TextContentBlock(text="What is inside?"),
        ],
    )
    assert resp.stop_reason == "end_turn"
    assert session.active_document_name() == "notes.md"
    assert any("Answered: What is inside?" in t for t in client.agent_texts(sid))


@pytest.mark.asyncio
async def test_cancel_stops_inflight_prompt(acp_harness: Any, tmp_path: Path) -> None:
    agent, _client, _session = acp_harness(slow_agent=True)
    doc_path = tmp_path / "slow.md"
    doc_path.write_text("# Slow", encoding="utf-8")

    created = await agent.new_session(cwd=str(tmp_path))
    sid = created.session_id
    await agent.prompt(sid, [TextContentBlock(text="/open slow.md")])

    prompt_task = asyncio.create_task(
        agent.prompt(sid, [TextContentBlock(text="Take your time")])
    )
    await asyncio.sleep(0.05)
    await agent.cancel(sid)
    resp = await asyncio.wait_for(prompt_task, timeout=2.0)
    assert resp.stop_reason == "cancelled"


@pytest.mark.asyncio
async def test_new_session_registers_client_mcp_servers_and_list_close(
    acp_harness: Any, tmp_path: Path
) -> None:
    agent, _client, session = acp_harness()
    srv = McpServerStdio(
        name="orca-extra",
        command="echo",
        args=["hello"],
        env=[EnvVariable(name="FOO", value="bar")],
    )
    created = await agent.new_session(cwd=str(tmp_path), mcp_servers=[srv])
    assert any(name == "orca-extra" for name, _cfg, _doms in session.mcp_configs)

    listed = await agent.list_sessions(cwd=str(tmp_path))
    assert [s.session_id for s in listed.sessions] == [created.session_id]

    await agent.close_session(created.session_id)
    listed_after = await agent.list_sessions()
    assert listed_after.sessions == []


def test_resolve_source_handles_urls_and_file_uris(tmp_path: Path) -> None:
    sample = tmp_path / "my report.pdf"
    sample.write_bytes(b"pdf")
    assert _resolve_source("https://example.com/docs", str(tmp_path)) == "https://example.com/docs"
    assert _resolve_source(sample.as_uri(), str(tmp_path)) == str(sample.resolve())
    assert _resolve_source("my report.pdf", str(tmp_path)) == str(sample.resolve())
