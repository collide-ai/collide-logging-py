"""Test helpers for asserting collide/v1 conformance.

Use in downstream services, after ``collide_logging.configure()``::

    from collide_logging.testing import captured_logs

    with captured_logs() as captured:
        my_service.do_thing()

    captured.assert_conforms()
    captured.assert_absent("super-secret-value")

and, for code that calls ``annotate()`` outside a request or task::

    from collide_logging.testing import unit_of_work

    with unit_of_work() as work:
        import_rows(rows)

    assert work.annotations == {"rows_imported": 3}
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from collide_logging import _HANDLER_TAG
from collide_logging._fields import FieldValue
from collide_logging._processors import _SUFFIX_RULES, DEFAULT_REDACT_KEYS, EVENT_NAME_RE
from collide_logging._unit import Unit, close_unit, open_unit, record_exception

_REQUIRED_FIELDS = ("timestamp", "level", "service", "logger", "event")
_VALID_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_REDACTED_SENTINEL = "***REDACTED***"


def _is_redact_target(key: str) -> bool:
    lowered = key.lower()
    return lowered in DEFAULT_REDACT_KEYS or lowered.endswith(_SUFFIX_RULES)


def assert_collide_v1(record: dict[str, Any]) -> None:
    """Raise AssertionError if ``record`` does not conform to collide/v1.

    An event name that is not dot-notation (prose, hyphens, camelCase) fails.
    """
    for field in _REQUIRED_FIELDS:
        if field not in record:
            raise AssertionError(f"missing required field: {field}")

    level = record["level"]
    if not isinstance(level, str):
        raise AssertionError(f"level must be a string, got {type(level).__name__}")
    if level not in _VALID_LEVELS:
        raise AssertionError(f"level {level!r} is not one of {sorted(_VALID_LEVELS)}")

    timestamp = record["timestamp"]
    if not isinstance(timestamp, str):
        raise AssertionError(f"timestamp must be a string, got {type(timestamp).__name__}")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise AssertionError(
            f"timestamp {timestamp!r} is not parseable as ISO 8601: {exc}"
        ) from None
    if parsed.tzinfo is None:
        raise AssertionError(f"timestamp {timestamp!r} has no timezone info")

    service = record["service"]
    if not isinstance(service, str) or not service:
        raise AssertionError(f"service must be a non-empty string, got {service!r}")

    logger = record["logger"]
    if not isinstance(logger, str) or not logger:
        raise AssertionError(f"logger must be a non-empty string, got {logger!r}")

    event = record["event"]
    if not isinstance(event, str) or not event:
        raise AssertionError(f"event must be a non-empty string, got {event!r}")
    if not EVENT_NAME_RE.fullmatch(event):
        raise AssertionError(
            f"event name {event!r} does not match the event-name rule {EVENT_NAME_RE.pattern!r}"
        )

    for key, value in record.items():
        if _is_redact_target(key) and value != _REDACTED_SENTINEL:
            raise AssertionError(
                f"field {key!r} has unredacted value {value!r}; "
                "did the _redact_secrets processor run?"
            )


@dataclass
class CapturedLogs:
    """Output captured by :func:`captured_logs`: stdout, the handler stream and stderr.

    ``text`` is read from the buffers on every access, so assertions inside the block see
    the output so far.
    """

    _buffers: tuple[io.StringIO, ...] = ()

    @property
    def text(self) -> str:
        return "".join(buffer.getvalue() for buffer in self._buffers)

    @property
    def _lines(self) -> list[str]:
        return [line for line in self.text.splitlines() if line.strip()]

    @property
    def records(self) -> list[dict[str, Any]]:
        """Every captured line that parses as a JSON object."""
        parsed: list[dict[str, Any]] = []
        for line in self._lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                parsed.append(value)
        return parsed

    def assert_conforms(self) -> None:
        """Fail unless every non-empty captured line is a JSON event that passes the check."""
        for line in self._lines:
            try:
                record = json.loads(line)
            except ValueError:
                raise AssertionError(f"captured line is not JSON: {line!r}") from None
            if not isinstance(record, dict):
                raise AssertionError(f"captured line is not a JSON object: {line!r}")
            assert_collide_v1(record)

    def assert_absent(self, sentinel: str) -> None:
        """Fail if ``sentinel`` appears anywhere in the captured text, ``message`` included."""
        if sentinel in self.text:
            raise AssertionError(f"{sentinel!r} found in captured log output")


def _find_collide_handler() -> logging.StreamHandler:  # type: ignore[type-arg]
    for handler in logging.getLogger().handlers:
        if getattr(handler, _HANDLER_TAG, False) and isinstance(handler, logging.StreamHandler):
            return handler
    raise RuntimeError("no collide-logging handler on the root logger; call configure() first")


@contextlib.contextmanager
def captured_logs() -> Iterator[CapturedLogs]:
    """Capture ``sys.stdout``, the configured handler's stream and ``sys.stderr`` for the block.

    The handler holds the stdout it was configured with, so it is repointed explicitly; a
    ``configure()`` call inside the block builds its handler on the redirected stdout.
    """
    handler = _find_collide_handler()
    handler_buffer = io.StringIO()
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    original_stream = handler.setStream(handler_buffer)
    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            yield CapturedLogs((handler_buffer, stdout_buffer, stderr_buffer))
    finally:
        handler.setStream(original_stream)


class UnitOfWork:
    """The unit opened by :func:`unit_of_work`: what was annotated, and the recorded exception.

    Both are read live inside the block and keep their final values after it.
    """

    def __init__(self, unit: Unit) -> None:
        self._unit = unit
        self._final: tuple[dict[str, FieldValue], BaseException | None] | None = None

    @property
    def annotations(self) -> dict[str, FieldValue]:
        """A copy of the fields added with ``annotate()``."""
        if self._final is not None:
            return dict(self._final[0])
        return dict(self._unit.annotations)

    @property
    def exception(self) -> BaseException | None:
        """The exception recorded as the unit's failure, if any."""
        return self._final[1] if self._final is not None else self._unit.exc

    def _close(self) -> None:
        self._final = (dict(self._unit.annotations), self._unit.exc)


@contextlib.contextmanager
def unit_of_work() -> Iterator[UnitOfWork]:
    """Run the block as one unit of work, as a request or task would, and close it on exit.

    For testing code that calls ``annotate()``. An exception that escapes the block is recorded
    as the unit's failure, as the adapters do, and propagates. No outcome line is written.
    """
    unit, token = open_unit()
    work = UnitOfWork(unit)
    try:
        yield work
    except Exception as exc:
        record_exception(exc)
        raise
    finally:
        work._close()
        close_unit(unit, token)
