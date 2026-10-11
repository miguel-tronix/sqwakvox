"""Web presenter — a browser view of the same session the TUI drives.

Serves a single-page app from :mod:`sqwakvox.web_ui` and exposes the Sqwakvox
presenter to the browser **through MCP**: every action the UI performs is a
``sqwakvox_presenter_*`` tool call against the in-process FastMCP server
(:mod:`sqwakvox.mcp_presenter`), so the web UI holds no private code path —
anything the TUI can do the browser can do, and any MCP client can drive the
same session.

Endpoints
---------
``GET  /``                the single-page app
``GET  /healthz``         liveness probe
``GET  /api/tools``       tool catalog + JSON Schemas (drives the generic forms)
``POST /api/call``        ``{"tool": ..., "arguments": {...}}`` → tool result
``GET  /api/events``      Server-Sent Events stream of session events

Security
--------
Binds to 127.0.0.1 by default, and rejects non-local ``Host`` headers so a
DNS-rebinding attack cannot reach it.  When ``SQWAKVOX_WEB_TOKEN`` is set (or
a non-loopback address is requested, in which case one is generated), every
``/api/*`` route requires ``Authorization: Bearer <token>``; the browser is
prompted once and keeps it in ``sessionStorage``.

Launch with::

    python -m sqwakvox.web_presenter                  # http://127.0.0.1:8760
    SQWAKVOX_WEB_TOKEN=secret python -m sqwakvox.web_presenter --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from sqwakvox.doc_session import DocSession, SessionEvent, get_session

logger = logging.getLogger(__name__)

#: Directory holding the single-page app.
WEB_DIR = Path(__file__).parent / "web_ui"

#: Seconds between SSE keep-alive comments when the session is idle.
SSE_KEEPALIVE = 15.0

#: Hosts accepted when ``--allow-remote`` is not set (DNS-rebinding guard).
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1", "testserver"})


class WebPresenter:
    """Owns the session, the MCP bridge, and the Starlette application."""

    def __init__(
        self,
        session: DocSession | None = None,
        *,
        token: str | None = None,
        allow_remote: bool = False,
        static_dir: Path | None = None,
    ) -> None:
        self.session = session if session is not None else get_session()
        self.token = (
            token if token is not None else os.environ.get("SQWAKVOX_WEB_TOKEN", "").strip()
        )
        self.allow_remote = allow_remote
        self.static_dir = static_dir or WEB_DIR
        self.app = self._build_app()

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def authorized(self, request: Request) -> bool:
        """True when *request* may use the API (no configured token = local dev)."""
        if not self.token:
            return True
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = request.query_params.get("token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def _forbidden(self) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": "Unauthorized: send 'Authorization: Bearer <token>'."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ------------------------------------------------------------------ #
    # MCP bridge
    # ------------------------------------------------------------------ #
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke an MCP tool in-process and return a JSON-safe result.

        Uses the in-process FastMCP client, so the web UI exercises exactly the
        code path an external MCP client would.
        """
        from fastmcp import Client

        from sqwakvox.mcp_presenter import mcp

        try:
            async with Client(mcp) as client:
                result = await client.call_tool(name, arguments or {})
        except Exception as exc:
            logger.error("MCP tool %s failed: %s", name, exc, exc_info=True)
            return {"ok": False, "tool": name, "error": str(exc)}
        return {
            "ok": True,
            "tool": name,
            "data": _structured(result),
            "text": _text_of(result),
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        """Tool catalog with JSON Schemas, used to build the generic forms."""
        from fastmcp import Client

        from sqwakvox.mcp_presenter import mcp

        try:
            async with Client(mcp) as client:
                tools = await client.list_tools()
        except Exception as exc:
            logger.error("Listing MCP tools failed: %s", exc, exc_info=True)
            return []
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema,
                "annotations": _dump(getattr(t, "annotations", None)),
            }
            for t in tools
        ]

    # ------------------------------------------------------------------ #
    # Routes
    # ------------------------------------------------------------------ #
    def _build_app(self) -> Starlette:
        index = self.static_dir / "index.html"

        async def home(_request: Request) -> Response:
            return FileResponse(index)

        async def healthz(_request: Request) -> Response:
            return JSONResponse(
                {
                    "ok": True,
                    "documents": len(self.session.documents()),
                    "active": self.session.active_document_name(),
                    "auth_required": bool(self.token),
                }
            )

        async def api_tools(request: Request) -> Response:
            if not self.authorized(request):
                return self._forbidden()
            # Anyio threads for the MCP client and its polling, so a long
            # tool call never stalls the SSE stream on the same worker.
            tools = await _offload(self.list_tools())
            return JSONResponse({"ok": True, "tools": tools, "session": self.session.snapshot()})

        async def api_call(request: Request) -> Response:
            if not self.authorized(request):
                return self._forbidden()
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body."}, status_code=400)
            name = str(body.get("tool") or "")
            arguments = body.get("arguments") or {}
            if not name:
                return JSONResponse({"ok": False, "error": "Missing 'tool'."}, status_code=400)
            if not isinstance(arguments, dict):
                return JSONResponse(
                    {"ok": False, "error": "'arguments' must be an object."}, status_code=400
                )
            result = await _offload(self.call_tool(name, arguments))
            return JSONResponse(result)

        async def api_upload(request: Request) -> Response:
            if not self.authorized(request):
                return self._forbidden()
            try:
                form = await request.form()
            except Exception as exc:
                return JSONResponse(
                    {"ok": False, "error": f"Invalid form data: {exc}"}, status_code=400
                )
            upload = form.get("file")
            if not isinstance(upload, UploadFile) or not upload.filename:
                return JSONResponse({"ok": False, "error": "No file uploaded."}, status_code=400)
            filename = Path(upload.filename).name
            if not filename:
                return JSONResponse({"ok": False, "error": "Invalid filename."}, status_code=400)
            uploads_dir = Path.home() / ".sqwakvox" / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            target = uploads_dir / filename
            try:
                with target.open("wb") as fh:
                    while chunk := await upload.read(1024 * 1024):
                        fh.write(chunk)
            except Exception as exc:
                logger.error("Failed to write uploaded file %s: %s", target, exc, exc_info=True)
                return JSONResponse(
                    {"ok": False, "error": f"Failed to save file: {exc}"}, status_code=500
                )
            return JSONResponse({"ok": True, "path": str(target.resolve()), "file_name": filename})

        async def api_events(request: Request) -> Response:
            if not self.authorized(request):
                return self._forbidden()
            since_seq = _as_int(request.query_params.get("since_seq"), 0)
            return StreamingResponse(
                self._event_stream(since_seq),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        routes: list[BaseRoute] = [
            Route("/", home),
            Route("/healthz", healthz),
            Route("/api/tools", api_tools),
            Route("/api/call", api_call, methods=["POST"]),
            Route("/api/upload", api_upload, methods=["POST"]),
            Route("/api/events", api_events),
        ]
        if self.static_dir.is_dir():
            routes.append(
                Mount("/static", StaticFiles(directory=str(self.static_dir)), name="static")
            )

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> Any:
            yield
            # Clear the Redis session registry so external MCP clients do not
            # keep pointing at a session this view has stopped driving.
            with contextlib.suppress(Exception):
                self.session.publish_session_clear()
            with contextlib.suppress(Exception):
                self.session.close()

        middleware: list[Middleware] = []
        if not self.allow_remote:
            middleware.append(Middleware(BaseHTTPMiddleware, dispatch=self._host_guard))
        return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

    async def _host_guard(self, request: Request, call_next: Any) -> Any:
        """Reject non-local ``Host`` headers (DNS-rebinding protection)."""
        raw_host = request.headers.get("host", "").strip()
        if raw_host.startswith("["):
            end = raw_host.find("]")
            host = raw_host[: end + 1] if end != -1 else raw_host
        else:
            host = raw_host.split(":")[0].strip()
        if not host or host not in LOCAL_HOSTS:
            return JSONResponse(
                {"ok": False, "error": "Host not allowed (use localhost, or --allow-remote)."},
                status_code=403,
            )
        return await call_next(request)

    # ------------------------------------------------------------------ #
    # SSE
    # ------------------------------------------------------------------ #
    async def _event_stream(self, since_seq: int) -> Any:
        """Yield session events as SSE, replaying anything missed first."""
        loop = asyncio.get_running_loop()
        pending: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=2048)

        def _sink(event: SessionEvent) -> None:
            # The sink fires on the session's own loop thread.
            loop.call_soon_threadsafe(pending.put_nowait, event)

        # Replay the buffered tail so a late-connecting browser is not blind.
        backlog = self.session.events_since(since_seq)
        unsubscribe = self.session.subscribe(_sink)
        try:
            yield _sse("hello", {"session": self.session.snapshot(), "backlog": len(backlog)})
            for event in backlog:
                yield _sse(event.kind, event.as_dict())
            while True:
                try:
                    event = await asyncio.wait_for(pending.get(), timeout=SSE_KEEPALIVE)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event.kind, event.as_dict())
        finally:
            unsubscribe()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _offload(coro: Any) -> Any:
    """Await *coro* on a worker thread, keeping the event loop free.

    Tool listing and long tool calls would otherwise block the SSE stream when
    they land on the same single-threaded worker.
    """
    import anyio

    return await anyio.to_thread.run_sync(functools.partial(asyncio.run, coro))


def _dump(value: Any) -> Any:
    """Best-effort JSON conversion of pydantic models / enums."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    for attr in ("model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            with contextlib.suppress(Exception):
                return fn()
    with contextlib.suppress(Exception):
        return json.loads(json.dumps(value, default=str))
    return str(value)


def _text_of(result: Any) -> str:
    """Concatenate an MCP tool result's text content."""
    return "\n".join(
        text
        for item in (getattr(result, "content", None) or [])
        if (text := getattr(item, "text", None))
    )


def _structured(result: Any) -> Any:
    """Structured payload of an MCP tool result.

    The presenter tools answer with JSON *text* (so MCP clients and models both
    get something readable), so the text content is parsed first and the
    structured channel is only a fallback.
    """
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    for attr in ("structured_content", "data"):
        value = getattr(result, attr, None)
        if value not in (None, {}, ""):
            return value
    return {"result": _text_of(result)}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Entry-point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the web presenter with uvicorn."""
    import uvicorn

    parser = argparse.ArgumentParser(description="Sqwakvox web presenter")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8760, help="Bind port (default: 8760)")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow non-local Host headers (behind a reverse proxy)",
    )
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="Ignore SQWAKVOX_WEB_TOKEN and serve the API unauthenticated",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    token = "" if args.no_token else os.environ.get("SQWAKVOX_WEB_TOKEN", "").strip()
    if not token and args.host not in ("127.0.0.1", "localhost"):
        # Bind a strong token rather than exposing an open API on 0.0.0.0.
        token = secrets.token_urlsafe(32)
        logger.warning("SQWAKVOX_WEB_TOKEN is unset; generated API token: %s", token)

    presenter = WebPresenter(token=token, allow_remote=args.allow_remote)
    logger.info("Sqwakvox web presenter listening on http://%s:%d", args.host, args.port)
    uvicorn.run(presenter.app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
