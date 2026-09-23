"""Celery tasks for the Sqwakvox backend.

Each task wraps a synchronous, CPU/IO-bound operation from
:class:`~sqwakvox.controller.AppController` (or a domain hook).  Keeping them
as plain functions with explicit arguments (no model-bound objects that can't
be JSON-serialised through the broker) ensures Celery can always pickle the
call graph.

The presenter talks to these tasks via ``AsyncResult`` polling.  Every task
returns a JSON-serialisable result (Pydantic ``model_dump`` for documents).

Tasks are domain-agnostic: ``domain_id`` (see :mod:`sqwakvox.domains`)
selects the ingest plan, post-parse payload, and guardrail pipeline.  Unknown
ids fall back to the financial domain, so old queued messages keep working.
"""

from __future__ import annotations

import logging
from typing import Any

from any_agent.config import MCPParams
from celery import shared_task
from pydantic import TypeAdapter

from sqwakvox.backend.celery_app import celery_app  # noqa: F401 — registers tasks
from sqwakvox.controller import AgentResult, AppController
from sqwakvox.domains import get_domain
from sqwakvox.domains.base import IngestPlan
from sqwakvox.guardrails import FinancialValue
from sqwakvox.models import ModelProvider, StructuredDocument

logger = logging.getLogger(__name__)

MCP_SERVERS_ADAPTER: TypeAdapter[list[MCPParams]] = TypeAdapter(list[MCPParams])


def _get_controller() -> AppController:
    """Create a fresh controller per process.

    The controller itself is cheap; the heavyweight ``DocumentConverter`` it
    wraps is built lazily on the first ``convert_document`` call, so only the
    shared Docling ingest worker ever instantiates it (see
    :mod:`sqwakvox.worker_manager`).  Per-document agent workers never
    construct Docling models at all.
    """
    return AppController()


def _is_revoked(self: Any) -> bool:
    """Return whether the running task was revoked (backend may be absent)."""
    try:
        return bool(self.is_revoked())
    except Exception:
        return False


@shared_task(bind=True, name="sqwakvox.backend.tasks.convert_document")  # type: ignore[untyped-decorator]
def convert_document(
    self: Any,
    source: str,
    domain_id: str = "financial",
    options: dict[str, Any] | None = None,
    page_range: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    """Parse *source* (path/URL) into a :class:`StructuredDocument`.

    The domain's ingest plan decides how Docling is fed (EPUB chapters,
    single PDF/URL, ...) and how the result is reassembled.  ``page_range``
    (1-based ``[start, end]``) restricts the conversion to a slice of a PDF so
    large documents load incrementally instead of timing out.  Returns the
    document as a plain dict (``model_dump``), or ``None`` if the worker was
    revoked.
    """
    controller = _get_controller()
    task_id = self.request.id

    # A revocation sentinel: if the task was revoked before we even started
    # running, Celery will still call the body.  Check explicitly.
    if _is_revoked(self):
        logger.info("convert_document %s revoked before start", task_id)
        return None

    def is_cancelled() -> bool:
        return _is_revoked(self)

    domain = get_domain(domain_id)
    plan: IngestPlan = (
        domain.pre_convert(source, options or {})
        if domain.pre_convert
        else IngestPlan(kind="single", inputs=[source])
    )
    convert_fn = domain.convert
    doc = (
        controller.convert_document(
            source, is_cancelled, domain_id=domain_id, page_range=page_range
        )
        if convert_fn is None
        else convert_fn(controller, source, plan, is_cancelled, page_range)
    )
    if doc is None:
        return None
    doc.metadata["domain_id"] = domain_id
    return doc.model_dump()


@shared_task(bind=True, name="sqwakvox.backend.tasks.domain_postprocess")  # type: ignore[untyped-decorator]
def domain_postprocess(
    self: Any,  # noqa: ARG001
    domain_id: str,
    document_dump: dict[str, Any],
    source: str = "",
) -> dict[str, Any]:
    """Run a domain's post-parse processing on a parsed document.

    Returns a domain-defined, JSON-serialisable payload — financial:
    ``{"data_store": {...}}``; swe: TOC + code-block index + injection flags.
    """
    doc = StructuredDocument.model_validate(document_dump)
    domain = get_domain(domain_id)
    if domain.postprocess is None:
        return {}
    return domain.postprocess(doc, source)


@shared_task(bind=True, name="sqwakvox.backend.tasks.build_financial_data_store")  # type: ignore[untyped-decorator]
def build_financial_data_store(
    self: Any,  # noqa: ARG001
    document_dump: dict[str, Any],
) -> dict[str, str]:
    """Return the financial data store ``{label: raw_str}``.

    Backward-compatible wrapper over the financial domain's post-parse step
    (used by the TUI's older data-store flow and existing tests).
    """
    from typing import cast

    from sqwakvox.domains.financial import _postprocess

    doc = StructuredDocument.model_validate(document_dump)
    payload = _postprocess(doc)
    return cast(dict[str, str], payload.get("data_store", {}))


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
    api_key: str | None,
    user_query: str,
    doc_context: str,
    active_document_name: str,
    data_store: dict[str, str],
    mcp_servers: list[dict[str, Any]] | None,
    thread_id: str | None = None,
    domain_id: str = "financial",
) -> dict[str, Any] | AgentResult:
    """Execute the LLM agent for a user chat query.

    ``api_key`` is optional: the gateway sends ``None`` and the worker
    resolves the key from its own environment via
    :meth:`ModelProvider.get_env_var` (keys never cross the broker).
    Callers that already hold a key (presenter) may still pass it explicitly.

    ``mcp_servers`` is a broker-safe ``model_dump()`` list of any_agent MCP
    configs; we rehydrate them back into ``MCPParams`` here before handing
    them to the controller.  ``domain_id`` selects the agent prompts and the
    guardrail pipeline.
    """
    controller = _get_controller()

    if _is_revoked(self):
        result = AgentResult(
            success=False,
            error_message="Agent task was cancelled before it started executing.",
        )
        return result.__dict__

    # Resolve API key worker-side when the gateway did not send one.
    if not api_key:
        model_id, api_key = ModelProvider.resolve_key(model_id)
        if not api_key:
            env_var = ModelProvider.get_env_var(model_id)
            result = AgentResult(
                success=False,
                error_message=(
                    f"API key for model '{model_id}' ({env_var}) is not set "
                    "in the worker environment."
                ),
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
        thread_id=thread_id,
        domain_id=domain_id,
    )
    return result.__dict__ if hasattr(result, "__dict__") else result
