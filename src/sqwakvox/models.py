from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel


class TableData(BaseModel):
    headers: list[str]
    rows: list[list[str]]
    title: str | None = None


class StructuredDocument(BaseModel):
    file_name: str
    raw_markdown: str
    tables: list[TableData] = []
    metadata: dict[str, Any] = {}


class ModelProvider:
    MAP: ClassVar[dict[str, dict[str, Any]]] = {
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
        "gemini:gemini-3.6-flash": {
            "env_var": "GEMINI_API_KEY",
            "friendly_name": "Google Gemini 3.6 Flash",
            "supports_system_role": False,
        },
        "gemini:gemini-3.5-flash": {
            "env_var": "GEMINI_API_KEY",
            "friendly_name": "Google Gemini 3.5 Flash",
            "supports_system_role": True,
        },
        "gemini:gemini-3.5-pro": {
            "env_var": "GEMINI_API_KEY",
            "friendly_name": "Google Gemini 3.5 Pro",
            "supports_system_role": True,
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
    }

    @classmethod
    def get_env_var(cls, model_id: str) -> str:
        return str(cls.MAP.get(model_id, {}).get("env_var", "OPENAI_API_KEY"))

    @classmethod
    def supports_system_role(cls, model_id: str) -> bool:
        return bool(cls.MAP.get(model_id, {}).get("supports_system_role", True))
