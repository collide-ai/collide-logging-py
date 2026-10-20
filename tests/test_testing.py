from __future__ import annotations

import json
import logging
import sys
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

import collide_logging
from collide_logging import _HANDLER_TAG
from collide_logging._processors import DEFAULT_REDACT_KEYS
from collide_logging.testing import assert_collide_v1, captured_logs


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


@pytest.mark.parametrize("missing", ["timestamp", "level", "service", "logger", "event"])
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


@pytest.mark.parametrize("event", ["myEvent", "my-event", "Hello world"])
def test_assert_collide_v1_fails_on_prose_event(event: str) -> None:
    record = _good_record()
    record["event"] = event
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(AssertionError, match="event name"):
            assert_collide_v1(record)


def test_dot_notation_event_passes() -> None:
    record = _good_record()
    record["event"] = "collector.github.repo_processed"
    assert_collide_v1(record)


@pytest.mark.parametrize("key", sorted(DEFAULT_REDACT_KEYS))
def test_testing_keys_follow_library_defaults(key: str) -> None:
    record = _good_record()
    record[key] = "leaked-value"
    with pytest.raises(AssertionError, match="unredacted"):
        assert_collide_v1(record)


@pytest.fixture
def configured() -> Iterator[None]:
    collide_logging.configure(service="my-svc")
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    structlog.reset_defaults()


def test_captured_logs_requires_configure() -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = [h for h in saved if not getattr(h, _HANDLER_TAG, False)]
    try:
        with pytest.raises(RuntimeError, match="configure"), captured_logs():
            pass
    finally:
        root.handlers = saved


@pytest.mark.usefixtures("configured")
def test_captured_logs_parses_conforming_records_and_restores_streams() -> None:
    handler = next(h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False))
    original_stream = handler.stream
    original_stderr = sys.stderr
    with captured_logs() as captured:
        collide_logging.get_logger("t").info("test.event", answer=42)
    assert handler.stream is original_stream
    assert sys.stderr is original_stderr
    assert [r["event"] for r in captured.records] == ["test.event"]
    assert captured.records[0]["answer"] == 42
    captured.assert_conforms()


@pytest.mark.usefixtures("configured")
def test_captured_logs_reads_stdout() -> None:
    with captured_logs() as captured:
        print("plain")
    with pytest.raises(AssertionError, match="captured line is not JSON: 'plain'"):
        captured.assert_conforms()


@pytest.mark.usefixtures("configured")
def test_captured_logs_reads_stderr() -> None:
    with captured_logs() as captured:
        print("oops", file=sys.stderr)
    with pytest.raises(AssertionError, match="captured line is not JSON: 'oops'"):
        captured.assert_conforms()


@pytest.mark.usefixtures("configured")
def test_captured_logs_live_inside_block() -> None:
    with captured_logs() as captured:
        collide_logging.get_logger("t").info("test.event", note="SENTINEL-live")
        with pytest.raises(AssertionError, match="SENTINEL-live"):
            captured.assert_absent("SENTINEL-live")


@pytest.mark.usefixtures("configured")
def test_captured_logs_reconfigure_inside_block_still_captured() -> None:
    with captured_logs() as captured:
        collide_logging.configure(service="other-svc", json=True)
        collide_logging.get_logger("t").info("test.event", note="SENTINEL-reconf")
    assert [r["event"] for r in captured.records] == ["test.event"]
    assert captured.records[0]["service"] == "other-svc"


@pytest.mark.usefixtures("configured")
def test_captured_logs_restores_on_exception() -> None:
    handler = next(h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False))
    original_stream = handler.stream
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    with pytest.raises(ValueError, match="boom"), captured_logs():
        raise ValueError("boom")
    assert handler.stream is original_stream
    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no string form")


@pytest.mark.usefixtures("configured")
def test_logging_error_fallback_line_conforms() -> None:
    handler = next(h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False))
    record = logging.getLogger("other.lib").makeRecord(
        "other.lib", logging.WARNING, __file__, 0, "value %s", (_Unprintable(),), None
    )
    with captured_logs() as captured:
        handler.handle(record)
    assert [r["event"] for r in captured.records] == ["logging.error"]
    captured.assert_conforms()


@pytest.mark.usefixtures("configured")
def test_assert_conforms_fails_on_non_conforming_line() -> None:
    with captured_logs() as captured:
        print(json.dumps({"event": "test.event"}), file=sys.stderr)
    with pytest.raises(AssertionError, match="missing required field"):
        captured.assert_conforms()


@pytest.mark.usefixtures("configured")
def test_assert_absent() -> None:
    with captured_logs() as captured:
        collide_logging.get_logger("t").info("test.event", note="contains SENTINEL-1 inside")
    captured.assert_absent("SENTINEL-2")
    with pytest.raises(AssertionError, match="SENTINEL-1"):
        captured.assert_absent("SENTINEL-1")
    with captured_logs() as prose:
        logging.getLogger("t").info("hello SENTINEL-3 world")
    with pytest.raises(AssertionError):
        prose.assert_absent("SENTINEL-3")
