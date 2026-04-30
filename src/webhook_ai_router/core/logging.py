"""structlog configuration.

Two output modes:

* ``APP_ENV=prod`` — line-delimited JSON suitable for log aggregators.
* ``APP_ENV=dev``  — colorised, human-friendly console output.

``structlog.contextvars.merge_contextvars`` is always in the chain so any
value bound via the request-id middleware (or any other code) shows up on
every log record automatically.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor

from webhook_ai_router.config import AppEnv, LogLevel


def configure_logging(app_env: AppEnv, log_level: LogLevel) -> None:
    """Initialise structlog and route stdlib ``logging`` through it."""

    level = logging.getLevelNamesMapping()[log_level.value]

    # NOTE: ``structlog.stdlib.add_logger_name`` is intentionally absent. We
    # use ``PrintLoggerFactory`` (no stdlib logger), which has no ``.name``.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if app_env is AppEnv.PROD
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout)
