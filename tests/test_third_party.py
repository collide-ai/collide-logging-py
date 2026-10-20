from __future__ import annotations

import json
import logging
import logging.config
from collections.abc import Iterator
from typing import Any

import pytest

import collide_logging
from collide_logging import _HANDLER_TAG

_LOGGERS = ("httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access")


async def _noop_app(scope: Any, receive: Any, send: Any) -> None:
    return None


@pytest.fixture(autouse=True)
def _restore_third_party_loggers() -> Iterator[None]:
    saved = {
        name: (
            logging.getLogger(name).level,
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in _LOGGERS
    }
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    for name, (level, handlers, propagate, disabled) in saved.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate
        lg.disabled = disabled


def _assert_uvicorn_routes_through_chain(capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    try:
        raise ValueError("canary")
    except ValueError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    logging.getLogger("uvicorn.access").info("GET /canary-path 200")

    captured = capsys.readouterr()
    lines = [ln for ln in (captured.out + captured.err).splitlines() if ln.strip()]
    assert lines
    records = [json.loads(ln) for ln in lines]
    assert all(r["service"] == "t" for r in records)
    assert "canary" not in captured.out + captured.err
    assert not any(r["logger"] == "uvicorn.access" for r in records)


def test_httpx_held_at_warning(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).info('HTTP Request: GET https://canary.example/x "200 OK"')
        logging.getLogger(name).warning(f"{name}.warned")

    out = capsys.readouterr().out
    assert "canary" not in out
    assert "httpx.warned" in out
    assert "httpcore.warned" in out


def test_configured_level_raises_third_party_floor(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, level="error")
    logging.getLogger("uvicorn.error").info("uvicorn.informed")
    logging.getLogger("httpx").warning("httpx.warned")
    logging.getLogger("uvicorn.error").error("uvicorn.errored")

    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "uvicorn.informed" not in out
    assert "httpx.warned" not in out
    assert "uvicorn.errored" in out


def test_uvicorn_access_stays_off_after_dictconfig(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})
    capsys.readouterr()
    logging.getLogger("uvicorn.access").warning("GET /canary-path 200")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_uvicorn_cli_order_routes_through_chain(capsys: pytest.CaptureFixture[str]) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    uvicorn.Config(app=_noop_app).configure_logging()
    collide_logging.configure(service="t", json=True)
    _assert_uvicorn_routes_through_chain(capsys)


def test_uvicorn_programmatic_order_routes_through_chain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    collide_logging.configure(service="t", json=True)
    uvicorn.Config(app=_noop_app, log_config=None).configure_logging()
    _assert_uvicorn_routes_through_chain(capsys)


def test_reconfigure_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    uvicorn.Config(app=_noop_app).configure_logging()
    for _ in range(3):
        collide_logging.configure(service="t", json=True)
    tagged = [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False)]
    assert len(tagged) == 1
    _assert_uvicorn_routes_through_chain(capsys)
