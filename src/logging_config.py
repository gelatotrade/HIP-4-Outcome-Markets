"""Structured JSON logging.

Default to human-readable when stdout is a TTY, JSON otherwise. Used by
the dashboard, fetcher and renderer; the Rust executor does its own
tracing-subscriber setup.
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup(level: str = "INFO") -> None:
    is_tty = sys.stdout.isatty()
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
    ]
    if is_tty:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level.upper(), format="%(message)s")


def get(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
