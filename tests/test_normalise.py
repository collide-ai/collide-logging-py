from __future__ import annotations

import json as json_lib
import logging
from collections.abc import Iterator
from typing import Any

import pytest

import collide_logging
from collide_logging import _HANDLER_TAG


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]


def _last_json_line(captured: str) -> dict[str, Any]:
    return json_lib.loads(captured.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_nonconforming_event_normalised(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").info("Shutdown complete")  # type: ignore[arg-type]

    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == "Shutdown complete"


def test_conforming_event_untouched(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").info("a.b")

    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "a.b"
    assert "message" not in out


def test_foreign_record_normalised(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    logging.getLogger("lib").info("hello %s", "x")

    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == "hello x"
    assert out["logger"] == "lib"


def test_trailing_newline_event_normalised(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").info("a.b\n")  # type: ignore[arg-type]

    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == "a.b\n"


@pytest.mark.parametrize(("event", "expected"), [(None, "None"), (5, "5")])
def test_non_string_event_normalised(
    capsys: pytest.CaptureFixture[str], event: Any, expected: str
) -> None:
    collide_logging.configure(service="svc", json=True)
    log: Any = collide_logging.get_logger("t")
    log.info(event)

    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == expected


def test_dict_event_redacted_before_normalising(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log: Any = collide_logging.get_logger("t")
    log.info({"password": "x", "ok": 1})

    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert "***REDACTED***" in out["message"]
    assert "'x'" not in out["message"]


def test_bearer_in_prose_event_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log: Any = collide_logging.get_logger("t")
    log.info("Bearer abc.def")

    out = _last_json_line(capsys.readouterr().out)
    assert out["message"] == "Bearer ***REDACTED***"
