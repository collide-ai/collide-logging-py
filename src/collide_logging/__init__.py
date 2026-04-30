"""collide-logging: Python implementation of the collide/v1 logging spec.

Public API:
  - get_logger(name): obtain a structlog BoundLogger.

The configure() entrypoint that wires the processor chain into structlog is
forthcoming (#3). Calling get_logger() before configure() returns a logger
backed by structlog's default settings.
"""

from __future__ import annotations

from typing import cast

import structlog

__version__ = "0.0.0"

__all__ = ["__version__", "get_logger"]


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger for the given component.

    Use module-qualified names (e.g. ``get_logger(__name__)``) so the `logger`
    field in emitted events matches the code path.
    """
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
