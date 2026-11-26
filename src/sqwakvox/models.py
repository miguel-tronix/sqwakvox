from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import BaseModel, Field


def extract_message(error_string: str) -> str:
    """Extract the most human-readable message from an error or JSON payload."""
    if not error_string:
        return error_string

    try:
        obj = json.loads(error_string)
        msg = _walk_for_message(obj)
        if msg:
            return msg
    except json.JSONDecodeError:
        pass

    for m in re.finditer(r"\{.*\}", error_string, re.DOTALL):
        try:
            obj = json.loads(m.group())
            msg = _walk_for_message(obj)
            if msg:
                return msg
        except (json.JSONDecodeError, ValueError):
            continue

    lines = [line.strip() for line in error_string.splitlines() if line.strip()]
    return lines[-1] if lines else error_string


def _walk_for_message(obj: object) -> str | None:
    if isinstance(obj, dict):
        if "message" in obj and isinstance(obj["message"], str):
            return obj["message"]
        for v in obj.values():
            result = _walk_for_message(v)
            if result:
                return result
    return None


@dataclass
class AgentResult:
    response: str = ""
    is_blocked: bool = False
    blocked_reason: str = ""
    pii_redacted_query: bool = False
    pii_redacted_response: bool = False
    math_discrepancies: list[str] | None = None
    success: bool = True
    error_message: str = ""

    def __post_init__(self) -> None:
        if self.math_discrepancies is None:
            self.math_discrepancies = []


class TableData(BaseModel):
    headers: list[str]
    rows: list[list[str]]
    title: str | None = None


class StructuredDocument(BaseModel):
    file_name: str
    raw_markdown: str
    tables: list[TableData] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelProvider:
    MAP: ClassVar[Mapping[str, Mapping[str, Any]]] = MappingProxyType(
        {
            model_id: MappingProxyType(meta)
            for model_id, meta in {
                "openai:gpt-5.5-high": {
                    "env_var": "OPENAI_API_KEY",
                    "friendly_name": "OpenAI GPT-5.5 High",
                    "supports_system_role": True,
                },
                "anthropic:claude-4.6": {
                    "env_var": "ANTHROPIC_API_KEY",
                    "friendly_name": "Anthropic Claude 4.6",
                    "supports_system_role": True,
                },
                "gemini:gemini-3.8-flash": {
                    "env_var": "GEMINI_API_KEY",
                    "friendly_name": "Google Gemini 3.8 Flash",
                    "supports_system_role": True,
                },
                "gemini:gemini-3.7-flash": {
                    "env_var": "GEMINI_API_KEY",
                    "friendly_name": "Google Gemini 3.7 Flash",
                    "supports_system_role": True,
                },
                "gemini:gemini-3.6-flash": {
                    "env_var": "GEMINI_API_KEY",
                    "friendly_name": "Google Gemini 3.6 Flash",
                    "supports_system_role": False,
                },
                "deepseek:deepseek-v4-flash": {
                    "env_var": "DEEPSEEK_API_KEY",
                    "friendly_name": "Deepseek v4 Flash",
                    "supports_system_role": True,
                },
                "deepseek:deepseek-v4-pro": {
                    "env_var": "DEEPSEEK_API_KEY",
                    "friendly_name": "Deepseek v4 Pro",
                    "supports_system_role": True,
                },
            }.items()
        }
    )

    @classmethod
    def get_env_var(cls, model_id: str) -> str:
        return str(cls.MAP.get(model_id, {}).get("env_var", "OPENAI_API_KEY"))

    @classmethod
    def supports_system_role(cls, model_id: str) -> bool:
        return bool(cls.MAP.get(model_id, {}).get("supports_system_role", True))

    @classmethod
    def resolve_key(cls, model_id: str) -> tuple[str, str]:
        """Resolve *model_id* and its API key from the environment.

        Falls back through the common provider env vars if the model's own
        env var is empty, remapping ``model_id`` to match whichever key is
        found (same behaviour the gateway used before keys stopped crossing
        the broker).
        """
        env_var = cls.get_env_var(model_id)
        api_key = os.environ.get(env_var, "").strip()
        if api_key:
            return model_id, api_key

        for alt_var, alt_model in (
            ("OPENAI_API_KEY", "openai:gpt-5.5-high"),
            ("ANTHROPIC_API_KEY", "anthropic:claude-4.6"),
            ("GEMINI_API_KEY", "gemini:gemini-3.8-flash"),
        ):
            val = os.environ.get(alt_var, "").strip()
            if val:
                return alt_model, val
        return model_id, ""
