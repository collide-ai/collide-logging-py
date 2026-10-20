"""Units of work: one request or task, and the single outcome line it writes.

An adapter opens a unit, ``annotate`` adds fields to it, ``record_exception`` records its failure,
and the adapter writes its outcome line with ``emit_outcome`` before closing it.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import LiteralString

from collide_logging._fields import (
    APPLICATION_CONTEXT_FIELDS,
    LOG_CALL_KEYWORDS,
    FieldValue,
    StrictModeViolation,
    clean_fields,
    clean_value,
    is_strict,
    is_valid_name,
)
from collide_logging.contract import Logger

MAX_ANNOTATIONS = 64

# Set on an exception recorded as a unit's failure, which its outcome line carries. It outlives
# the unit, so a server's own record of the exception, written after the unit closed, is still
# recognised.
_OUTCOME_ATTRIBUTE = "_collide_on_outcome_line"
# The server loggers that write their own record of a failure the outcome line already carries.
_SERVER_LOGGERS = frozenset({"django.request", "uvicorn.error"})


@dataclass(eq=False)
class Unit:
    """One request or task. Code running in a copy of its context shares this same object."""

    start: float = field(default_factory=time.monotonic)
    annotations: dict[str, FieldValue] = field(default_factory=dict)
    exc: BaseException | None = None
    # Identity stamped on exceptions, so a captured context from another unit is told apart.
    # A plain object keeps an exception that outlives the unit from holding it alive.
    stamp: object = field(default_factory=object)
    closed: bool = False


_current_unit: contextvars.ContextVar[Unit | None] = contextvars.ContextVar(
    "collide_unit", default=None
)


def open_unit() -> tuple[Unit, contextvars.Token[Unit | None]]:
    """Start a unit of work in the current context; pass both to ``close_unit``."""
    unit = Unit()
    return unit, _current_unit.set(unit)


def close_unit(unit: Unit, token: contextvars.Token[Unit | None]) -> None:
    """End ``unit`` once its outcome line is written, and drop its annotations and exception.

    Copies of the unit's context that outlive it, such as an ``asyncio.create_task`` child or a
    ``run_in_context`` thread, then see no current unit. A token from another context, or one
    already used, is ignored.
    """
    unit.closed = True
    unit.annotations = {}
    unit.exc = None
    with contextlib.suppress(ValueError, RuntimeError):
        _current_unit.reset(token)


def current_unit() -> Unit | None:
    """The open unit of this context, if any."""
    unit = _current_unit.get()
    return None if unit is None or unit.closed else unit


def _mark_on_outcome_line(exc: BaseException, marked: bool) -> None:
    # An exception that refuses the attribute is simply not recognised by the filter.
    with contextlib.suppress(Exception):
        setattr(exc, _OUTCOME_ATTRIBUTE, marked)


def _set_exception(unit: Unit, exc: BaseException | None) -> None:
    if unit.exc is not None and unit.exc is not exc:
        _mark_on_outcome_line(unit.exc, False)
    unit.exc = exc
    if exc is not None:
        _mark_on_outcome_line(exc, True)


def record_exception(exc: BaseException | None) -> None:
    """Record ``exc`` as the current unit's failure; nothing happens outside an open unit."""
    unit = current_unit()
    if unit is not None and exc is not None:
        _set_exception(unit, exc)


def discard_exception(unit: Unit) -> None:
    """Forget the exception recorded on ``unit``, for a unit that turned out to succeed."""
    _set_exception(unit, None)


def _check_annotations(fields: Mapping[str, object]) -> None:
    for name, value in fields.items():
        if name in LOG_CALL_KEYWORDS or name in APPLICATION_CONTEXT_FIELDS:
            continue
        if not is_valid_name(name):
            raise StrictModeViolation(f"annotate field name {name!r} is malformed or library-owned")
        try:
            clean_value(value)
        except ValueError as error:
            raise StrictModeViolation(f"annotate field {name!r}: {error}") from None


def annotate(**fields: FieldValue | uuid.UUID) -> None:
    """Add ``fields`` to the current request's or task's outcome line.

    A later value for a name wins. A unit keeps at most 64 names; later new names are dropped.
    Names and values follow the field rule: a library-owned or malformed name, or a value that is
    not a str, an int within 64 bits, a finite float, a bool, None or a UUID, is dropped. So are
    ``exc_info``, ``stack_info``, ``stacklevel`` and the identity fields (``account_id``,
    ``user_id``, ``agent_run_id``), which belong in bound context. Under ``configure(strict=True)``
    a malformed or library-owned name, or an unsupported value, raises ``StrictModeViolation``
    instead; the identity fields and log-call keywords are still dropped. Outside an open request
    or task this does nothing, in every mode.
    """
    unit = current_unit()
    if unit is None:
        return
    if is_strict():
        _check_annotations(fields)
    annotations = unit.annotations
    for name, value in clean_fields(fields, limit=MAX_ANNOTATIONS).items():
        if name in LOG_CALL_KEYWORDS or name in APPLICATION_CONTEXT_FIELDS:
            continue
        if name in annotations or len(annotations) < MAX_ANNOTATIONS:
            annotations[name] = value


class RecordedExceptionFilter(logging.Filter):
    """Drop a server's own record of an exception that an outcome line carries.

    Applies to ``django.request`` (written before the middleware sees the 500 response) and
    ``uvicorn.error`` (written after the middleware re-raises, once the unit has closed); every
    other record passes. It sits on the library's handler, not on the logger, so other handlers
    of those loggers (such as ``mail_admins``) still receive the record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.name not in _SERVER_LOGGERS or not record.exc_info:
                return True
            return getattr(record.exc_info[1], _OUTCOME_ATTRIBUTE, False) is not True
        except Exception:
            return True


def outcome_fields(unit: Unit, *, failed: bool) -> dict[str, object]:
    """The fields every outcome line carries beyond its own: annotations, outcome, error details.

    When the unit recorded an exception, ``exc_info`` carries it so the exception renderer adds
    its type, attributes, declared fields and captured context.
    """
    fields: dict[str, object] = dict(unit.annotations)
    exc = unit.exc
    fields["outcome"] = "error" if failed or exc is not None else "ok"
    if exc is not None:
        fields["error_type"] = type(exc).__name__
        fields["exc_info"] = exc
    return fields


def snapshot(unit: Unit) -> Unit:
    """A detached copy of ``unit`` for an outcome line written after the unit has closed."""
    return dataclasses.replace(unit, annotations=dict(unit.annotations))


def emit_outcome(
    logger: Logger,
    event: LiteralString,
    unit: Unit,
    *,
    failed: bool,
    fields: Mapping[str, object],
    warning: bool = False,
) -> None:
    """Write ``unit``'s outcome line.

    ``failed`` is the adapter's own failure signal, such as a status of 500 or more. The unit's
    annotations come first and ``fields`` over them, so an annotation never replaces the line's
    own fields. The line logs at ``error`` for an error outcome, otherwise at ``warning`` when
    ``warning`` is set, otherwise at ``info``.
    """
    line = {
        **outcome_fields(unit, failed=failed),
        **fields,
        "duration_ms": int((time.monotonic() - unit.start) * 1000),
    }
    if line["outcome"] == "error":
        logger.error(event, **line)
    elif warning:
        logger.warning(event, **line)
    else:
        logger.info(event, **line)
