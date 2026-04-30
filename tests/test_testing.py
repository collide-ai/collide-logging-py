from __future__ import annotations

import warnings
from typing import Any

import pytest

from collide_logging.testing import assert_collide_v1


def _good_record() -> dict[str, Any]:
    return {
        "timestamp": "2026-04-30T12:00:00.123456Z",
        "level": "info",
        "service": "my-svc",
        "logger": "test.module",
        "event": "test.event",
    }


def test_passes_on_valid_record() -> None:
    assert_collide_v1(_good_record())


@pytest.mark.parametrize(
    "missing", ["timestamp", "level", "service", "logger", "event"]
)
def test_fails_on_missing_required_field(missing: str) -> None:
    record = _good_record()
    del record[missing]
    with pytest.raises(AssertionError, match=f"missing required field: {missing}"):
        assert_collide_v1(record)


def test_fails_on_invalid_level() -> None:
    record = _good_record()
    record["level"] = "trace"
    with pytest.raises(AssertionError, match="level 'trace'"):
        assert_collide_v1(record)


def test_fails_on_unparseable_timestamp() -> None:
    record = _good_record()
    record["timestamp"] = "not-a-date"
    with pytest.raises(AssertionError, match="ISO 8601"):
        assert_collide_v1(record)


def test_fails_on_timestamp_without_timezone() -> None:
    record = _good_record()
    record["timestamp"] = "2026-04-30T12:00:00"
    with pytest.raises(AssertionError, match="timezone"):
        assert_collide_v1(record)


def test_fails_on_empty_event() -> None:
    record = _good_record()
    record["event"] = ""
    with pytest.raises(AssertionError, match="event must be a non-empty string"):
        assert_collide_v1(record)


def test_fails_on_empty_service() -> None:
    record = _good_record()
    record["service"] = ""
    with pytest.raises(AssertionError, match="service must be a non-empty string"):
        assert_collide_v1(record)


def test_fails_on_unredacted_secret() -> None:
    record = _good_record()
    record["api_key"] = "leaked-value"
    with pytest.raises(AssertionError, match="unredacted"):
        assert_collide_v1(record)


def test_fails_on_unredacted_token_suffix() -> None:
    record = _good_record()
    record["github_token"] = "leaked-pat"
    with pytest.raises(AssertionError, match="unredacted"):
        assert_collide_v1(record)


def test_redacted_value_passes() -> None:
    record = _good_record()
    record["api_key"] = "***REDACTED***"
    record["github_token"] = "***REDACTED***"
    assert_collide_v1(record)


def test_passes_on_camel_case_event_with_warning() -> None:
    record = _good_record()
    record["event"] = "myEvent"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_collide_v1(record)
    assert any("dot-notation" in str(w.message) for w in caught)


def test_passes_on_hyphenated_event_with_warning() -> None:
    record = _good_record()
    record["event"] = "my-event"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_collide_v1(record)
    assert any("dot-notation" in str(w.message) for w in caught)


def test_dot_notation_event_does_not_warn() -> None:
    record = _good_record()
    record["event"] = "collector.github.repo_processed"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert_collide_v1(record)
    assert not any("dot-notation" in str(w.message) for w in caught)
