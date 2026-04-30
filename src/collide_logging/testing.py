"""Test helpers for asserting collide/v1 conformance.

Use in downstream services::

    from structlog.testing import capture_logs
    from collide_logging.testing import assert_collide_v1

    with capture_logs() as records:
        my_service.do_thing()

    for record in records:
        assert_collide_v1(record)
"""

from __future__ import annotations

import re
import warnings
from datetime import datetime
from typing import Any

_REQUIRED_FIELDS = ("timestamp", "level", "service", "logger", "event")
_VALID_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
_REDACTED_SENTINEL = "***REDACTED***"

_REDACT_EXACT_KEYS = frozenset(
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
_REDACT_SUFFIXES = ("_token", "_api_token", "_signing_secret")


def _is_redact_target(key: str) -> bool:
    lk = key.lower()
    return lk in _REDACT_EXACT_KEYS or lk.endswith(_REDACT_SUFFIXES)


def assert_collide_v1(record: dict[str, Any]) -> None:
    """Raise AssertionError if ``record`` does not conform to collide/v1.

    Hyphenated or camelCase event names emit a warning rather than failing,
    since the spec is opinionated about dot-notation but the helper is
    slightly forgiving for transitional codebases.
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
        raise AssertionError(
            f"timestamp must be a string, got {type(timestamp).__name__}"
        )
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
    if not _EVENT_NAME_RE.fullmatch(event):
        warnings.warn(
            f"event name {event!r} does not match dot-notation convention "
            f"{_EVENT_NAME_RE.pattern!r}",
            stacklevel=2,
        )

    for key, value in record.items():
        if _is_redact_target(key) and value != _REDACTED_SENTINEL:
            raise AssertionError(
                f"field {key!r} has unredacted value {value!r}; "
                "did the _redact_secrets processor run?"
            )
