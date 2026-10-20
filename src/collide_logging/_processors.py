"""structlog processors implementing the collide/v1 spec.

Internal module. The public surface is the factories `_redact_secrets` and
`_add_service_info`, which `configure()` (issue #3) wires into the structlog
processor chain.
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import traceback
import uuid
from collections.abc import Callable, Mapping, MutableMapping
from datetime import UTC, datetime
from types import ModuleType, TracebackType
from typing import Any

import structlog
import structlog.contextvars

from collide_logging._context import active_field_names
from collide_logging._exception_fields import exception_fields, operation_fields
from collide_logging._fields import (
    LIBRARY_OWNED_NAMES,
    LOG_CALL_KEYWORDS,
    RESERVED_CONTEXT_FIELDS,
    StrictModeViolation,
)

_Processor = Callable[
    [Any, str, MutableMapping[str, Any]],
    MutableMapping[str, Any],
]

_REDACTED = "***REDACTED***"
_REDACTION_ERROR = "***REDACTION_ERROR***"
_SUFFIX_RULES = ("_token", "_api_token", "_signing_secret")
_MAX_DEPTH = 32
_MAX_VISITS_PER_EVENT = 10_000
_MAX_EXCEPTION_CHAIN = 50
EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")

DEFAULT_REDACT_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "secret",
        "secret_key",
        "prompt",
        "user_prompt",
        "user_query",
        "query",
        "question",
        "completion",
        "ai_response",
        "messages",
        "text_delta",
        "tool_input",
        "tool_output",
        "request_payload",
        "response_body",
        "email",
        "user_email",
        "recipient",
        "request",
    }
)


def _json_default(value: object) -> object:
    """Serialize values ``json.dumps`` cannot.

    UUIDs become canonical strings; objects defining ``__structlog__`` use it (as
    structlog's own default does); anything else falls back to ``repr``.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        structlog_hook = getattr(value, "__structlog__", None)
        if callable(structlog_hook):
            return structlog_hook()
        return repr(value)
    except Exception:
        return _REDACTION_ERROR


def _redact_secrets(redact_keys: frozenset[str]) -> _Processor:
    """Return a processor that redacts sensitive values from event dicts.

    Redaction triggers on either:
      - case-insensitive exact match against `redact_keys`, or
      - case-insensitive suffix match against the spec-mandated patterns
        `*_token`, `*_api_token`, `*_signing_secret`.

    The processor returns a new dict and copies every container it walks, so
    objects held by the caller are never mutated. Inline ``Bearer <token>`` in
    any string is scrubbed. A value that cannot be walked, or that nests deeper
    than the depth bound (which also ends self-referencing structures), becomes
    the redaction-error sentinel; so does a container that contains itself, and
    every value past a per-event visit budget (bounds branching structures).
    """
    keys = frozenset(k.lower() for k in redact_keys)

    def is_secret(key: object) -> bool:
        lk = str(key).lower()
        return lk in keys or lk.endswith(_SUFFIX_RULES)

    def json_key(key: object) -> object:
        """JSON objects only take scalar keys; stringify the rest so the line still renders."""
        if key is None or isinstance(key, str | int | float | bool):
            return key
        if isinstance(key, bytes):
            return key.decode("utf-8", errors="replace")
        return str(key)

    def processor(
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        remaining = _MAX_VISITS_PER_EVENT
        path: set[int] = set()

        def redact(value: object, depth: int) -> object:
            nonlocal remaining
            remaining -= 1
            if depth >= _MAX_DEPTH or remaining < 0:
                return _REDACTION_ERROR
            if isinstance(value, str):
                return _BEARER_RE.sub(f"Bearer {_REDACTED}", value)
            if not isinstance(value, Mapping | list | tuple | set | frozenset):
                return value
            if id(value) in path:
                return _REDACTION_ERROR
            path.add(id(value))
            try:
                if isinstance(value, Mapping):
                    items = [(json_key(k), v) for k, v in value.items()]
                    return {
                        k: _REDACTED if is_secret(k) else redact(v, depth + 1) for k, v in items
                    }
                if isinstance(value, list):
                    return [redact(v, depth + 1) for v in value]
                if isinstance(value, tuple):
                    return tuple(redact(v, depth + 1) for v in value)
                if isinstance(value, set):
                    return {redact(v, depth + 1) for v in value}
                return frozenset(redact(v, depth + 1) for v in value)
            except Exception:
                return _REDACTION_ERROR
            finally:
                path.discard(id(value))

        return {
            key: _REDACTED if is_secret(key) else redact(value, 0)
            for key, value in event_dict.items()
        }

    return processor


def _add_service_info(service: str) -> _Processor:
    """Return a processor that tags every event with the service slug.

    Uses setdefault so an explicit `service=...` on a log call is preserved.
    """

    def processor(
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        event_dict.setdefault("service", service)
        return event_dict

    return processor


def _add_trace_ids() -> _Processor:
    """Return a processor that tags events with the active OpenTelemetry span's ids.

    The API is imported once, when the processor is built; without it installed the
    processor is a pass-through. Explicit ids are preserved; a ``None`` id counts as
    unset, so the keys are filled from the span or absent, never null.
    """
    trace: ModuleType | None
    try:
        from opentelemetry import trace
    except ImportError:
        trace = None

    def processor(
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        for key in ("trace_id", "span_id"):
            if key in event_dict and event_dict[key] is None:
                del event_dict[key]
        if trace is None:
            return event_dict
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
            event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
        return event_dict

    return processor


def _normalise_event(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Move a prose or non-string event into `message` under the `log.message` event name."""
    event = event_dict.get("event")
    if isinstance(event, str) and EVENT_NAME_RE.fullmatch(event):
        return event_dict
    event_dict["message"] = str(event)
    event_dict["event"] = "log.message"
    return event_dict


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Walk ``__cause__`` / ``__context__`` from the raised exception, bounded and cycle-safe."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < _MAX_EXCEPTION_CHAIN:
        seen.add(id(current))
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _resolve_exc_info(
    exc_info: object,
) -> tuple[type[BaseException], BaseException, TracebackType | None] | None:
    if isinstance(exc_info, BaseException):
        return type(exc_info), exc_info, exc_info.__traceback__
    if (
        isinstance(exc_info, tuple)
        and len(exc_info) == 3
        and isinstance(exc_info[1], BaseException)
    ):
        return exc_info
    if exc_info:
        resolved = sys.exc_info()
        if resolved[1] is not None:
            return resolved
    return None


def _render_exception(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace ``exc_info`` with type names, traceback frames and safe attributes.

    Exception messages and notes can carry arbitrary data, so they are never rendered. Frames omit
    source lines for the same reason: a line like ``raise E("...")`` repeats the message literal.
    """
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict
    resolved = _resolve_exc_info(exc_info)
    if resolved is None:
        return event_dict
    exc = resolved[1]
    chain = _exception_chain(exc)
    parts: list[str] = []
    for item in reversed(chain):
        tb = resolved[2] if item is exc else item.__traceback__
        frames = traceback.StackSummary.extract(traceback.walk_tb(tb), lookup_lines=False)
        lines = [
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n' for frame in frames
        ]
        lines.append(f"{_type_name(item)}\n")
        if isinstance(item, BaseExceptionGroup):
            lines.extend(f"  {_type_name(member)}\n" for member in item.exceptions)
        parts.append("".join(lines))
    event_dict["exception_type"] = _type_name(exc)
    event_dict["exception"] = "\n".join(parts).rstrip("\n")
    for name, value in exception_fields(chain).items():
        event_dict.setdefault(name, value)
    return event_dict


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _logging_error_event(
    error_type: str, logger: object, service: object, timestamp: object = None
) -> dict[str, Any]:
    """Build the fixed-shape line that replaces an event whose processing failed.

    It carries only the exception class name, never the failed event's fields or message.
    """
    event: dict[str, Any] = {
        "event": "logging.error",
        "error_type": error_type,
        "level": "error",
        "timestamp": timestamp or _timestamp(),
    }
    if logger is not None:
        event["logger"] = logger
    if service is not None:
        event["service"] = service
    return event


_STDLIB_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, None, None, None).__dict__
) | frozenset({"message"})

_STRICT_FORBIDDEN_FIELDS = frozenset({"message", "task"})


def _call_site_fields(event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    record: logging.LogRecord | None = event_dict.get("_record")
    if record is None:
        return event_dict
    return {k: v for k, v in record.__dict__.items() if k not in _STDLIB_LOGRECORD_ATTRS}


_NON_OPERATION_NAMES = LIBRARY_OWNED_NAMES | RESERVED_CONTEXT_FIELDS | LOG_CALL_KEYWORDS


def _exception_of_call(
    method_name: str, event_dict: MutableMapping[str, Any], record: logging.LogRecord | None
) -> tuple[bool, BaseException | None]:
    """Whether the call carries an exception, and the exception when it can be resolved."""
    if record is not None:
        exc_info: object = record.exc_info
        carries = bool(exc_info)
    else:
        exc_info = event_dict.get("exc_info", method_name == "exception")
        carries = method_name == "exception" or bool(exc_info)
    resolved = _resolve_exc_info(exc_info) if carries else None
    return carries, resolved[1] if resolved is not None else None


def _check_operation_context(
    method_name: str, event_dict: MutableMapping[str, Any], fields: Mapping[str, Any]
) -> None:
    """Require a call that carries an exception to name the operation that failed."""
    record: logging.LogRecord | None = event_dict.get("_record")
    carries, exc = _exception_of_call(method_name, event_dict, record)
    if not carries:
        return
    names = set(fields) | active_field_names()
    if exc is not None:
        names.update(operation_fields(_exception_chain(exc)))
    if not names - _NON_OPERATION_NAMES:
        raise StrictModeViolation("exception logged without a field describing the operation")


def _strict_stdlib_args_filter(
    first_party: tuple[str, ...], exempt: tuple[str, ...]
) -> Callable[[logging.LogRecord], bool]:
    """Build a handler filter that rejects `%`-args on first-party stdlib records.

    It runs in `Handler.handle`, before `emit`, because a mismatched format string would
    otherwise fail inside `format()` and be swallowed into a `logging.error` line.
    """

    def check(record: logging.LogRecord) -> bool:
        if record.name.startswith(exempt) or not record.name.startswith(first_party):
            return True
        if record.args:
            raise StrictModeViolation(f"{record.name}: positional arguments are not allowed")
        return True

    return check


def _enforce_strict(first_party: tuple[str, ...], exempt: tuple[str, ...]) -> _Processor:
    """Raise `StrictModeViolation` for a call that breaks the call-site rules.

    Runs ahead of every other processor so the fields it sees are the caller's own, and is
    deliberately not wrapped by `_guarded`. Stdlib positional arguments are rejected earlier, by
    `_strict_stdlib_args_filter`.
    """

    def processor(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        record: logging.LogRecord | None = event_dict.get("_record")
        name = record.name if record is not None else getattr(logger, "name", "") or ""
        if name.startswith(exempt):
            return event_dict
        if record is not None and not name.startswith(first_party):
            return event_dict

        event = event_dict.get("event")
        if not (isinstance(event, str) and EVENT_NAME_RE.fullmatch(event)):
            raise StrictModeViolation(f"event name {event!r} does not match the event-name rule")
        if record is None and "positional_args" in event_dict:
            raise StrictModeViolation(f"{name}: positional arguments are not allowed")

        fields = _call_site_fields(event_dict)
        forbidden = _STRICT_FORBIDDEN_FIELDS.intersection(fields)
        if forbidden:
            raise StrictModeViolation(f"reserved call-site field(s) {sorted(forbidden)}")
        bound = structlog.contextvars.get_contextvars()
        for key in RESERVED_CONTEXT_FIELDS.intersection(fields):
            if key in bound and str(bound[key]) != str(fields[key]):
                raise StrictModeViolation(f"{key} conflicts with the bound context value")
        _check_operation_context(method_name, event_dict, fields)
        return event_dict

    return processor


def _positional_args_count(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace the values of stray positional arguments with their count.

    The values are caller data that no field name marks for redaction, so they are never rendered.
    """
    args = event_dict.pop("positional_args", None)
    if args is not None:
        event_dict["positional_args_count"] = len(args)
    return event_dict


_FORMATTER_META_KEYS = ("_record", "_from_structlog")


def _guarded(processor: _Processor, service: str | None = None) -> _Processor:
    """Wrap a processor so a failure becomes a ``logging.error`` event instead of an exception.

    ``service`` is the fallback slug for a failure before the event carries one. The formatter's
    meta keys are carried over because ``ProcessorFormatter`` deletes them after the chain runs.
    """

    @functools.wraps(processor)
    def wrapper(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        try:
            return processor(logger, method_name, event_dict)
        except Exception as exc:
            fallback = _logging_error_event(
                type(exc).__qualname__,
                event_dict.get("logger") or event_dict.get("logger_name"),
                event_dict.get("service") or service,
                event_dict.get("timestamp"),
            )
            fallback.update((k, event_dict[k]) for k in _FORMATTER_META_KEYS if k in event_dict)
            return fallback

    return wrapper
