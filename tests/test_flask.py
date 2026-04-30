from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator

import pytest

pytest.importorskip("flask")

import structlog
from flask import Flask

import collide_logging
from collide_logging.flask import init_app


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)
    ]


def _make_app(view: Callable[[], object] | None = None) -> Flask:
    app = Flask(__name__)
    init_app(app)
    if view is None:
        app.add_url_rule("/", view_func=lambda: "ok", endpoint="root")
    else:
        app.add_url_rule("/", view_func=view, endpoint="root")
    return app


def test_generates_when_absent() -> None:
    response = _make_app().test_client().get("/")
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) == 8


def test_uses_inbound() -> None:
    response = _make_app().test_client().get("/", headers={"X-Request-ID": "abc12345"})
    assert response.headers["X-Request-ID"] == "abc12345"


def test_rejects_oversize() -> None:
    big = "x" * 10_000
    response = _make_app().test_client().get("/", headers={"X-Request-ID": big})
    assert response.headers["X-Request-ID"] != big
    assert len(response.headers["X-Request-ID"]) == 8


def test_response_carries_id() -> None:
    response = _make_app().test_client().get("/", headers={"X-Request-ID": "ok-1"})
    assert response.headers["X-Request-ID"] == "ok-1"


def test_logs_inside_view_carry_id(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    seen: list[str] = []

    def view() -> str:
        log = collide_logging.get_logger("test.view")
        log.info("view.handled")
        seen.append(structlog.contextvars.get_contextvars()["request_id"])
        return "ok"

    _make_app(view).test_client().get("/", headers={"X-Request-ID": "seen-id"})

    lines = capsys.readouterr().out.strip().splitlines()
    view_line = next(
        json.loads(line) for line in lines if json.loads(line)["event"] == "view.handled"
    )
    assert view_line["request_id"] == "seen-id"
    assert seen == ["seen-id"]


def test_emits_http_request_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    _make_app().test_client().get("/", headers={"X-Request-ID": "rid-1"})

    lines = capsys.readouterr().out.strip().splitlines()
    record = next(
        json.loads(line) for line in lines if json.loads(line)["event"] == "http.request"
    )
    assert record["method"] == "GET"
    assert record["path"] == "/"
    assert record["status"] == 200
    assert record["request_id"] == "rid-1"
    assert "duration_ms" in record


def test_clears_after_request() -> None:
    _make_app().test_client().get("/")
    assert "request_id" not in structlog.contextvars.get_contextvars()


def test_clears_on_exception() -> None:
    def boom() -> str:
        raise RuntimeError("boom")

    app = _make_app(boom)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    response = app.test_client().get("/")
    assert response.status_code == 500
    assert "request_id" not in structlog.contextvars.get_contextvars()
