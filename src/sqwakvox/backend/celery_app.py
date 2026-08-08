"""Celery application definition for the Sqwakvox backend.

The broker / result-backend default to Redis running on localhost.  Override
with the standard Celery environment variables if you need something else::

    SQWAKVOX_CELERY_BROKER=pyamqp://guest@localhost//
    SQWAKVOX_CELERY_BACKEND=rpc://

Tasks are auto-discovered from the :mod:`sqwakvox.backend.tasks` module.
"""

from __future__ import annotations

import os

from celery import Celery

broker_url = os.environ.get("SQWAKVOX_CELERY_BROKER", "redis://localhost:6379/0")
result_backend = os.environ.get("SQWAKVOX_CELERY_BACKEND", "redis://localhost:6379/1")

celery_app: Celery = Celery(
    "sqwakvox",
    broker=broker_url,
    backend=result_backend,
    include=["sqwakvox.backend.tasks"],
)

# --- Minimal, sane defaults for a local single-worker deployment ---
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Single-queue deployment: the worker consumes only the ``sqwakvox``
    # queue (``run_worker.py -Q sqwakvox``), so every task must default there.
    task_default_queue="sqwakvox",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Don't ACK until the task body finishes; lets the presenter poll for
    # state transitions (PENDING -> STARTED -> SUCCESS/FAILURE).
    task_acks_late=True,
    # Generous limits for slow hardware: document OCR/parsing (docling) on an
    # old laptop routinely exceeds the old 9-10 min budget.  The soft limit
    # (30 min) raises SoftTimeLimitExceeded inside the task so it can fail
    # gracefully; the hard limit (31 min) is the SIGKILL backstop.  Override
    # via env if a particular machine needs even more headroom.
    task_time_limit=int(os.environ.get("SQWAKVOX_CELERY_TASK_TIME_LIMIT", "1860")),
    task_soft_time_limit=int(os.environ.get("SQWAKVOX_CELERY_TASK_SOFT_TIME_LIMIT", "1800")),
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=20,
)


# Eager mode is convenient for unit tests / offline execution where a real
# broker may not be running.  Enable with SQWAKVOX_CELERY_EAGER=1.
if os.environ.get("SQWAKVOX_CELERY_EAGER", "").lower() in ("1", "true", "yes"):
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
