"""Tests for the skills MCP server tools (thin wrappers over the skills store)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sqwakvox import mcp_skills_server as skills_mcp


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SQWAKVOX_SKILLS_DIR", str(tmp_path))


def test_list_skills_empty(_env: None) -> None:
    result = skills_mcp.list_skills()
    assert '"name"' not in result


def test_create_read_update_delete_flow(_env: None) -> None:
    created = skills_mcp.create_skill("api-pattern", "Use for API design", "# API Pattern\n\nBody.")
    assert "Created skill" in created

    listed = skills_mcp.list_skills()
    assert "api-pattern" in listed

    read = skills_mcp.read_skill("api-pattern")
    assert "API Pattern" in read

    updated = skills_mcp.update_skill("api-pattern", "Use for API design", "# API Pattern v2")
    assert "Updated skill" in updated

    deleted = skills_mcp.delete_skill("api-pattern")
    assert "Deleted skill" in deleted
    assert skills_mcp.read_skill("api-pattern").startswith("Error")


def test_search_skill(_env: None) -> None:
    skills_mcp.create_skill("db-role", "read-only db roles", "body")
    result = skills_mcp.search_skills("read-only")
    assert "db-role" in result


def test_invalid_skill_reports_error(_env: None) -> None:
    result = skills_mcp.create_skill("Bad Name", "d", "body")
    assert result.startswith("Error:")


def test_read_only_mode_blocks_mutations(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQWAKVOX_MCP_READ_ONLY", "1")
    created = skills_mcp.create_skill("ro-skill", "desc", "body")
    assert created.startswith("Error: read-only")
    assert skills_mcp.update_skill("ro-skill", "d", "c").startswith("Error: read-only")
    assert skills_mcp.delete_skill("ro-skill").startswith("Error: read-only")
    # reads still allowed
    assert isinstance(skills_mcp.list_skills(), str)
    assert isinstance(skills_mcp.search_skills("anything"), str)
    assert isinstance(skills_mcp.read_skill("ro-skill"), str)
