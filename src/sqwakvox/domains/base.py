"""Document-domain abstraction — the per-assistant profile for Sqwakvox.

A :class:`DocumentDomain` parameterises every layer that currently assumes a
financial document assistant: agent prompts, ingest, post-parse processing,
guardrails, rendering, and skills storage.  The view, presenter, Celery task
layer, and workers stay generic and ask "what does the active document's
domain say?".

Adding a new assistant = registering a new ``DocumentDomain`` in
:mod:`sqwakvox.domains` — no core changes required.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jinja2 import Template

from sqwakvox import guardrails as gr
from sqwakvox.models import StructuredDocument

if TYPE_CHECKING:
    pass

#: Loose typing for the controller to keep this module importable from the
#: view (which must not pull in the worker-side docling dependency graph).
Controller = Any


@dataclass
class IngestPlan:
    """How a source should be fed to Docling (built inside the worker).

    ``inputs`` is a list of concrete conversion inputs — file paths, URLs, or
    Docling ``DocumentStream`` objects — plus metadata describing how the
    domain should reassemble the result (EPUB chapters, crawled pages, ...).
    """

    kind: str
    inputs: list[Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InputGuardrailResult:
    """Verdict of a domain's input guardrail stages for a user prompt."""

    safe: bool = True
    blocked_reason: str = ""
    #: Transformed (e.g. redacted) prompt text; ``None`` means unchanged.
    text: str | None = None


@dataclass
class OutputGuardrailResult:
    """Verdict of a domain's output guardrail stages for an agent response."""

    #: Transformed (e.g. redacted) response text; ``None`` means unchanged.
    text: str | None = None
    warnings: list[str] = field(default_factory=list)


def standard_validate_input(prompt: str) -> InputGuardrailResult:
    """Mozilla any-guardrail prompt check, then local PII redaction.

    The shared input-guardrail stage used by domains that do not need
    customised input validation (currently all of them).  Output validation
    stays domain-specific (see each domain's ``_validate_output``).
    """
    if not gr.AnyGuardrailValidator.validate_prompt(prompt):
        return InputGuardrailResult(
            safe=False,
            blocked_reason="Mozilla any-guardrail prompt safety violation",
        )
    redacted = gr.PIIRedactor.redact_text(prompt)
    return InputGuardrailResult(safe=True, text=redacted)


@dataclass
class GuardrailPipeline:
    """The input/output guardrail hooks a domain runs around the agent call."""

    validate_input: Callable[[str], InputGuardrailResult] | None = None
    validate_output: Callable[[str, dict[str, Any]], OutputGuardrailResult] | None = None


def _default_render(doc: StructuredDocument) -> str:
    """Fallback renderer: filename plus the raw markdown body."""
    return f"[bold]{doc.file_name}[/bold]\n\n{doc.raw_markdown or ''}"


def default_build_context(doc: StructuredDocument) -> str:
    """Default agent context: the document's full raw markdown."""
    return doc.raw_markdown or ""


@dataclass
class DocumentDomain:
    """Declarative profile for one document assistant (financial, swe, ...)."""

    domain_id: str
    display_name: str
    description: str
    #: Domain system-prompt / user-prompt templates (receive ``{{context}}``).
    system_instructions: Template | None = None
    user_prompt_template: Template | None = None
    #: Prepare a source for Docling; returns the IngestPlan.  Runs inside the
    #: shared docling worker where the Docling models already live.
    pre_convert: Callable[[str, dict[str, Any]], IngestPlan] | None = None
    #: Convert a planned source into a StructuredDocument (runs in the worker).
    convert: (
        Callable[
            [Controller, str, IngestPlan, Callable[[], bool], tuple[int, int] | None],
            StructuredDocument | None,
        ]
        | None
    ) = None
    #: Post-parse processing (runs on the per-document agent worker).
    #: Returns a JSON-serialisable payload, e.g. ``{"data_store": {...}}``.
    postprocess: Callable[[StructuredDocument, str], dict[str, Any]] | None = None
    #: Input/output guardrail pipeline for agent queries.
    guardrail_pipeline: Callable[[], GuardrailPipeline] | None = None
    #: TUI renderer for parsed documents.
    render: Callable[[StructuredDocument], str] | None = None
    #: Agent context builder; None uses :func:`default_build_context`
    #: (full raw markdown).  The SWE domain overrides this with a
    #: TOC + first-chunks context for large documents.
    build_context: Callable[[StructuredDocument], str] | None = None
    #: Whether this domain can author/persist skill files (see
    #: :mod:`sqwakvox.domains.swe.skills`).
    skills_enabled: bool = False
    #: Skills storage root; ``None`` uses ``./skills/<domain_id>`` (cwd).
    skills_dir: str | None = None

    def context_for(self, doc: StructuredDocument) -> str:
        """The context string handed to the agent for this document."""
        if self.build_context is not None:
            return self.build_context(doc)
        return default_build_context(doc)


@dataclass
class LoadedDocument:
    """View-side wrapper: a parsed document plus the domain that parsed it.

    Lets a single TUI session hold tabs from *different* domains at once.

    When the document was loaded as PDF page batches (see
    :meth:`~sqwakvox.app.SqwakvoxApp._prefetch_batch`), the paging fields
    track which slices have been fetched and rendered so the TUI can show a
    "Load more" control and prefetch the next slice in the background.
    """

    domain_id: str
    structured: StructuredDocument
    source: str
    #: Pages in each fetched slice (the first slice is ``structured`` itself).
    batch_size: int = 0
    #: Total pages of the source PDF, or ``None`` when unknown (then the end is
    #: detected when a fetched slice comes back empty).
    total_pages: int | None = None
    #: Highest 1-based page currently rendered (``batch_size`` after slice 0).
    rendered_pages: int = 0
    #: First 1-based page currently rendered.  Sequential loads keep this at 1;
    #: a page seek moves the window without appending earlier pages.
    window_start: int = 1
    #: True once an empty slice proved the end of a document with no page count.
    paging_complete: bool = False
    #: Bumped on a seek so in-flight prefetches from the old window stop caching.
    paging_generation: int = 0
    #: Next slice index to load (0 is the initial parse result).
    next_batch: int = 0
    #: Prefetched slices awaiting display, keyed by slice index.
    batch_cache: dict[int, StructuredDocument] = field(default_factory=dict)
    #: In-flight prefetch task handles, keyed by slice index.
    pending: dict[int, Any] = field(default_factory=dict)

    @property
    def file_name(self) -> str:
        return self.structured.file_name

    @property
    def is_paged(self) -> bool:
        """True when this document was loaded page-slice by page-slice."""
        return self.batch_size > 0
