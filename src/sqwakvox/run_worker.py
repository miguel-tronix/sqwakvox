"""Entry-point script for running the Sqwakvox Celery worker.

Usage::

    python -m sqwakvox.run_worker            # default: info logging
    python -m sqwakvox.run_worker --loglevel=debug

The worker imports the :mod:`sqwakvox.backend.tasks` module (auto-registered
via ``celery_app.conf.include``) which in turn imports
:class:`~sqwakvox.controller.AppController` for real document parsing,
cross-validation, and agent execution.

The worker is designed to run **separately** from the Textual TUI.  Start the
worker in one terminal, then launch the TUI in another::

    Terminal 1:  python -m sqwakvox.run_worker
    Terminal 2:  python -m sqwakvox
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqwakvox.backend.celery_app import celery_app

logger = logging.getLogger("sqwakvox")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Sqwakvox Celery worker")
    parser.add_argument(
        "--loglevel",
        default="INFO",
        help="Celery worker log level (default: INFO)",
    )
    args = parser.parse_args()

    # Configure logging for the worker process.
    logging.basicConfig(
        level=getattr(logging, args.loglevel.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Ensure telemetry is set up in the worker (for tracing spans).
    from sqwakvox.telemetry import setup_telemetry

    setup_telemetry()

    argv = [
        "worker",
        "-B",  # beat scheduler — needed for periodic tasks (future cron-style jobs)
        "-l",
        args.loglevel,
        "-Q",
        "sqwakvox",  # only consume the sqwakvox default queue
    ]
    try:
        celery_app.worker_main(argv)
    except KeyboardInterrupt:
        logger.info("Worker shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
