"""Skills storage for the SWE domain.

Skills are Markdown files with YAML frontmatter, matching the repo's existing
``.agents/skills/*/SKILL.md`` convention.  They are written to
``./skills/<domain_id>/<skill-name>/SKILL.md`` relative to the current
working directory (override with ``SQWAKVOX_SKILLS_DIR``), so skills created
by the assistant persist on disk in a standard, reusable format.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqwakvox import storage

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_SKILL_CHARS = 32_000


def skills_root(domain_id: str = "swe") -> Path:
    """Return the writable skills root for *domain_id*."""
    return storage.skills_base_dir() / domain_id


def skill_path(domain_id: str, name: str) -> Path:
    """Return the SKILL.md path for a skill."""
    return skills_root(domain_id) / name / "SKILL.md"


def list_skills(domain_id: str = "swe") -> list[dict[str, str]]:
    """List skills as ``[{name, description, path}]``."""
    root = skills_root(domain_id)
    if not root.exists():
        return []
    skills: list[dict[str, str]] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        md_path = entry / "SKILL.md"
        if not md_path.exists():
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, _body = _split_frontmatter(text)
        skills.append(
            {
                "name": entry.name,
                "description": frontmatter.get("description", ""),
                "path": str(md_path),
            }
        )
    return skills


def read_skill(name: str, domain_id: str = "swe") -> str | None:
    """Return the raw SKILL.md content for *name*, or None."""
    path = skill_path(domain_id, name)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def create_skill(name: str, description: str, content: str, domain_id: str = "swe") -> Path:
    """Create (or overwrite) a skill file, validating name, size, and secrets."""
    _validate_skill(name, description, content)
    path = skill_path(domain_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ensure_frontmatter(name, description, content), encoding="utf-8")
    return path


def update_skill(name: str, description: str, content: str, domain_id: str = "swe") -> Path:
    """Update an existing skill (same validation as create)."""
    if not skill_path(domain_id, name).exists():
        raise ValueError(f"Skill '{name}' does not exist")
    return create_skill(name, description, content, domain_id=domain_id)


def delete_skill(name: str, domain_id: str = "swe") -> bool:
    """Remove a skill directory; returns True if something was deleted."""
    path = skill_path(domain_id, name)
    if not path.exists():
        return False
    import shutil

    shutil.rmtree(path.parent)
    return True


def search_skills(query: str, domain_id: str = "swe") -> list[dict[str, str]]:
    """Filter skills by a case-insensitive match on name or description."""
    q = query.lower()
    return [
        s for s in list_skills(domain_id) if q in s["name"].lower() or q in s["description"].lower()
    ]


# --------------------------------------------------------------------------- #
# Validation + frontmatter helpers
# --------------------------------------------------------------------------- #


def _validate_skill(name: str, description: str, content: str) -> None:
    if not SKILL_NAME_RE.match(name):
        raise ValueError(
            f"Invalid skill name '{name}': use lowercase letters, digits, hyphens "
            "(start with a letter or digit)."
        )
    if len(content) > MAX_SKILL_CHARS:
        raise ValueError(f"Skill content too large ({len(content)} chars; max {MAX_SKILL_CHARS}).")
    if not description.strip():
        raise ValueError("Skill description is required (one line, when to use it).")

    from sqwakvox.domains.swe.guardrails import redact_secrets

    if redact_secrets(content) != content:
        raise ValueError("Skill content looks like it contains secrets; refusing to store it.")


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse a ``---``-delimited YAML-ish frontmatter block (name/description)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    frontmatter: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip().strip("'\"")
    body = "\n".join(lines[end + 1 :]).strip()
    return frontmatter, body


def _ensure_frontmatter(name: str, description: str, content: str) -> str:
    """Normalise *content* so it always carries name/description frontmatter."""
    safe_description = description.replace("'", "\\'")
    if content.lstrip().startswith("---"):
        _fm, body = _split_frontmatter(content)
        return f"---\nname: {name}\ndescription: '{safe_description}'\n---\n\n{body}\n"
    return f"---\nname: {name}\ndescription: '{safe_description}'\n---\n\n{content}\n"
