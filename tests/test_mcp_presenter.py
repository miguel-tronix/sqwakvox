"""Tests for the MCP presenter server (tool surface + behaviour)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import Client
from test_doc_session import FakePresenter

from sqwakvox import mcp_presenter
from sqwakvox.doc_session import DocSession, set_session


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DocSession:
    monkeypatch.setenv("SQWAKVOX_MANAGED_WORKERS", "0")
    session = DocSession(presenter=FakePresenter(), chat_log_dir=tmp_path)
    set_session(session)
    yield session
    set_session(None)
    session.close()


@pytest_asyncio.fixture
async def client() -> Any:
    async with Client(mcp_presenter.mcp) as mcp_client:
        yield mcp_client


async def _call(client: Any, name: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    for item in result.content:
        if item.text:
            return dict(json.loads(item.text))
    raise AssertionError(f"{name} returned no JSON text")


# ------------------------------------------------------------------- catalog


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_every_session_operation_is_published(client: Any) -> None:
    tools = {t.name for t in await client.list_tools()}
    assert tools == {
        "sqwakvox_presenter_status",
        "sqwakvox_presenter_list_domains",
        "sqwakvox_presenter_list_models",
        "sqwakvox_presenter_list_documents",
        "sqwakvox_presenter_open_document",
        "sqwakvox_presenter_open_document_sync",
        "sqwakvox_presenter_activate_document",
        "sqwakvox_presenter_close_document",
        "sqwakvox_presenter_document",
        "sqwakvox_presenter_load_more",
        "sqwakvox_presenter_cross_validate",
        "sqwakvox_presenter_cross_validate_async",
        "sqwakvox_presenter_data_store",
        "sqwakvox_presenter_ask",
        "sqwakvox_presenter_ask_sync",
        "sqwakvox_presenter_chat",
        "sqwakvox_presenter_clear_chat",
        "sqwakvox_presenter_jobs",
        "sqwakvox_presenter_wait_job",
        "sqwakvox_presenter_cancel",
        "sqwakvox_presenter_events",
        "sqwakvox_presenter_list_skills",
        "sqwakvox_presenter_search_chunks",
        "sqwakvox_presenter_indexed_documents",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_tools_document_their_arguments(client: Any) -> None:
    tools = {t.name: t for t in await client.list_tools()}
    ask = tools["sqwakvox_presenter_ask"]
    assert set(ask.inputSchema["properties"]) == {"query", "model_id", "source", "api_key"}
    assert "query" in ask.inputSchema["required"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_status_reports_an_empty_session(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_status")
    assert payload["ok"] is True
    assert payload["session"]["documents"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_catalogs(client: Any) -> None:
    domains = await _call(client, "sqwakvox_presenter_list_domains")
    assert {d["domain_id"] for d in domains["domains"]} == {"financial", "swe"}
    assert (await _call(client, "sqwakvox_presenter_list_models"))["models"]


# ------------------------------------------------------------------ documents


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_open_document_then_read_it(client: Any) -> None:
    opened = await _call(client, "sqwakvox_presenter_open_document", source="/tmp/report.pdf")
    assert opened["ok"] is True
    finished = await _call(client, "sqwakvox_presenter_wait_job", job_id=opened["job_id"])
    assert finished["ok"] is True

    listed = await _call(client, "sqwakvox_presenter_list_documents")
    assert listed["documents"][0]["file_name"] == "report.pdf"

    document = await _call(client, "sqwakvox_presenter_document")
    assert "report.pdf" in document["document"]["rendered"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_open_document_sync_blocks_until_parsed(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")
    assert payload["ok"] is True
    assert payload["file_name"] == "report.pdf"


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_open_document_rejects_an_empty_source(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_open_document", source="   ")
    assert payload["ok"] is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_activate_and_close(client: Any) -> None:
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/a.pdf")
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/b.pdf")

    switched = await _call(client, "sqwakvox_presenter_activate_document", source="/tmp/a.pdf")
    assert switched["active_document_name"] == "a.pdf"

    missing = await _call(client, "sqwakvox_presenter_activate_document", source="/tmp/zzz.pdf")
    assert missing["ok"] is False

    closed = await _call(client, "sqwakvox_presenter_close_document", source="/tmp/b.pdf")
    assert closed["ok"] is True
    assert len((await _call(client, "sqwakvox_presenter_list_documents"))["documents"]) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_document_without_a_document_reports_the_error(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_document")
    assert payload["ok"] is False
    assert "No loaded document" in payload["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_data_store_and_cross_validation(client: Any) -> None:
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")

    store = await _call(client, "sqwakvox_presenter_data_store")
    assert store["data_store"] == {"Revenue": "100"}

    validated = await _call(client, "sqwakvox_presenter_cross_validate")
    assert validated["results"][0]["ok"] is True

    async_job = await _call(client, "sqwakvox_presenter_cross_validate_async")
    assert (await _call(client, "sqwakvox_presenter_wait_job", job_id=async_job["job_id"]))["ok"]


# ------------------------------------------------------------------------ chat


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_ask_then_read_the_transcript(client: Any) -> None:
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")

    asked = await _call(
        client,
        "sqwakvox_presenter_ask",
        query="what is the revenue?",
        model_id="openai:gpt-5.5-high",
        api_key="k" * 20,
    )
    await _call(client, "sqwakvox_presenter_wait_job", job_id=asked["job_id"])

    chat = await _call(client, "sqwakvox_presenter_chat")
    assert chat["answer"] == "The answer is 42."
    assert [m["role"] for m in chat["messages"]] == ["user", "agent"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_ask_sync_returns_the_answer(client: Any) -> None:
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")
    payload = await _call(
        client,
        "sqwakvox_presenter_ask_sync",
        query="hello",
        model_id="openai:gpt-5.5-high",
        api_key="k" * 20,
    )
    assert payload["answer"] == "The answer is 42."


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_ask_without_a_document_reports_the_error(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_ask", query="hi")
    assert payload["ok"] is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_ask_rate_limit_is_reported(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQWAKVOX_MCP_QUERY_RPM", "1")
    # The limiter is process-wide; start this test with an empty window.
    mcp_presenter._limiter._calls.clear()
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")
    first = await _call(
        client, "sqwakvox_presenter_ask", query="one", api_key="k" * 20
    )
    assert first["ok"] is True
    second = await _call(
        client, "sqwakvox_presenter_ask", query="two", api_key="k" * 20
    )
    assert second["ok"] is False
    assert "Rate limit" in second["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_clear_chat(client: Any) -> None:
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")
    await _call(client, "sqwakvox_presenter_ask_sync", query="hi", api_key="k" * 20)

    assert (await _call(client, "sqwakvox_presenter_clear_chat"))["ok"] is True
    assert (await _call(client, "sqwakvox_presenter_chat"))["messages"] == []
    missing = await _call(client, "sqwakvox_presenter_clear_chat", source="/tmp/x.pdf")
    assert missing["ok"] is False


# ----------------------------------------------------------------------- jobs


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_jobs_events_and_cancel(client: Any) -> None:
    job = await _call(client, "sqwakvox_presenter_open_document", source="/tmp/report.pdf")
    listed = await _call(client, "sqwakvox_presenter_jobs")
    assert any(j["job_id"] == job["job_id"] for j in listed["jobs"])

    events = await _call(client, "sqwakvox_presenter_events")
    assert events["ok"] is True
    assert events["events"]

    assert (await _call(client, "sqwakvox_presenter_cancel", job_id="nope"))["ok"] is False
    await _call(client, "sqwakvox_presenter_wait_job", job_id=job["job_id"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_wait_job_on_an_unknown_id(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_wait_job", job_id="nope")
    assert payload["ok"] is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_events_resume_from_a_sequence_cursor(client: Any) -> None:
    await _call(client, "sqwakvox_presenter_open_document_sync", source="/tmp/report.pdf")
    first = await _call(client, "sqwakvox_presenter_events")
    cursor = first["events"][0]["seq"]
    later = await _call(client, "sqwakvox_presenter_events", since_seq=cursor)
    assert all(e["seq"] > cursor for e in later["events"])


# -------------------------------------------------------- skills & retrieval


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_list_skills_for_a_domain_without_skills(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_list_skills", domain_id="financial")
    assert payload["skills"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("session")
async def test_indexed_documents_are_listed(client: Any) -> None:
    payload = await _call(client, "sqwakvox_presenter_indexed_documents")
    assert payload["ok"] is True


# --------------------------------------------------------------- transport


def test_http_transport_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from sqwakvox.mcp_http import http_opt_in_error

    monkeypatch.delenv("SQWAKVOX_MCP_ALLOW_HTTP", raising=False)
    args = SimpleNamespace(transport="http", host="127.0.0.1", port=8765)
    with pytest.raises(SystemExit):
        mcp_presenter.run_with_http_guard(
            mcp_presenter.mcp, args, server_name="presenter"
        )

    monkeypatch.setenv("SQWAKVOX_MCP_ALLOW_HTTP", "1")
    monkeypatch.setenv("SQWAKVOX_MCP_HTTP_TOKEN", "tok")
    assert http_opt_in_error("presenter") is None


def test_rate_limiter_reads_its_budget_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = mcp_presenter._RateLimiter(default_rpm=2)
    monkeypatch.delenv("SQWAKVOX_MCP_QUERY_RPM", raising=False)
    assert [limiter.allows() for _ in range(3)] == [True, True, False]

    monkeypatch.setenv("SQWAKVOX_MCP_QUERY_RPM", "0")
    assert all(limiter.allows() for _ in range(5))

    # A non-numeric budget falls back to the default rather than crashing.
    monkeypatch.setenv("SQWAKVOX_MCP_QUERY_RPM", "not-a-number")
    fresh = mcp_presenter._RateLimiter(default_rpm=1)
    assert [fresh.allows() for _ in range(2)] == [True, False]
