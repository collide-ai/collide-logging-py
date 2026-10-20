"""Operation context: ``log_context`` blocks bind fields and record them on escaping exceptions."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, ParamSpec, TypeVar

import structlog

from collide_logging._exception_fields import record_context
from collide_logging._fields import (
    FIELD_NAME_RE,
    LOG_CALL_KEYWORDS,
    MAX_FIELDS,
    RESERVED_CONTEXT_FIELDS,
    FieldValue,
    StrictModeViolation,
    clean_value,
    is_reserved_name,
    is_strict,
)
from collide_logging._unit import current_unit

P = ParamSpec("P")
R = TypeVar("R")

_Entry = tuple["LogContext", Mapping[str, contextvars.Token[Any]]]

# One entry per active block in the current context, tagged with the instance that entered it.
# It is a contextvar so a block entered in a thread or task is exited in the same one.
_active_entries: contextvars.ContextVar[tuple[_Entry, ...]] = contextvars.ContextVar(
    "collide_log_context_entries", default=()
)


def active_field_names() -> frozenset[str]:
    """Names bound by the `log_context` blocks active in this context.

    Unlike `get_contextvars()` this leaves out fields an adapter binds around the whole unit, such
    as a django-q task's `task` and `task_id`.
    """
    return frozenset(name for owner, _ in _active_entries.get() for name in owner.field_names)


def reset_log_context() -> None:
    """Forget every block still open in this context, for a worker starting its next task.

    A block left open by an earlier task, such as one in a suspended generator, then cannot
    unwind the blocks of the task that runs after it.
    """
    _active_entries.set(())


def _validated(fields: Mapping[str, object]) -> dict[str, FieldValue]:
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"log_context takes at most {MAX_FIELDS} fields, got {len(fields)}")
    cleaned: dict[str, FieldValue] = {}
    for name, value in fields.items():
        if name in LOG_CALL_KEYWORDS:
            raise TypeError(f"log_context cannot set the log-call keyword {name!r}")
        if is_reserved_name(name):
            raise TypeError(f"log_context cannot set the library-owned field {name!r}")
        if not FIELD_NAME_RE.fullmatch(name):
            raise ValueError(f"log_context field name {name!r} must match [a-z][a-z0-9_]*")
        try:
            cleaned[name] = clean_value(value)
        except ValueError as error:
            if is_strict():
                raise StrictModeViolation(f"log_context field {name!r}: {error}") from None
    return cleaned


def _check_reserved_conflicts(fields: Mapping[str, FieldValue]) -> None:
    bound = structlog.contextvars.get_contextvars()
    for name in RESERVED_CONTEXT_FIELDS.intersection(fields):
        if name in bound and str(bound[name]) != str(fields[name]):
            raise StrictModeViolation(f"{name} conflicts with the bound context value")


class LogContext:
    """Context manager, async context manager and decorator that bind fields to log lines.

    Fields are bound on entry and reset on exit. When the block exits with an exception, they are
    also recorded on it (innermost block wins), so a failure line logged outside the block still
    carries them. One instance may be entered concurrently from different threads or tasks.

    An exit unwinds by identity: it resets its own block and every block entered after it in the
    same context that is still open, such as one inside a generator kept past the block. Those
    blocks' later exits do nothing, so exits out of order, or in another context, never leave
    fields bound or reset another block's values. An instance entered twice in the same context
    is told apart only by order: its first exit releases the later entry.

    An exception instance that is re-raised through several blocks keeps the fields of every
    block it passed through; for a name set by more than one, the innermost value wins.
    """

    def __init__(self, fields: dict[str, FieldValue]) -> None:
        self._fields = fields

    @property
    def field_names(self) -> frozenset[str]:
        return frozenset(self._fields)

    def __enter__(self) -> None:
        if is_strict():
            _check_reserved_conflicts(self._fields)
        tokens = structlog.contextvars.bind_contextvars(**self._fields)
        _active_entries.set((*_active_entries.get(), (self, tokens)))

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._release()
        if exc is not None:
            unit = current_unit()
            record_context(exc, self._fields, unit=unit.stamp if unit is not None else None)

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, traceback)

    def __call__(self, function: Callable[P, R]) -> Callable[P, R]:
        if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
            raise TypeError(
                "log_context cannot decorate a generator function; use it as a block inside"
            )
        fields = self._fields

        if inspect.iscoroutinefunction(function):
            async_function: Callable[P, Awaitable[object]] = function

            @functools.wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
                async with LogContext(fields):
                    return await async_function(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with LogContext(fields):
                return function(*args, **kwargs)

        return wrapper

    def _release(self) -> None:
        active = _active_entries.get()
        owned = [index for index, (owner, _) in enumerate(active) if owner is self]
        if not owned:
            return
        start = owned[-1]
        _active_entries.set(active[:start])
        for _, tokens in reversed(active[start:]):
            for token in tokens.values():
                # A token from another Context, or one already used, must not raise over the
                # exception in flight.
                with contextlib.suppress(ValueError, RuntimeError):
                    token.var.reset(token)


def log_context(**fields: FieldValue | uuid.UUID) -> LogContext:
    """Bind ``fields`` to every log line in the block, and to exceptions that escape it.

    Raises ``TypeError`` for a library-owned name (or one starting with ``exception``) and
    ``TypeError`` also for ``exc_info``, ``stack_info`` and ``stacklevel``, and
    ``ValueError`` for a malformed name or more than 20 fields. A value that is not a str, int
    within 64 bits, finite float, bool, None or UUID is dropped, or raises
    ``StrictModeViolation`` under ``configure(strict=True)``. Strings are capped at 128
    characters. Under strict mode, entering a block that sets a reserved context field to a
    value other than the bound one also raises ``StrictModeViolation``.

    Not for generator functions: decorating one raises ``TypeError``.
    """
    return LogContext(_validated(fields))
