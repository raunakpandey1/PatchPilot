"""Structured logging setup.

A log line is not a sentence, it is an event with fields::

    log.info("repo_cloned", repo="pallets/flask", duration_s=4.2)

That can be filtered, counted and averaged. "Cloned pallets/flask in 4.2s"
cannot. By Phase 7 a single agent run emits dozens of events across ten
components, and the only way to make sense of it is to filter by ``run_id``.
"""

import logging
import sys

import structlog

from patchpilot.config import LogLevel


def setup_logging(level: LogLevel = "INFO", *, json_output: bool = False) -> None:
    """Configure structlog. Call once, at process startup.

    Args:
        level: Minimum level to emit.
        json_output: One event per line as JSON — for shipping to a log
            collector. Off by default because coloured console output is far
            easier to read while developing.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            # Makes bound context (run_id, repo, ...) available to every event.
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to a component name."""
    # structlog.get_logger is untyped upstream, so mypy sees Any here.
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(component=name)
    return logger
