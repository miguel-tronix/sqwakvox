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
        "openai:gpt-4o": {"env_var": "OPENAI_API_KEY", "friendly_name": "OpenAI GPT-4o"},
        "openai:gpt-4o-mini": {
            "env_var": "OPENAI_API_KEY", "friendly_name": "OpenAI GPT-4o-Mini",
        },
        "anthropic:claude-3-5-sonnet": {
            "env_var": "ANTHROPIC_API_KEY", "friendly_name": "Anthropic Claude 3.5 Sonnet",
        },
        "mistral:mistral-small-latest": {
            "env_var": "MISTRAL_API_KEY", "friendly_name": "Mistral Small",
        },
        "gemini:gemini-1.5-pro": {
            "env_var": "GEMINI_API_KEY", "friendly_name": "Gemini 1.5 Pro",
        },
        "gemini:gemini-1.5-flash": {
            "env_var": "GEMINI_API_KEY", "friendly_name": "Gemini 1.5 Flash",
        },
    }

    @classmethod
    def get_env_var(cls, model_id: str) -> str:
        return cls.MAP.get(model_id, {}).get("env_var", "OPENAI_API_KEY")
