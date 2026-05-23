from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class TableData(BaseModel):
    headers: List[str]
    rows: List[List[str]]
    title: Optional[str] = None


class StructuredDocument(BaseModel):
    file_name: str
    raw_markdown: str
    tables: List[TableData] = []
    metadata: dict = {}


class ModelProvider:
    MAP = {
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
    }

    @classmethod
    def get_env_var(cls, model_id: str) -> str:
        return cls.MAP.get(model_id, {}).get("env_var", "OPENAI_API_KEY")
