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
