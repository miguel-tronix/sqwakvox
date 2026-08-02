"""Backend layer for Sqwakvox.

Contains the Celery application definition and its task modules.  These tasks
wrap the heavy, blocking work that previously ran inside the Textual TUI
process — document conversion (Docling), financial data-store construction,
cross-validation, and agent (any-agent) execution.

Run the worker with: ``python -m sqwakvox.run_worker``
"""
