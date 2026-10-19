from __future__ import annotations

import json as json_lib
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

import collide_logging
from collide_logging import _HANDLER_TAG, RESERVED_CONTEXT_FIELDS, StrictModeViolation


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    collide_logging.configure(service="reset", json=True)
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]


def _strict(**kwargs: Any) -> None:
    kwargs.setdefault("first_party", ("app",))
    collide_logging.configure(service="t", json=True, strict=True, **kwargs)


def _last_json_line(captured: str) -> dict[str, Any]:
    return json_lib.loads(captured.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_reserved_fields_exported() -> None:
    assert {"request_id", "user_id", "trace_id"} <= RESERVED_CONTEXT_FIELDS
    assert issubclass(StrictModeViolation, AssertionError)


def test_strict_rejects_prose_event() -> None:
    _strict()
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        logging.getLogger("app.mod").info("Shutdown complete")


def test_strict_rejects_positional_args() -> None:
    _strict()
    with pytest.raises(StrictModeViolation, match="positional arguments are not allowed"):
        collide_logging.get_logger("app.mod").info("a.b", 1)  # type: ignore[call-arg]
    with pytest.raises(StrictModeViolation, match="positional arguments are not allowed"):
        logging.getLogger("app.mod").info("a.b %s", "x")


def test_strict_rejects_conflicting_context_value() -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id="7")
    conflict = "user_id conflicts with the bound context value"
    with pytest.raises(StrictModeViolation, match=conflict):
        collide_logging.get_logger("app.mod").info("a.b", user_id="5")
    with pytest.raises(StrictModeViolation, match=conflict):
        logging.getLogger("app.mod").info("a.b", extra={"user_id": "5"})
    with pytest.raises(StrictModeViolation, match=conflict):
        collide_logging.get_logger("app.mod").bind(user_id="5").info("a.b")


def test_strict_allows_same_value(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id="7")
    collide_logging.get_logger("app.mod").info("a.b", user_id="7")
    logging.getLogger("app.mod").info("a.c", extra={"user_id": "7"})
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json_lib.loads(x)["event"] for x in lines] == ["a.b", "a.c"]


def test_strict_allows_unbound_value(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    collide_logging.get_logger("app.mod").info("a.b", account_id="a")
    assert _last_json_line(capsys.readouterr().out)["account_id"] == "a"


def test_strict_rejects_message_and_task_kwargs() -> None:
    _strict()
    log = collide_logging.get_logger("app.mod")
    with pytest.raises(StrictModeViolation, match=r"reserved call-site field\(s\) \['message'\]"):
        log.info("a.b", message="x")
    with pytest.raises(StrictModeViolation, match=r"reserved call-site field\(s\) \['task'\]"):
        log.info("a.b", task="x")
    with pytest.raises(StrictModeViolation, match=r"reserved call-site field\(s\) \['task'\]"):
        logging.getLogger("app.mod").info("a.b", extra={"task": "x"})


def test_strict_allows_bound_context_task(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(task="pkg.mod.fn")
    collide_logging.get_logger("app.mod").info("a.b")
    logging.getLogger("app.mod").info("a.c")
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json_lib.loads(x)["task"] for x in lines] == ["pkg.mod.fn"] * 2


def test_strict_exempt_logger_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    _strict(strict_exempt=("app.legacy",))
    logging.getLogger("app.legacy.x").info("Hello there")
    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == "Hello there"


def test_strict_ignores_third_party_stdlib(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    logging.getLogger("lib").info("hello %s", "x")
    assert _last_json_line(capsys.readouterr().out)["message"] == "hello x"


def test_strict_survives_reconfigure() -> None:
    _strict()
    _strict()
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        logging.getLogger("app.mod").info("Shutdown complete")


def test_strict_off_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, first_party=("app",))
    collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    assert _last_json_line(capsys.readouterr().out)["event"] == "log.message"


def test_strict_allows_bind_matching_context(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id="7")
    collide_logging.get_logger("app.mod").bind(user_id="7").info("a.b")
    assert _last_json_line(capsys.readouterr().out)["user_id"] == "7"


def test_strict_same_rendered_value_passes(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id=5)
    collide_logging.get_logger("app.mod").info("a.b", user_id="5")
    logging.getLogger("app.mod").info("a.c", extra={"user_id": "5"})
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json_lib.loads(x)["event"] for x in lines] == ["a.b", "a.c"]


def test_strict_exempt_structlog_logger(capsys: pytest.CaptureFixture[str]) -> None:
    _strict(strict_exempt=("app.legacy",))
    collide_logging.get_logger("app.legacy.x").info("Hello there")  # type: ignore[arg-type]
    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == "Hello there"


def test_strict_rejects_non_str_event() -> None:
    _strict()
    log: Any = collide_logging.get_logger("app.mod")
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        log.info(42)


def test_strict_empty_first_party_skips_stdlib(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, strict=True)
    logging.getLogger("app.mod").info("Shutdown %s", "complete")
    assert _last_json_line(capsys.readouterr().out)["message"] == "Shutdown complete"


def test_reconfigure_without_strict_turns_it_off(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    collide_logging.configure(service="t", json=True, first_party=("app",))
    collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    logging.getLogger("app.mod").info("a.b %s", "x")
    events = [json_lib.loads(x)["event"] for x in capsys.readouterr().out.strip().splitlines()]
    assert events == ["log.message", "log.message"]


@pytest.mark.parametrize("parameter", ["first_party", "strict_exempt"])
def test_strict_rejects_str_prefix_arguments(parameter: str) -> None:
    with pytest.raises(TypeError, match=parameter):
        collide_logging.configure(service="t", json=True, strict=True, **{parameter: "app"})


@pytest.mark.parametrize(
    ("message", "args"), [("a.b", (1,)), ("a.b %s %s", (1,))], ids=["no-placeholder", "too-few"]
)
def test_strict_rejects_mismatched_stdlib_args(
    monkeypatch: pytest.MonkeyPatch, message: str, args: tuple[object, ...]
) -> None:
    _strict()
    root = logging.getLogger()
    # pytest's own root handler formats first and would raise its own TypeError.
    monkeypatch.setattr(root, "handlers", [h for h in root.handlers if hasattr(h, _HANDLER_TAG)])
    with pytest.raises(StrictModeViolation, match="positional arguments are not allowed"):
        logging.getLogger("app.mod").info(message, *args)
