"""Logging configuration for a run (ADR-015).

Configured at entry by ``main()``, never at import. Only the
``stocks_on_the_move`` logger hierarchy is configured: it runs at DEBUG, the
console handler shows what ``LOG_LEVEL`` asks for, and the per-run file
handler that ``RunArtifacts.attach_log`` adds takes everything. The root
logger is left alone, so third-party libraries still surface warnings
through Python's last-resort handler without flooding the run's file.

Level policy: WARNING for anything skipped or swallowed that a person should
look at; INFO for decisions and totals; DEBUG for per-call and per-symbol
detail; never, at any level, a token, a secret or a Kite response body.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

PACKAGE_LOGGER = "stocks_on_the_move"
CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(module)s:%(lineno)d %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONSOLE_MARK = "_sotm_console"


def package_logger() -> logging.Logger:
    return logging.getLogger(PACKAGE_LOGGER)


def console_handler() -> logging.Handler | None:
    """The console handler ``configure_logging`` installed, if any."""
    for handler in package_logger().handlers:
        if getattr(handler, _CONSOLE_MARK, False):
            return handler
    return None


def configure_logging(level: LogLevel | str = "INFO") -> logging.Handler:
    """Route the package's log records to the console at ``level``; the logger itself runs at DEBUG.

    Idempotent: a second call changes the console level and installs nothing new,
    so ``main()`` can log a configuration error before the configured level is known.
    """
    logger = package_logger()
    logger.setLevel(logging.DEBUG)
    handler = console_handler()
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
        setattr(handler, _CONSOLE_MARK, True)
        logger.addHandler(handler)
    handler.setLevel(logging.getLevelNamesMapping()[str(level).upper()])
    return handler


def file_handler(path: str) -> logging.Handler:
    """A DEBUG handler in the per-run format, for ``RunArtifacts.attach_log``."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
    return handler
