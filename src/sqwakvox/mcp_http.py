"""Shared HTTP transport guard for Sqwakvox sibling MCP servers.

The calc / skills / retrieval servers default to stdio. Running them over
SSE or streamable-HTTP requires an explicit opt-in:

* ``SQWAKVOX_MCP_ALLOW_HTTP=1`` — acknowledges that the server will listen
  on a network socket.
* ``SQWAKVOX_MCP_HTTP_TOKEN`` (non-empty) — shared bearer token; every
  request must send ``Authorization: Bearer <token>``.

Without both, ``--transport sse|http`` exits with a clear error instead of
binding an unauthenticated port.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from fastmcp.server.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

ENV_ALLOW = "SQWAKVOX_MCP_ALLOW_HTTP"
ENV_TOKEN = "SQWAKVOX_MCP_HTTP_TOKEN"


class _BearerTokenVerifier(TokenVerifier):
    """Static bearer-token verifier for the sibling MCP servers.

    Subclasses FastMCP's :class:`~fastmcp.server.auth.TokenVerifier` (the
    ``AuthProvider`` fastmcp 3.x builds its auth middleware from) and accepts
    only the exact shared token, compared in constant time.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        import hmac

        if token and hmac.compare_digest(token, self._token):
            return AccessToken(
                token=token,
                client_id="sqwakvox-mcp",
                scopes=[],
                expires_at=None,
            )
        return None


def http_opt_in_error(server_name: str) -> str | None:
    """Return an error message if HTTP transport is not fully enabled."""
    if os.environ.get(ENV_ALLOW, "").strip() not in {"1", "true", "yes", "on"}:
        return (
            f"HTTP/SSE transport for the {server_name} MCP server is disabled. "
            f"Set {ENV_ALLOW}=1 to opt in (and {ENV_TOKEN} to a secret value)."
        )
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not token:
        return (
            f"HTTP/SSE transport for the {server_name} MCP server requires a "
            f"non-empty {ENV_TOKEN} env var (bearer token)."
        )
    return None


def run_with_http_guard(mcp: Any, args: Any, *, server_name: str) -> None:
    """Run *mcp* honouring *args.transport* with the HTTP opt-in guard.

    *args* is an argparse Namespace with ``transport``, ``host``, ``port``.
    """
    transport = getattr(args, "transport", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    err = http_opt_in_error(server_name)
    if err:
        logger.error(err)
        sys.stderr.write(err + "\n")
        raise SystemExit(2)

    token = os.environ.get(ENV_TOKEN, "").strip()
    # Lazy-import so stdio-only runs don't pull the auth stack.
    from fastmcp import FastMCP  # noqa: F401 — type anchor only

    verifier = _BearerTokenVerifier(token)
    # Attach to the existing server instance (auth is consulted for HTTP
    # transports; stdio above never reaches this path).
    mcp.auth = verifier

    transport_kw: dict[str, Any] = {
        "host": getattr(args, "host", "127.0.0.1"),
        "port": getattr(args, "port", 8000),
    }
    if transport == "sse":
        mcp.run(transport="sse", **transport_kw)
    else:
        mcp.run(transport="streamable-http", **transport_kw)
