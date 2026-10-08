"""Tests for the web presenter: MCP bridge, auth, and the event stream."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_doc_session import FakePresenter

from sqwakvox.doc_session import DocSession, SessionEvent, set_session
from sqwakvox.web_presenter import WebPresenter, _structured, _text_of


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DocSession:
    monkeypatch.setenv("SQWAKVOX_MANAGED_WORKERS", "0")
    session = DocSession(presenter=FakePresenter(), chat_log_dir=tmp_path)
    set_session(session)
    yield session
    set_session(None)
    session.close()


@pytest.fixture
def client(session: DocSession) -> Any:
    presenter = WebPresenter(session=session, allow_remote=True)
    with TestClient(presenter.app) as client:
        yield client


def _call(client: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    response = client.post("/api/call", json={"tool": tool, "arguments": arguments})
    body = response.json()
    assert response.status_code == 200
    return dict(body["data"])


# ------------------------------------------------------------------- serving


def test_index_and_assets_are_served(client: Any) -> None:
    assert client.get("/").status_code == 200
    for asset in ("/static/app.js", "/static/styles.css"):
        assert client.get(asset).status_code == 200


def test_healthz(client: Any) -> None:
    body = client.get("/healthz").json()
    assert body == {
        "ok": True,
        "documents": 0,
        "active": "",
        "auth_required": False,
    }


# ------------------------------------------------------------------ mcp bridge


def test_tools_endpoint_publishes_schemas(client: Any) -> None:
    body = client.get("/api/tools").json()
    assert body["ok"] is True
    names = {t["name"] for t in body["tools"]}
    assert "sqwakvox_presenter_open_document" in names
    open_tool = next(t for t in body["tools"] if t["name"] == "sqwakvox_presenter_open_document")
    assert "source" in open_tool["input_schema"]["properties"]
    assert body["session"]["documents"] == []


def test_call_routes_through_mcp(client: Any) -> None:
    opened = _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")
    assert opened["ok"] is True
    assert _call(client, "sqwakvox_presenter_document")["document"]["file_name"] == "report.pdf"


def test_a_failing_tool_reports_ok_false(client: Any) -> None:
    body = client.post("/api/call", json={"tool": "sqwakvox_presenter_document"}).json()
    assert body["ok"] is True  # the bridge succeeded
    assert body["data"]["ok"] is False  # the tool did not


def test_unknown_tool_is_reported_not_raised(client: Any) -> None:
    body = client.post("/api/call", json={"tool": "nope"}).json()
    assert body["ok"] is False
    assert "error" in body


def test_malformed_call_bodies_are_rejected(client: Any) -> None:
    assert client.post("/api/call", json={"arguments": {}}).status_code == 400
    assert (
        client.post("/api/call", json={"tool": "t", "arguments": "oops"}).status_code == 400
    )
    assert client.post(
        "/api/call", content="not json", headers={"Content-Type": "application/json"}
    ).status_code == 400


# ------------------------------------------------------------------ security


def test_a_token_is_required_when_configured(session: DocSession) -> None:
    presenter = WebPresenter(session=session, token="secret", allow_remote=True)
    with TestClient(presenter.app) as client:
        assert client.get("/api/tools").status_code == 401
        assert (
            client.get("/api/tools", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        assert (
            client.get("/api/tools", headers={"Authorization": "Bearer secret"}).status_code == 200
        )
        # SSE accepts the token as a query parameter (EventSource cannot set headers).
        assert client.get("/api/events", params={"token": "wrong"}).status_code == 401
        assert client.get("/healthz").status_code == 200  # probe stays open


def test_non_local_hosts_are_refused(session: DocSession) -> None:
    presenter = WebPresenter(session=session, allow_remote=False)
    with TestClient(presenter.app) as client:
        assert client.get("/healthz", headers={"Host": "attacker.example"}).status_code == 403
        assert client.get("/healthz", headers={"Host": "127.0.0.1:8760"}).status_code == 200


# --------------------------------------------------------------------- events


@pytest.mark.asyncio
async def test_event_stream_replays_and_streams(session: DocSession) -> None:
    session.open_document_and_wait("/tmp/report.pdf")
    presenter = WebPresenter(session=session, allow_remote=True)

    # Resume from the session's cursor: only live events should arrive.
    cursor = session.snapshot()["seq"]
    stream = presenter._event_stream(cursor)
    hello = await stream.__anext__()
    assert hello.startswith("event: hello")
    assert json.loads(hello.split("data: ", 1)[1])["backlog"] == 0

    session.append_chat("/tmp/report.pdf", "user", "over the stream")
    frame = await asyncio.wait_for(stream.__anext__(), timeout=5)
    assert frame.startswith("event: chat")
    assert "over the stream" in frame
    await stream.aclose()


@pytest.mark.asyncio
async def test_event_stream_handles_a_disconnected_client(session: DocSession) -> None:
    presenter = WebPresenter(session=session, allow_remote=True)
    stream = presenter._event_stream(0)
    await stream.__anext__()
    await stream.aclose()
    # No subscriber may be left behind after the generator is closed.
    assert session._sinks == []


@pytest.mark.asyncio
async def test_event_stream_replays_a_backlog(session: DocSession) -> None:
    session.open_document_and_wait("/tmp/report.pdf")
    presenter = WebPresenter(session=session, allow_remote=True)

    stream = presenter._event_stream(0)
    hello = await stream.__anext__()
    assert json.loads(hello.split("data: ", 1)[1])["backlog"] > 0

    replayed = await stream.__anext__()
    assert replayed.startswith("event: ")
    await stream.aclose()


def test_sinks_receive_session_events(session: DocSession) -> None:
    seen: list[SessionEvent] = []
    unsubscribe = session.subscribe(seen.append)
    session._emit("log", {"message": "hello"})
    unsubscribe()
    assert [e.kind for e in seen] == ["log"]


# ------------------------------------------------------------------- helpers


def test_structured_parses_the_json_text_payload() -> None:
    class _Item:
        text = '{"ok": true, "answer": "42"}'

    result = SimpleNamespace(content=[_Item()])
    assert _structured(result) == {"ok": True, "answer": "42"}
    assert _text_of(result) == '{"ok": true, "answer": "42"}'


def test_structured_falls_back_to_a_result_wrapper() -> None:
    class _Item:
        text = "plain text answer"

    result = SimpleNamespace(content=[_Item()])
    assert _structured(result) == {"result": "plain text answer"}
