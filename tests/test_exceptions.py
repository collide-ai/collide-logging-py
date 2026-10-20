from __future__ import annotations

import json as json_lib
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import collide_logging
from collide_logging._processors import _MAX_EXCEPTION_CHAIN


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _record(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    return json_lib.loads(out.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_exception_messages_never_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log = collide_logging.get_logger("t")
    try:
        try:
            raise KeyError("canary-2")
        except KeyError as inner:
            err = ValueError("canary-1")
            err.add_note("canary-3")
            raise err from inner
    except ValueError:
        log.exception("x.failed")

    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception_type"] == "builtins.ValueError"
    assert "exc_info" not in record
    for needle in ("builtins.ValueError", "builtins.KeyError", 'File "'):
        assert needle in record["exception"]


def test_exception_chain_types_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log = collide_logging.get_logger("t")
    try:
        raise ExceptionGroup("canary-g", [TypeError("canary-t"), OSError("canary-o")])
    except ExceptionGroup:
        log.exception("x.failed")

    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception_type"] == "builtins.ExceptionGroup"
    for needle in ("builtins.TypeError", "builtins.OSError"):
        assert needle in record["exception"]


def test_safe_attributes_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log = collide_logging.get_logger("t")

    http_err = RuntimeError("canary-h")
    http_err.response = SimpleNamespace(status_code=503)  # type: ignore[attr-defined]
    try:
        raise http_err
    except RuntimeError:
        log.exception("http.failed")
    record = _record(capsys)
    assert record["status_code"] == 503

    db_err = RuntimeError("canary-d")
    db_err.diag = SimpleNamespace(sqlstate="23505", constraint_name="uniq_x")  # type: ignore[attr-defined]
    try:
        raise db_err
    except RuntimeError:
        log.exception("db.failed")
    record = _record(capsys)
    assert record["sqlstate"] == "23505"
    assert record["constraint"] == "uniq_x"


def test_safe_attributes_do_not_overwrite_caller_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="svc", json=True)
    err = RuntimeError("x")
    err.response = SimpleNamespace(status_code=503)  # type: ignore[attr-defined]
    try:
        raise err
    except RuntimeError:
        collide_logging.get_logger("t").exception("up.failed", status_code=502)
    assert _record(capsys)["status_code"] == 502


def test_non_int_status_code_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    err = RuntimeError("x")
    err.response = SimpleNamespace(status_code="canary-503")  # type: ignore[attr-defined]
    try:
        raise err
    except RuntimeError:
        collide_logging.get_logger("t").exception("http.failed")
    out = capsys.readouterr().out
    assert "canary-" not in out
    assert "status_code" not in json_lib.loads(out.strip().splitlines()[-1])


def test_cyclic_chain_terminates(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    a, b = ValueError("canary-a"), KeyError("canary-b")
    a.__context__ = b
    b.__context__ = a
    try:
        raise a
    except ValueError:
        collide_logging.get_logger("t").exception("loop.failed")
    record = _record(capsys)
    assert record["exception"].count("builtins.KeyError") == 1


def test_foreign_record_exception_rendered_by_type(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    try:
        raise ValueError("canary-boom")
    except ValueError:
        logging.getLogger("lib").exception("boom")

    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception_type"] == "builtins.ValueError"
    assert 'File "' in record["exception"]
    assert "exc_info" not in record


def test_non_str_sqlstate_falls_back_to_diag(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    err = RuntimeError("x")
    err.sqlstate = 12345  # type: ignore[attr-defined]
    err.diag = SimpleNamespace(sqlstate="23505")  # type: ignore[attr-defined]
    try:
        raise err
    except RuntimeError:
        collide_logging.get_logger("t").exception("db.failed")
    assert _record(capsys)["sqlstate"] == "23505"


def test_long_chain_is_capped(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    exc: BaseException = ValueError("canary-0")
    for i in range(_MAX_EXCEPTION_CHAIN * 2):
        nxt = ValueError(f"canary-{i + 1}")
        nxt.__cause__ = exc
        exc = nxt
    try:
        raise exc
    except ValueError:
        collide_logging.get_logger("t").exception("deep.failed")
    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception"].count("builtins.ValueError") == _MAX_EXCEPTION_CHAIN
