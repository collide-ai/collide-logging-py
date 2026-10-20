from __future__ import annotations

import json as json_lib
import logging
import re
import uuid
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


def test_emits_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    log = collide_logging.get_logger("test.module")
    log.info("test.event")

    record = _last_json_line(capsys.readouterr().out)
    for field in ("timestamp", "level", "service", "logger", "event"):
        assert field in record, f"missing required field: {field}"
    assert record["level"] == "info"
    assert record["service"] == "test-svc"
    assert record["logger"] == "test.module"
    assert record["event"] == "test.event"


def test_timestamp_is_iso_with_z(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("e")

    record = _last_json_line(capsys.readouterr().out)
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$",
        record["timestamp"],
    ), f"timestamp does not match spec format: {record['timestamp']!r}"


def test_service_field_matches_arg(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="alpha", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("e")

    record = _last_json_line(capsys.readouterr().out)
    assert record["service"] == "alpha"


def test_redaction_active(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("auth.event", api_key="hunter2", github_token="ghp_abc")

    record = _last_json_line(capsys.readouterr().out)
    assert record["api_key"] == "***REDACTED***"
    assert record["github_token"] == "***REDACTED***"


def test_extra_redact_keys_honored(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(
        service="t",
        json=True,
        extra_redact_keys=["my_special_key"],
    )
    log = collide_logging.get_logger("t.m")
    log.info("e", my_special_key="x")

    record = _last_json_line(capsys.readouterr().out)
    assert record["my_special_key"] == "***REDACTED***"


def test_console_renderer_when_json_false(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=False)
    log = collide_logging.get_logger("t.m")
    log.info("test.event")

    out = capsys.readouterr().out.strip()
    with pytest.raises(json_lib.JSONDecodeError):
        json_lib.loads(out)
    assert "test.event" in out


def test_user_supplied_service_field_is_preserved(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="default-svc", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("e", service="explicit-override")

    record = _last_json_line(capsys.readouterr().out)
    assert record["service"] == "explicit-override"


def test_idempotent_does_not_pile_up_handlers() -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.configure(service="t", json=True)
    collide_logging.configure(service="t", json=True)

    root = logging.getLogger()
    tagged = [h for h in root.handlers if getattr(h, "_collide_logging_handler", False)]
    assert len(tagged) == 1


def test_exception_logs_traceback_at_error_level(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    log = collide_logging.get_logger("test.module")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("test.failed", attempt=2)

    record = _last_json_line(capsys.readouterr().out)
    assert record["level"] == "error"
    assert record["attempt"] == 2
    assert "builtins.ValueError" in record["exception"]
    assert "boom" not in record["exception"]
    assert 'File "' in record["exception"]


def test_positional_args_do_not_raise_and_render_as_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="test-svc", json=True)
    log: Any = collide_logging.get_logger("t")
    log.info("a %s", "b")

    record = _last_json_line(capsys.readouterr().out)
    assert record["message"] == "a %s"
    assert record["positional_args_count"] == 1
    assert "positional_args" not in record


def test_positional_args_values_never_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = "CANARY-positional-7f3a"
    collide_logging.configure(service="test-svc", json=True)
    log: Any = collide_logging.get_logger("t")
    log.info("x.y", sentinel)
    record = _last_json_line(capsys.readouterr().out)
    assert sentinel not in json_lib.dumps(record)
    assert record["positional_args_count"] == 1

    log.info("x.y", note=sentinel)
    assert sentinel in capsys.readouterr().out


def test_uuid_renders_as_canonical_string(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    collide_logging.get_logger("test.module").info("a.b", user_id=uuid.UUID(int=1))

    out = capsys.readouterr().out
    assert _last_json_line(out)["user_id"] == "00000000-0000-0000-0000-000000000001"
    assert "UUID(" not in out


def test_structlog_hook_object_renders_via_hook(capsys: pytest.CaptureFixture[str]) -> None:
    class Hooked:
        def __structlog__(self) -> str:
            return "custom"

    collide_logging.configure(service="test-svc", json=True)
    collide_logging.get_logger("test.module").info("a.b", thing=Hooked())

    assert _last_json_line(capsys.readouterr().out)["thing"] == "custom"


def test_ordinary_object_renders_as_repr(capsys: pytest.CaptureFixture[str]) -> None:
    class Plain:
        def __repr__(self) -> str:
            return "Plain<1>"

    collide_logging.configure(service="test-svc", json=True)
    collide_logging.get_logger("test.module").info("a.b", thing=Plain())

    assert _last_json_line(capsys.readouterr().out)["thing"] == "Plain<1>"


def test_object_with_raising_repr_renders_redaction_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    collide_logging.configure(service="test-svc", json=True)
    # pytest's capture handlers str() the event dict, which would hit the broken repr themselves.
    root = logging.getLogger()
    monkeypatch.setattr(
        root,
        "handlers",
        [h for h in root.handlers if getattr(h, "_collide_logging_handler", False)],
    )
    collide_logging.get_logger("test.module").info("a.b", thing=Broken())

    assert _last_json_line(capsys.readouterr().out)["thing"] == "***REDACTION_ERROR***"
