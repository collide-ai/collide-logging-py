from __future__ import annotations

import json as json_lib
import logging
import uuid
from collections.abc import Iterator, MutableMapping
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

import collide_logging
from collide_logging._processors import _guarded


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    root = logging.getLogger()
    saved = list(root.handlers)
    yield
    root.handlers = saved


def _configure() -> None:
    # pytest's capture handlers re-raise from handleError and repr the raw record, which would
    # mask the behaviour under test.
    collide_logging.configure(service="svc", json=True)
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if getattr(h, "_collide_logging_handler", False)]


def _lines(capsys: pytest.CaptureFixture[str]) -> tuple[list[str], str]:
    captured = capsys.readouterr()
    return [ln for ln in captured.out.splitlines() if ln.strip()], captured.err


def test_processor_failure_emits_logging_error() -> None:
    def raising(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        raise ZeroDivisionError("canary")

    out = _guarded(raising)(
        None,
        "info",
        {"event": "a.b", "logger": "x", "service": "s", "secret_field": "canary"},
    )

    assert set(out) == {"event", "error_type", "logger", "level", "timestamp", "service"}
    assert out["event"] == "logging.error"
    assert out["error_type"] == "ZeroDivisionError"
    assert out["level"] == "error"
    assert "canary" not in json_lib.dumps(out)


def test_guarded_passes_through_on_success() -> None:
    def ok(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict["added"] = 1
        return event_dict

    assert _guarded(ok)(None, "info", {"event": "a.b"}) == {"event": "a.b", "added": 1}


def test_percent_args_mismatch_emits_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    logging.getLogger("lib").warning("a %s %s", "only-one-canary")

    lines, err = _lines(capsys)
    assert len(lines) == 1
    record = json_lib.loads(lines[0])
    assert record["event"] == "logging.error"
    assert record["error_type"] == "TypeError"
    assert record["logger"] == "lib"
    assert record["service"] == "svc"
    assert "only-one-canary" not in lines[0]
    assert err.strip() == ""


def test_unserialisable_value_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    class BadRepr:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    _configure()
    collide_logging.get_logger("t").info(
        "x.done", s={1}, b=b"x", d=datetime.now(), dec=Decimal("1.5"), bad=BadRepr()
    )

    lines, err = _lines(capsys)
    assert len(lines) == 1
    record = json_lib.loads(lines[0])
    assert record["event"] == "x.done"
    assert record["bad"] == "<BadRepr>"
    assert err.strip() == ""


def test_non_json_dict_keys_keep_the_line(capsys: pytest.CaptureFixture[str]) -> None:
    key = uuid.uuid4()
    _configure()
    collide_logging.get_logger("t").info(
        "x.done", u={key: 1, b"password": "s", (1, 2): 3, Decimal("1.5"): 4}
    )

    lines, err = _lines(capsys)
    assert len(lines) == 1
    record = json_lib.loads(lines[0])
    assert record["event"] == "x.done"
    assert record["u"][str(key)] == 1
    assert record["u"]["password"] == "***REDACTED***"
    assert record["u"]["(1, 2)"] == 3
    assert record["u"]["1.5"] == 4
    assert err.strip() == ""


def test_shared_container_in_event_redacted_everywhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    x = {"password": 1}
    _configure()
    collide_logging.get_logger("t").info("a.b", data={"a": x, "b": [x, x]})

    lines, _ = _lines(capsys)
    data = json_lib.loads(lines[0])["data"]
    assert data == {"a": {"password": "***REDACTED***"}, "b": [{"password": "***REDACTED***"}] * 2}


def test_structlog_renderer_failure_is_caught_by_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    collide_logging.get_logger("t").info("x.done", k=10**5000)

    lines, err = _lines(capsys)
    assert len(lines) == 1
    assert json_lib.loads(lines[0])["event"] == "logging.error"
    assert err.strip() == ""


def test_raising_exception_property_does_not_break_log_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BoomError(Exception):
        @property
        def response(self) -> object:
            raise RuntimeError("canary-prop")

    _configure()
    try:
        raise BoomError("m")
    except BoomError:
        collide_logging.get_logger("t").exception("x.failed")

    lines, err = _lines(capsys)
    assert len(lines) == 1
    record = json_lib.loads(lines[0])
    assert record["event"] == "x.failed"
    assert record["exception_type"].endswith("BoomError")
    assert "status_code" not in record
    assert "canary-prop" not in lines[0]
    assert err.strip() == ""


def test_foreign_logger_exception_with_raising_property(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BoomError(Exception):
        @property
        def response(self) -> object:
            raise RuntimeError("canary-prop")

    _configure()
    try:
        raise BoomError("m")
    except BoomError:
        logging.getLogger("lib").exception("boom")

    lines, err = _lines(capsys)
    assert len(lines) == 1
    record = json_lib.loads(lines[0])
    assert record["event"] == "boom"
    assert record["exception_type"].endswith("BoomError")
    assert record["logger"] == "lib"
    assert record["service"] == "svc"
    assert err.strip() == ""


def test_early_processor_failure_keeps_logger_and_service() -> None:
    def failing(
        logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        raise ValueError("canary")

    out = _guarded(failing, "svc")(None, "info", {"event": "a.b", "logger_name": "early"})

    assert out["logger"] == "early"
    assert out["service"] == "svc"
    assert out["error_type"] == "ValueError"


def test_fallback_error_line_carries_no_exception_so_strict_rule_skips_it() -> None:
    out = _guarded(lambda *_: 1 / 0)(None, "info", {"event": "a.b", "logger": "x"})

    assert out["event"] == "logging.error"
    assert "exc_info" not in out
