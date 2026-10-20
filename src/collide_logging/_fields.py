"""Rules for caller-supplied failure-line fields."""

from __future__ import annotations

import itertools
import math
import re
import uuid
from collections.abc import Mapping

FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_STRING_LENGTH = 128
MAX_FIELDS = 20
_MIN_INT = -(2**63)
_MAX_INT = 2**63 - 1

RESERVED_CONTEXT_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "worker_run_id",
        "agent_run_id",
        "client_request_id",
        "trace_id",
        "span_id",
        "account_id",
        "user_id",
    }
)
APPLICATION_CONTEXT_FIELDS: frozenset[str] = frozenset({"account_id", "user_id", "agent_run_id"})

LIBRARY_OWNED_NAMES: frozenset[str] = (RESERVED_CONTEXT_FIELDS - APPLICATION_CONTEXT_FIELDS) | {
    "event",
    "level",
    "timestamp",
    "logger",
    "service",
    "message",
    "outcome",
    "duration_ms",
    "error_type",
    "error_code",
    "task",
    "task_id",
}

_EXCEPTION_PREFIX = "exception"

FieldValue = str | int | float | bool | None

# Keywords a log call interprets rather than renders; they never name an operation or a field.
LOG_CALL_KEYWORDS: frozenset[str] = frozenset({"exc_info", "stack_info", "stacklevel"})


class StrictModeViolation(AssertionError):  # noqa: N818
    """A call broke the collide/v1 rules while ``strict=True``."""


_strict = False


def set_strict(enabled: bool) -> None:
    """Record whether ``configure(strict=True)`` is active, for checks outside the processors."""
    global _strict
    _strict = enabled


def is_strict() -> bool:
    return _strict


def cap_string(value: str) -> str:
    """Cap to ``MAX_STRING_LENGTH`` as a plain ``str``; a subclass's ``__getitem__`` never runs."""
    return str.__getitem__(value, slice(0, MAX_STRING_LENGTH))


def is_valid_name(name: object) -> bool:
    """Whether ``name`` passes the key regex and is neither library-owned nor ``exception*``."""
    return (
        isinstance(name, str)
        and FIELD_NAME_RE.fullmatch(name) is not None
        and not is_reserved_name(name)
    )


def is_reserved_name(name: str) -> bool:
    return name in LIBRARY_OWNED_NAMES or name.startswith(_EXCEPTION_PREFIX)


def clean_value(value: object) -> FieldValue:
    """Normalise one value under the field rule, or raise ``ValueError`` if it is not allowed."""
    if isinstance(value, uuid.UUID):
        value = str(value)
    if value is not None and not isinstance(value, str | int | float | bool):
        raise ValueError(f"unsupported field value type: {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("field value must be a finite float")
    # Python refuses to stringify an int past about 4300 digits, which would fail the whole line.
    if isinstance(value, int) and not _MIN_INT <= value <= _MAX_INT:
        raise ValueError("field value int must fit in 64 bits")
    return cap_string(value) if isinstance(value, str) else value


def clean_fields(raw: Mapping[str, object], *, limit: int) -> dict[str, FieldValue]:
    """Keep the entries that satisfy the field rule, in order, up to ``limit`` of them.

    Invalid names, library-owned names, names starting with ``exception`` and values that are not
    scalars are dropped. A UUID becomes its canonical string; strings are capped. At most
    ``4 * limit`` entries are examined, so a huge mapping costs no more than a small one.
    """
    cleaned: dict[str, FieldValue] = {}
    for name, value in itertools.islice(raw.items(), 4 * limit):
        if len(cleaned) >= limit:
            break
        if not isinstance(name, str) or not is_valid_name(name):
            continue
        try:
            cleaned[name] = clean_value(value)
        except ValueError:
            continue
    return cleaned
