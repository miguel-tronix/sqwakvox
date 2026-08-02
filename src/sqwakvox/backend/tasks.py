"""Celery tasks for the Sqwakvox backend.

Each task wraps a synchronous, CPU/IO-bound operation from
:class:`~sqwakvox.controller.AppController`.  Keeping them as plain functions
with explicit arguments (no model-bound objects that can't be JSON-serialised
through the broker) ensures Celery can always pickle the call graph.

The presenter talks to these tasks via ``AsyncResult`` polling.  Every task
returns a JSON-serialisable result (Pydantic ``model_dump`` for documents).
"""

from __future__ import annotations

import logging
from typing import Any

from any_agent.config import MCPParams
from celery import shared_task
from pydantic import TypeAdapter

from sqwakvox.backend.celery_app import celery_app  # noqa: F401 — registers tasks
from sqwakvox.controller import AgentResult, AppController
from sqwakvox.guardrails import FinancialValue
from sqwakvox.models import StructuredDocument

logger = logging.getLogger(__name__)

MCP_SERVERS_ADAPTER: TypeAdapter[list[MCPParams]] = TypeAdapter(list[MCPParams])


def _get_controller() -> AppController:
    """Create a fresh controller per process.

    ``DocumentConverter`` is heavyweight and carries open file handles, so we
    lazily instantiate it once per worker process via the task closure.
    """
    return AppController()


@shared_task(bind=True, name="sqwakvox.backend.tasks.convert_document")  # type: ignore[untyped-decorator]
def convert_document(self: Any, source: str) -> dict[str, Any] | None:
    """Parse *source* (path/URL) into a :class:`StructuredDocument`.

    Returns the document as a plain dict (``model_dump``) so it can travel
    through the result backend, or ``None`` if the worker was revoked.
    """
    controller = _get_controller()
    task_id = self.request.id

    # A revocation sentinel: if the task was revoked before we even started
    # running, Celery will still call the body.  Check explicitly.
    # ``is_revoked`` requires a result backend, so guard against environments
    # where it's unavailable (e.g. eager mode without a broker).
    try:
        revoked = bool(self.is_revoked())
    except Exception:
        revoked = False
    if revoked:
        logger.info("convert_document %s revoked before start", task_id)
        return None

    def is_cancelled() -> bool:
        try:
            return bool(self.is_revoked())
        except Exception:
            return False

    doc = controller.convert_document(source, is_cancelled)
    if doc is None:
        return None
    return doc.model_dump()


@shared_task(bind=True, name="sqwakvox.backend.tasks.build_financial_data_store")  # type: ignore[untyped-decorator]
def build_financial_data_store(
    self: Any,  # noqa: ARG001
    document_dump: dict[str, Any],
) -> dict[str, str]:
    """Return a JSON-serialisable mapping ``{label: raw_str}``.

    ``FinancialValue`` is a float subclass; we serialise it back to its raw
    string form so the presenter can hand it to the guardrail layer verbatim.
    """
    controller = _get_controller()
    doc = StructuredDocument.model_validate(document_dump)
    data_store = controller.build_financial_data_store(doc)
    # Serialise to {label: raw_str} for broker-safe transport.
    return {
        label: str(value.raw_str if hasattr(value, "raw_str") else value)
        for label, value in data_store.items()
    }


@shared_task(bind=True, name="sqwakvox.backend.tasks.cross_validate")  # type: ignore[untyped-decorator]
def cross_validate(
    self: Any,  # noqa: ARG001
    document_dump: dict[str, Any],
) -> list[tuple[str, float, float, bool]]:
    """Run financial column cross-validation on the parsed document tables."""
    controller = _get_controller()
    doc = StructuredDocument.model_validate(document_dump)
    return controller.cross_validate(doc)


@shared_task(bind=True, name="sqwakvox.backend.tasks.execute_agent")  # type: ignore[untyped-decorator]
def execute_agent(
    self: Any,
    model_id: str,
    api_key: str,
    user_query: str,
    doc_context: str,
    active_document_name: str,
    data_store: dict[str, str],
    mcp_servers: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Execute the LLM agent for a user chat query.

    ``mcp_servers`` is a broker-safe ``model_dump()`` list of any_agent MCP
    configs; we rehydrate them back into ``MCPParams`` here before handing
    them to the controller.
    """
    controller = _get_controller()

    try:
        revoked = self.is_revoked()
    except Exception:
        revoked = False
    if revoked:
        result = AgentResult(
            success=False,
            error_message="Agent task was cancelled before it started executing.",
        )
        return result.__dict__

    # Rehydrate FinancialValue objects — the presenter sends back strings.
    # Parse the numeric portion so guardrail cross-checks have real values.
    from sqwakvox.guardrails import parse_financial_value

    hydrated_store = {
        label: (parse_financial_value(raw) or FinancialValue(0.0, raw_str=raw))
        for label, raw in data_store.items()
    }

    hydrated_mcp = MCP_SERVERS_ADAPTER.validate_python(mcp_servers) if mcp_servers else None

    result = controller.execute_agent(
        model_id=model_id,
        api_key=api_key,
        user_query=user_query,
        doc_context=doc_context,
        active_document_name=active_document_name,
        data_store=hydrated_store,
        mcp_servers=hydrated_mcp,
    )
    return result.__dict__
