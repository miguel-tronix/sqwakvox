"""Broker-safe MCP server configuration and tool loading.

These models replace the equivalents that used to come from ``any_agent.config``.
They exist because MCP configs are serialised through the Celery broker
(:mod:`sqwakvox.app` -> :mod:`sqwakvox.backend.tasks` -> :mod:`sqwakvox.agent`),
and pydantic models round-trip through JSON while the ``TypedDict`` connection
shapes from ``langchain-mcp-adapters`` do not.

The wire format is unchanged from the any-agent models: same field names, same
types, same defaults.  Broker payloads written by an older worker therefore
rehydrate cleanly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias, cast

from langchain_core.tools import BaseTool
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter

if TYPE_CHECKING:
    from langchain_mcp_adapters.sessions import (
        SSEConnection,
        StdioConnection,
        StreamableHttpConnection,
        WebsocketConnection,
    )

__all__ = [
    "Connection",
    "MCPConnectionError",
    "MCPParams",
    "MCPSse",
    "MCPStdio",
    "MCPStreamableHttp",
    "McpConnection",
    "connected_tools",
    "filter_tools",
    "to_connection",
    "tool_names",
]

Connection: TypeAlias = (
    "StdioConnection | SSEConnection | StreamableHttpConnection | WebsocketConnection"
)
"""A ``langchain-mcp-adapters`` connection shape (union of its ``TypedDict``s)."""


class _MCPConfigBase(BaseModel):
    """Fields shared by every transport."""

    model_config = ConfigDict(frozen=True)

    tools: Sequence[str] | None = None
    """Tool names to expose from this server.

    When set, only these tools are registered and a missing name is an error.
    When ``None``, every tool the server advertises is registered.
    """

    client_session_timeout_seconds: float | None = 5
    """Read timeout passed to the MCP ``ClientSession``."""


class MCPStdio(_MCPConfigBase):
    """A locally-spawned MCP server communicating over stdio pipes."""

    transport: Literal["stdio"] = "stdio"
    command: str
    """Executable that starts the server, e.g. ``uvx``, ``npx``, ``python``."""

    args: Sequence[str] = Field(default_factory=tuple)
    """Command line arguments passed to ``command``."""

    env: Mapping[str, str] | None = None
    """Environment overrides for the server process.

    The parent environment is inherited regardless; this only adds to it.
    """


class MCPSse(_MCPConfigBase):
    """A remote MCP server over SSE.

    Deprecated in the MCP specification since 2025-03-26 in favour of
    :class:`MCPStreamableHttp`.  Kept because ``mcp_servers.json`` may still
    declare ``"transport": "sse"``.
    """

    transport: Literal["sse"] = "sse"
    url: str
    headers: Mapping[str, str] | None = None


class MCPStreamableHttp(_MCPConfigBase):
    """A remote MCP server over streamable HTTP."""

    transport: Literal["streamable_http"] = "streamable_http"
    url: str
    headers: Mapping[str, str] | None = None


MCPParams: TypeAlias = MCPStdio | MCPSse | MCPStreamableHttp
"""Union of every supported transport, discriminated on the ``transport`` field.

The ``transport`` literal is what makes rehydration unambiguous.  Without it
``MCPSse`` and ``MCPStreamableHttp`` have identical field sets, and a pydantic
union silently resolves every remote config to whichever member is declared
first - a streamable-HTTP server would be dialled as SSE.
"""


def _infer_transport(configs: Any) -> Any:
    """Fill in a missing ``transport`` on each config so the union tag resolves.

    Payloads written by a pre-migration any-agent worker carry no ``transport``
    field.  Infer from shape: a ``command`` means stdio, and a bare ``url`` is
    treated as SSE, which is what the old ambiguous union resolved those to.

    This has to wrap the whole list, not each member: a per-member validator
    would run after the union tag has already been extracted.
    """
    if not isinstance(configs, list):
        return configs
    inferred = []
    for config in configs:
        if isinstance(config, dict) and "transport" not in config:
            if "command" in config:
                config = {**config, "transport": "stdio"}
            elif "url" in config:
                config = {**config, "transport": "sse"}
        inferred.append(config)
    return inferred


McpConnection: TypeAdapter[list[MCPParams]] = TypeAdapter(
    Annotated[
        list[Annotated[MCPParams, Field(discriminator="transport")]],
        BeforeValidator(_infer_transport),
    ]
)
"""Adapter used to rehydrate broker payloads back into :data:`MCPParams`."""


def to_connection(config: MCPParams) -> Connection:
    """Convert *config* to a ``langchain-mcp-adapters`` connection dict.

    The adapters package uses ``TypedDict`` connection shapes rather than
    pydantic models, so the conversion happens here at the boundary and the
    pydantic models stay the single source of truth everywhere else.
    """
    if isinstance(config, MCPStdio):
        raw: dict[str, Any] = {
            "transport": "stdio",
            "command": config.command,
            "args": list(config.args),
        }
        if config.env:
            raw["env"] = dict(config.env)
        typed: Connection = cast("StdioConnection", raw)
    elif isinstance(config, MCPSse):
        raw = {"transport": "sse", "url": config.url}
        if config.headers:
            raw["headers"] = dict(config.headers)
        typed = cast("SSEConnection", raw)
    elif isinstance(config, MCPStreamableHttp):
        raw = {"transport": "streamable_http", "url": config.url}
        if config.headers:
            raw["headers"] = dict(config.headers)
        typed = cast("StreamableHttpConnection", raw)
    else:
        raise TypeError(f"Unsupported MCP config type: {type(config)!r}")

    if config.client_session_timeout_seconds is not None:
        # session_kwargs is splatted straight into mcp.ClientSession, whose
        # read-timeout parameter is read_timeout_seconds (a timedelta, for
        # every transport). Do not confuse it with the SSE/HTTP-level
        # `timeout`, which is a separate connection field.
        raw["session_kwargs"] = {
            "read_timeout_seconds": timedelta(seconds=config.client_session_timeout_seconds)
        }
    return typed


def filter_tools(tools: list[BaseTool], config: MCPParams) -> list[BaseTool]:
    """Restrict *tools* to the allowlist on *config*.

    Raises ``ValueError`` when an allowlisted tool is absent, so a typo in
    ``mcp_servers.json`` surfaces at startup instead of silently shrinking the
    agent's toolset.
    """
    requested = list(config.tools or [])
    if not requested:
        return tools

    by_name = {tool.name: tool for tool in tools}
    missing = [name for name in requested if name not in by_name]
    if missing:
        raise ValueError(
            f"Could not find all requested tools in the MCP server "
            f"{config!r}: requested={requested}, "
            f"available={list(by_name)}, missing={missing}"
        )
    return [by_name[name] for name in requested]


def tool_names(tools: Sequence[BaseTool]) -> list[str]:
    """Names of *tools*, for logging."""
    return [getattr(tool, "name", str(tool)) for tool in tools]


@asynccontextmanager
async def connected_tools(
    configs: Sequence[MCPParams],
) -> AsyncIterator[list[BaseTool]]:
    """Yield the combined tools of every server in *configs*.

    Sessions are opened one at a time through an ``AsyncExitStack`` so that each
    server's ``tools`` allowlist can be applied to its own tools (see
    :func:`filter_tools`) and so every session stays open for the whole run.
    Sessions must outlive tool calls: a stdio server's subprocess pipes are
    bound to the session that spawned them, and closing early leaves the tools
    dead mid-run.  The caller must therefore keep this ``async with`` block
    open for the duration of the agent run.

    Note ``MultiServerMCPClient`` is deliberately not used as a context manager:
    langchain-mcp-adapters 0.1.0 removed that and raises ``NotImplementedError``.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    if not configs:
        yield []
        return

    client = MultiServerMCPClient(
        connections={f"server_{i}": to_connection(cfg) for i, cfg in enumerate(configs)}
    )

    collected: list[BaseTool] = []
    async with AsyncExitStack() as stack:
        for index, config in enumerate(configs):
            server_name = f"server_{index}"
            try:
                session = await stack.enter_async_context(client.session(server_name))
                tools = await load_mcp_tools(session, server_name=server_name)
            except Exception as exc:
                raise MCPConnectionError(
                    f"MCP server {server_name} failed to start: {exc}"
                ) from exc
            collected.extend(filter_tools(tools, config))
        yield collected


class MCPConnectionError(RuntimeError):
    """Raised when an MCP server cannot be reached or initialised."""
