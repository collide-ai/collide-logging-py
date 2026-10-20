"""collide-logging: Python implementation of the collide/v1 logging spec.

Public API:
  - configure(service, ...): set up the structlog processor chain.
  - get_logger(name): obtain a Logger.
  - Logger: the typed logging contract.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any, LiteralString, Self

import structlog

from collide_logging._processors import _add_service_info, _redact_secrets
from collide_logging.contract import Logger
from collide_logging.workers import bind_worker_run_id, with_worker_run_id

__version__ = "0.5.1"
__all__ = [
    "CollideLogger",
    "Logger",
    "__version__",
    "bind_worker_run_id",
    "configure",
    "get_logger",
    "with_worker_run_id",
]


class CollideLogger(structlog.BoundLoggerBase):
    """Bound logger used as the ``wrapper_class`` for collide/v1 logging.

    Built on ``BoundLoggerBase`` rather than ``structlog.stdlib.BoundLogger`` because
    the base class takes ``event`` as a keyword-capable parameter, which cannot be
    reconciled with the positional-only ``event`` of the ``Logger`` contract.
    """

    def bind(self, **fields: object) -> Self:
        return super().bind(**fields)

    def _emit(
        self, method_name: str, event: str, args: tuple[object, ...], fields: dict[str, object]
    ) -> None:
        # Positional args are accepted at runtime so a stray `log.info("x %s", y)` never raises
        # into the caller; they travel as `positional_args` like structlog's stdlib BoundLogger.
        if args:
            fields["positional_args"] = args
        self._proxy_to_logger(method_name, event, **fields)

    def debug(self, event: LiteralString, /, *args: object, **fields: object) -> None:
        self._emit("debug", event, args, fields)

    def info(self, event: LiteralString, /, *args: object, **fields: object) -> None:
        self._emit("info", event, args, fields)

    def warning(self, event: LiteralString, /, *args: object, **fields: object) -> None:
        self._emit("warning", event, args, fields)

    def error(self, event: LiteralString, /, *args: object, **fields: object) -> None:
        self._emit("error", event, args, fields)

    def critical(self, event: LiteralString, /, *args: object, **fields: object) -> None:
        self._emit("critical", event, args, fields)

    def exception(self, event: LiteralString, /, *args: object, **fields: object) -> None:
        fields.setdefault("exc_info", True)
        self._emit("exception", event, args, fields)


_STDLIB_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, None, None, None).__dict__
) | frozenset({"message"})


def _merge_record_extra(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    # No-op for structlog-originated records: _from_structlog is absent in that path.
    if event_dict.get("_from_structlog", True):
        return event_dict
    record: logging.LogRecord = event_dict["_record"]
    if record.exc_info and record.exc_info[0] is not None:
        event_dict.setdefault("exc_info", record.exc_info)
    for key, value in record.__dict__.items():
        if key not in _STDLIB_LOGRECORD_ATTRS and not key.startswith("_"):
            event_dict.setdefault(key, value)
    return event_dict


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


def get_logger(name: str) -> Logger:
    """Return a structured logger for the given component.

    Use module-qualified names (e.g. ``get_logger(__name__)``) so the
    ``logger`` field in emitted events matches the code path.
    """
    logger: CollideLogger = structlog.get_logger(name)
    return logger


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

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.stdlib.add_logger_name,
        _rename_logger_field,
        _add_service_info(service),
        _merge_record_extra,
        _redact_secrets(redact_keys),
        structlog.processors.format_exc_info,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    root.setLevel(level_int)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=CollideLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
