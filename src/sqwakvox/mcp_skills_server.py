"""MCP Server: Skills CRUD for the SWE document domain.

Lets the agent author, read, update, search, and delete reusable skill files
during a chat session.  Skills are stored as ``SKILL.md`` files with YAML
frontmatter under ``./skills/<domain>/<skill-name>/`` (see
:mod:`sqwakvox.domains.swe.skills`) — the same convention used by the repo's
``.agents/skills/`` directory, so anything that reads that format can reuse
them.

Launch with:
    python -m sqwakvox.mcp_skills_server
"""

from __future__ import annotations

import json
import logging
import os
import time

from fastmcp import FastMCP

from sqwakvox.domains.swe import skills as skills_store
from sqwakvox.mcp_http import run_with_http_guard

logger = logging.getLogger(__name__)

mcp = FastMCP("sqwakvox-skills")

#: Domains whose skills this server manages.
DEFAULT_DOMAIN = "swe"


def _is_read_only() -> bool:
    """True when ``SQWAKVOX_MCP_READ_ONLY`` is set to a truthy value."""
    return os.environ.get("SQWAKVOX_MCP_READ_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_only_blocked(action: str) -> str | None:
    if _is_read_only():
        return f"Error: read-only mode is enabled (SQWAKVOX_MCP_READ_ONLY); cannot {action}."
    return None


def _trace_tool(tool_name: str, fn: object) -> str:
    """Record tool usage in telemetry, then run *fn* (mirrors calc server)."""
    from sqwakvox.telemetry import get_telemetry, trace_span

    tm = get_telemetry()
    start = time.monotonic()
    with trace_span("sqwakvox.mcp.skills", {"tool": tool_name}):
        try:
            result = fn()  # type: ignore[operator]
        except Exception as exc:
            logger.error("Skills tool %s failed: %s", tool_name, exc, exc_info=True)
            if tm.mcp_tool_counter:
                tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "failure"})
            return f"Error: tool '{tool_name}' failed. Check server logs for details."
    if tm.mcp_tool_counter:
        tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "success"})
    if tm.mcp_tool_duration:
        tm.mcp_tool_duration.record(time.monotonic() - start, {"tool": tool_name})
    return result  # type: ignore[no-any-return]


@mcp.tool(
    name="list_skills",
    description=(
        "List the available skills (name + description) stored for the SWE "
        "domain. Use this to see what reusable knowledge already exists."
    ),
    annotations={"readOnlyHint": True},
)
def list_skills() -> str:
    """Return all stored skills as JSON."""

    def _run() -> str:
        return json.dumps(skills_store.list_skills(DEFAULT_DOMAIN), indent=2)

    return _trace_tool("list_skills", _run)


@mcp.tool(
    name="read_skill",
    description=(
        "Read the full content of a skill by name. Use before following or editing a skill."
    ),
    annotations={"readOnlyHint": True},
)
def read_skill(name: str) -> str:
    """Return the SKILL.md body for *name*."""

    def _run() -> str:
        content = skills_store.read_skill(name, DEFAULT_DOMAIN)
        if content is None:
            return f"Error: skill '{name}' not found"
        return content

    return _trace_tool("read_skill", _run)


@mcp.tool(
    name="create_skill",
    description=(
        "Create a reusable skill file. Args: name (lowercase letters/digits/"
        "hyphens), description (one line: when to use it), content (the skill "
        "body in Markdown — concise, imperative, one concern per skill). "
        "Persists to ./skills/swe/<name>/SKILL.md."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
def create_skill(name: str, description: str, content: str) -> str:
    """Persist a new skill file."""

    def _run() -> str:
        blocked = _read_only_blocked("create skill")
        if blocked:
            return blocked
        path = skills_store.create_skill(name, description, content, DEFAULT_DOMAIN)
        return f"Created skill '{name}' at {path}"

    return _trace_tool("create_skill", _run)


@mcp.tool(
    name="update_skill",
    description=(
        "Update an existing skill's content/description. Args: name, "
        "description, content — same validation as create_skill."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": True},
)
def update_skill(name: str, description: str, content: str) -> str:
    """Overwrite an existing skill file."""

    def _run() -> str:
        blocked = _read_only_blocked("update skill")
        if blocked:
            return blocked
        path = skills_store.update_skill(name, description, content, DEFAULT_DOMAIN)
        return f"Updated skill '{name}' at {path}"

    return _trace_tool("update_skill", _run)


@mcp.tool(
    name="delete_skill",
    description="Delete a skill by name from the writable skills directory.",
    annotations={"readOnlyHint": False, "destructiveHint": True},
)
def delete_skill(name: str) -> str:
    """Remove a skill file."""

    def _run() -> str:
        blocked = _read_only_blocked("delete skill")
        if blocked:
            return blocked
        deleted = skills_store.delete_skill(name, DEFAULT_DOMAIN)
        return f"Deleted skill '{name}'" if deleted else f"Error: skill '{name}' not found"

    return _trace_tool("delete_skill", _run)


@mcp.tool(
    name="search_skills",
    description=(
        "Search skills by a case-insensitive match on name or description. "
        "Returns matching skills as JSON."
    ),
    annotations={"readOnlyHint": True},
)
def search_skills(query: str) -> str:
    """Return skills matching *query*."""

    def _run() -> str:
        return json.dumps(skills_store.search_skills(query, DEFAULT_DOMAIN), indent=2)

    return _trace_tool("search_skills", _run)


# --------------------------------------------------------------------------- #
# Entry-point
# --------------------------------------------------------------------------- #


def main() -> None:
    """Run the skills MCP server (default: stdio; HTTP needs opt-in)."""
    import argparse

    parser = argparse.ArgumentParser(description="Sqwakvox skills MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport to use (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for sse/http transport")
    parser.add_argument("--port", type=int, default=8000, help="Port for sse/http transport")
    args = parser.parse_args()

    run_with_http_guard(mcp, args, server_name="skills")


if __name__ == "__main__":
    main()
