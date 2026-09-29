from __future__ import annotations

import json
from datetime import timedelta

import pytest
from langchain_core.tools import StructuredTool

from sqwakvox.mcp import (
    McpConnection,
    MCPSse,
    MCPStdio,
    MCPStreamableHttp,
    filter_tools,
    to_connection,
    tool_names,
)


def _tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda **_kw: "ok", name=name, description="d")


class TestToConnection:
    def test_stdio_maps_command_and_args(self) -> None:
        conn = to_connection(MCPStdio(command="uvx", args=["mcp-server-fetch"]))

        assert conn["transport"] == "stdio"
        assert conn["command"] == "uvx"
        assert conn["args"] == ["mcp-server-fetch"]

    def test_stdio_env_included_when_set(self) -> None:
        conn = to_connection(MCPStdio(command="x", args=[], env={"A": "B"}))

        assert conn["env"] == {"A": "B"}

    def test_stdio_env_omitted_when_none(self) -> None:
        """Omitting env lets the adapters layer inherit the parent environment."""
        conn = to_connection(MCPStdio(command="x", args=[]))

        assert "env" not in conn

    def test_sse_transport(self) -> None:
        conn = to_connection(MCPSse(url="http://h/sse", headers={"X": "1"}))

        assert conn["transport"] == "sse"
        assert conn["url"] == "http://h/sse"
        assert conn["headers"] == {"X": "1"}

    def test_streamable_http_transport(self) -> None:
        conn = to_connection(MCPStreamableHttp(url="http://h/mcp"))

        assert conn["transport"] == "streamable_http"
        assert conn["url"] == "http://h/mcp"

    @pytest.mark.parametrize(
        "config",
        [
            MCPStdio(command="x", args=[]),
            MCPSse(url="http://h"),
            MCPStreamableHttp(url="http://h"),
        ],
    )
    def test_timeout_uses_read_timeout_seconds_kwarg(
        self, config: MCPStdio | MCPSse | MCPStreamableHttp
    ) -> None:
        """session_kwargs splats into mcp.ClientSession, which names it
        read_timeout_seconds. A `timeout` key raises TypeError at startup."""
        conn = to_connection(config.model_copy(update={"client_session_timeout_seconds": 42}))

        assert conn["session_kwargs"] == {"read_timeout_seconds": timedelta(seconds=42)}

    def test_timeout_omitted_when_none(self) -> None:
        conn = to_connection(MCPStdio(command="x", args=[], client_session_timeout_seconds=None))

        assert "session_kwargs" not in conn

    def test_default_timeout_present_by_default(self) -> None:
        """The 5s default from any-agent is preserved."""
        conn = to_connection(MCPStdio(command="x", args=[]))

        assert conn["session_kwargs"] == {"read_timeout_seconds": timedelta(seconds=5)}


class TestBrokerRoundTrip:
    def test_stdio_survives_dump_and_rehydrate(self) -> None:
        original = MCPStdio(
            command="node",
            args=["calc-server.js"],
            env={"K": "V"},
            client_session_timeout_seconds=300,
        )

        wire = json.loads(json.dumps(original.model_dump()))
        rehydrated = McpConnection.validate_python([wire])[0]

        assert isinstance(rehydrated, MCPStdio)
        assert rehydrated.command == "node"
        assert rehydrated.args == ["calc-server.js"]
        assert rehydrated.env == {"K": "V"}
        assert rehydrated.client_session_timeout_seconds == 300

    def test_http_survives_dump_and_rehydrate(self) -> None:
        original = MCPStreamableHttp(url="http://h/mcp", headers={"A": "B"})

        wire = json.loads(json.dumps(original.model_dump()))
        rehydrated = McpConnection.validate_python([wire])[0]

        assert isinstance(rehydrated, MCPStreamableHttp)
        assert rehydrated.url == "http://h/mcp"
        assert rehydrated.headers == {"A": "B"}

    def test_mixed_transports_round_trip(self) -> None:
        configs = [
            MCPStdio(command="a", args=[]),
            MCPSse(url="http://b"),
            MCPStreamableHttp(url="http://c"),
        ]
        wire = json.loads(json.dumps([c.model_dump() for c in configs]))

        rehydrated = McpConnection.validate_python(wire)

        assert [type(c) for c in rehydrated] == [MCPStdio, MCPSse, MCPStreamableHttp]

    def test_wire_format_is_backward_compatible(self) -> None:
        """A payload written by a pre-migration any-agent worker must rehydrate."""
        legacy = [
            {
                "command": "uvx",
                "args": ["mcp-server-fetch"],
                "env": None,
                "tools": None,
                "client_session_timeout_seconds": 300.0,
            }
        ]

        rehydrated = McpConnection.validate_python(legacy)[0]

        assert isinstance(rehydrated, MCPStdio)
        assert rehydrated.command == "uvx"
        assert rehydrated.args == ["mcp-server-fetch"]
        assert rehydrated.client_session_timeout_seconds == 300.0

    def test_legacy_url_payload_resolves_to_sse(self) -> None:
        """Legacy payloads carry no transport; a bare url means SSE.

        This is what the old ambiguous union did, so behaviour is unchanged.
        """
        legacy = [
            {
                "url": "http://h/sse",
                "headers": None,
                "tools": None,
                "client_session_timeout_seconds": 300.0,
            }
        ]

        rehydrated = McpConnection.validate_python(legacy)[0]

        assert isinstance(rehydrated, MCPSse)
        assert rehydrated.url == "http://h/sse"

    def test_streamable_http_is_not_misread_as_sse(self) -> None:
        """Regression: MCPSse and MCPStreamableHttp share a field set, so an
        undiscriminated union resolved every remote config to SSE and dialled
        streamable-HTTP servers on the wrong transport."""
        wire = json.loads(
            json.dumps(MCPStreamableHttp(url="http://h/mcp", headers={"A": "B"}).model_dump())
        )
        assert wire["transport"] == "streamable_http"

        rehydrated = McpConnection.validate_python([wire])[0]

        assert isinstance(rehydrated, MCPStreamableHttp)
        assert rehydrated.headers == {"A": "B"}
        assert to_connection(rehydrated)["transport"] == "streamable_http"

    def test_validates_live_instances(self) -> None:
        configs = [MCPStdio(command="a", args=[]), MCPStreamableHttp(url="http://b")]

        assert [type(c).__name__ for c in McpConnection.validate_python(configs)] == [
            "MCPStdio",
            "MCPStreamableHttp",
        ]


class TestFilterTools:
    def test_none_allowlist_returns_everything(self) -> None:
        tools = [_tool("a"), _tool("b")]

        assert filter_tools(tools, MCPStdio(command="x", args=[])) == tools

    def test_allowlist_selects_and_orders(self) -> None:
        tools = [_tool("a"), _tool("b"), _tool("c")]

        result = filter_tools(tools, MCPStdio(command="x", args=[], tools=["c", "a"]))

        assert [t.name for t in result] == ["c", "a"]

    def test_missing_allowlisted_tool_raises(self) -> None:
        tools = [_tool("a")]

        with pytest.raises(ValueError, match="missing=\\['b'\\]"):
            filter_tools(tools, MCPStdio(command="x", args=[], tools=["a", "b"]))

    def test_empty_toolset_with_allowlist_raises(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            filter_tools([], MCPStdio(command="x", args=[], tools=["a"]))


def test_tool_names() -> None:
    assert tool_names([_tool("alpha"), _tool("beta")]) == ["alpha", "beta"]


def test_tool_names_empty() -> None:
    assert tool_names([]) == []
