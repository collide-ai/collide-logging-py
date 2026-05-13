from __future__ import annotations

import json as json_lib
import logging
from collections.abc import Iterator
from typing import Any

import pytest

import collide_logging


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _last_json_line(captured: str) -> dict[str, Any]:
    line = captured.strip().splitlines()[-1]
    return json_lib.loads(line)  # type: ignore[no-any-return]


def test_foreign_stdlib_record_produces_collide_v1_json(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    logging.getLogger("django.request").warning("Unauthorized: /api/v1/health")

    record = _last_json_line(capsys.readouterr().out)
    for field in ("timestamp", "level", "service", "logger", "event"):
        assert field in record, f"missing required field: {field}"
    assert record["event"] == "Unauthorized: /api/v1/health"
    assert record["level"] == "warning"
    assert record["service"] == "test-svc"
    assert record["logger"] == "django.request"


def test_foreign_record_extra_fields_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    logging.getLogger("some.lib").info("doing auth", extra={"api_key": "hunter2"})

    record = _last_json_line(capsys.readouterr().out)
    assert record["api_key"] == "***REDACTED***"


def test_structlog_originated_record_shape_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    log = collide_logging.get_logger("test.module")
    log.info("test.event", extra_field="val")

    record = _last_json_line(capsys.readouterr().out)
    for field in ("timestamp", "level", "service", "logger", "event"):
        assert field in record, f"missing required field: {field}"
    assert record["event"] == "test.event"
    assert record["level"] == "info"
    assert record["service"] == "test-svc"
    assert record["logger"] == "test.module"
    assert record["extra_field"] == "val"


def test_foreign_record_with_exc_info_includes_exception(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("some.lib").error("something failed", exc_info=True)

    record = _last_json_line(capsys.readouterr().out)
    assert "exception" in record
    assert "ValueError" in record["exception"]
    assert "boom" in record["exception"]
