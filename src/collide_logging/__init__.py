"""collide-logging: Python implementation of the collide/v1 logging spec.

Public API:
  - configure(service, ...): set up the structlog processor chain.
  - get_logger(name): obtain a Logger.
  - Logger: the typed logging contract.
  - log_context(**fields): bind operation fields for a block and record them on exceptions.
  - annotate(**fields): add fields to the current request's or task's outcome line.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any, LiteralString, Self

import structlog

from collide_logging._context import log_context
from collide_logging._fields import RESERVED_CONTEXT_FIELDS, StrictModeViolation, set_strict
from collide_logging._handler import CollideStreamHandler
from collide_logging._processors import (
    _STDLIB_LOGRECORD_ATTRS,
    DEFAULT_REDACT_KEYS,
    _add_service_info,
    _add_trace_ids,
    _enforce_strict,
    _guarded,
    _json_default,
    _normalise_event,
    _positional_args_count,
    _Processor,
    _redact_secrets,
    _render_exception,
    _strict_stdlib_args_filter,
)
from collide_logging._unit import RecordedExceptionFilter, annotate
from collide_logging.contract import Logger
from collide_logging.workers import bind_worker_run_id, run_in_context, with_worker_run_id

__version__ = "0.5.1"
__all__ = [
    "RESERVED_CONTEXT_FIELDS",
    "CollideLogger",
    "Logger",
    "StrictModeViolation",
    "__version__",
    "annotate",
    "bind_worker_run_id",
    "configure",
    "get_logger",
    "log_context",
    "run_in_context",
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
        # into the caller; they travel as `positional_args`, which the chain reduces to a count.
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


def _apply_third_party_defaults() -> None:
    floor = max(logging.WARNING, logging.getLogger().level)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(floor)
    # uvicorn's CLI applies its own dictConfig before the app is imported, so
    # configure() must take those loggers over rather than leave them on plain text.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(logging.NOTSET)
    access = logging.getLogger("uvicorn.access")
    # The NullHandler keeps the logger silent if a later dictConfig re-enables it:
    # without a handler, logging's last-resort handler would print to stderr.
    access.handlers = [logging.NullHandler()]
    access.propagate = False
    access.disabled = True


def configure(
    service: str,
    *,
    level: str = "info",
    json: bool | None = None,
    extra_redact_keys: Iterable[str] = (),
    strict: bool = False,
    first_party: Iterable[str] = (),
    strict_exempt: Iterable[str] = (),
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
        strict: If ``True``, raise ``StrictModeViolation`` from a log call that
            uses a prose event name, positional arguments, a ``message`` or
            ``task`` field, or a reserved context field whose value differs from
            the bound one, and from ``log_context`` given an unsupported value or a
            reserved context field that differs from the bound one, and from
            ``annotate`` given a malformed or library-owned name or an unsupported
            value. Meant for test settings.
        first_party: Logger-name prefixes whose stdlib records strict mode
            checks. Calls through ``get_logger`` are always checked.
        strict_exempt: Logger-name prefixes strict mode skips.
    """
    if json is None:
        json = not sys.stdout.isatty()

    level_int = logging.getLevelNamesMapping()[level.upper()]
    redact_keys = DEFAULT_REDACT_KEYS | frozenset(extra_redact_keys)

    renderer: Any = (
        structlog.processors.JSONRenderer(default=_json_default)
        if json
        else structlog.dev.ConsoleRenderer()
    )

    if isinstance(first_party, str) or isinstance(strict_exempt, str):
        offender = "first_party" if isinstance(first_party, str) else "strict_exempt"
        raise TypeError(f"{offender} must be an iterable of prefixes, not a str")
    first_party_prefixes = tuple(first_party)
    exempt_prefixes = tuple(strict_exempt)
    set_strict(strict)

    strict_processors: list[_Processor] = (
        [_enforce_strict(first_party_prefixes, exempt_prefixes)] if strict else []
    )
    shared_processors: list[Any] = [
        *strict_processors,
        _positional_args_count,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.stdlib.add_logger_name,
        _guarded(_rename_logger_field, service),
        _guarded(_add_service_info(service), service),
        _guarded(_merge_record_extra, service),
        _guarded(_add_trace_ids(), service),
        _guarded(_render_exception, service),
        _guarded(_redact_secrets(redact_keys), service),
        _guarded(_normalise_event, service),
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
    handler = CollideStreamHandler(sys.stdout, service)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_TAG, True)
    handler.addFilter(RecordedExceptionFilter())
    if strict:
        handler.addFilter(_strict_stdlib_args_filter(first_party_prefixes, exempt_prefixes))
    root.addHandler(handler)
    root.setLevel(level_int)
    _apply_third_party_defaults()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=CollideLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
