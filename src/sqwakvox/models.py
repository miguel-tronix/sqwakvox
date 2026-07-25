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
    MAP: ClassVar[dict[str, dict[str, str]]] = {
        "openai:gpt-5.5": {"env_var": "OPENAI_API_KEY", "friendly_name": "OpenAI GPT-5.5"},
        "anthropic:claude-4.6": {
            "env_var": "ANTHROPIC_API_KEY",
            "friendly_name": "Anthropic Claude 4.6",
        },
        "google:gemini-3.5": {
            "env_var": "GEMINI_API_KEY",
            "friendly_name": "Google Gemini 3.5",
        },
        "deepseek:v4": {
            "env_var": "DEEPSEEK_API_KEY",
            "friendly_name": "Deepseek v4",
        },
        "openai:gpt-4o-mini": {
            "env_var": "OPENAI_API_KEY",
            "friendly_name": "OpenAI GPT-4o-Mini",
        },
        "gemini:gemini-2.5-pro": {
            "env_var": "GEMINI_API_KEY",
            "friendly_name": "Gemini 2.5 Pro",
        },
        "gemini:gemini-2.5-flash": {
            "env_var": "GEMINI_API_KEY",
            "friendly_name": "Gemini 2.5 Flash",
        },
    }

    @classmethod
    def get_env_var(cls, model_id: str) -> str:
        return cls.MAP.get(model_id, {}).get("env_var", "OPENAI_API_KEY")
