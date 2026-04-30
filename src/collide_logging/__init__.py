"""collide-logging: Python implementation of the collide/v1 logging spec.

Public API:
  - configure(service, ...): set up the structlog processor chain.
  - get_logger(name): obtain a structlog BoundLogger.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any, cast

import structlog

from collide_logging._processors import _add_service_info, _redact_secrets
from collide_logging.workers import bind_worker_run_id, with_worker_run_id

__version__ = "0.0.0"
__all__ = [
    "__version__",
    "bind_worker_run_id",
    "configure",
    "get_logger",
    "with_worker_run_id",
]


_DEFAULT_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "secret",
        "secret_key",
    }
)

_HANDLER_TAG = "_collide_logging_handler"


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger for the given component.

    Use module-qualified names (e.g. ``get_logger(__name__)``) so the
    ``logger`` field in emitted events matches the code path.
    """
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


def _rename_logger_field(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    # structlog's add_logger_name emits "logger_name"; the spec field is "logger".
    if "logger_name" in event_dict:
        event_dict["logger"] = event_dict.pop("logger_name")
    return event_dict


def configure(
    service: str,
    *,
    level: str = "info",
    json: bool | None = None,
    extra_redact_keys: Iterable[str] = (),
) -> None:
    """Configure structlog and stdlib logging to emit collide/v1 output.

    Args:
        service: Slug of the emitting service. Becomes the ``service`` field
            on every log line; should match ``slug`` in the service's
            ``collide.yaml``.
        level: Minimum log level. One of ``debug``, ``info``, ``warning``,
            ``error``, ``critical`` (case-insensitive).
        json: If ``True``, emit one JSON object per line. If ``False``, use
            structlog's developer-friendly console renderer. Default
            (``None``) selects JSON when stdout is not a TTY.
        extra_redact_keys: Field names (case-insensitive) to redact in
            addition to the spec-mandated default set.
    """
    if json is None:
        json = not sys.stdout.isatty()

    level_int = logging.getLevelNamesMapping()[level.upper()]
    redact_keys = _DEFAULT_REDACT_KEYS | frozenset(extra_redact_keys)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer()
    )

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    root.setLevel(level_int)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.stdlib.add_logger_name,
            _rename_logger_field,
            _add_service_info(service),
            _redact_secrets(redact_keys),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
