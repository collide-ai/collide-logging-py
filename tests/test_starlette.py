from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterator

import pytest

pytest.importorskip("starlette")

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import collide_logging
from collide_logging.starlette import RequestLoggingMiddleware


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)
    ]


def _make_client(handler) -> TestClient:  # type: ignore[no-untyped-def]
    app = Starlette(routes=[Route("/", handler)])
    app.add_middleware(RequestLoggingMiddleware)
    return TestClient(app, raise_server_exceptions=False)


async def _ok(_request: Request) -> Response:
    return PlainTextResponse("ok")


def _records(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.strip().splitlines()]


def test_generates_when_absent() -> None:
    client = _make_client(_ok)
    response = client.get("/")
    assert response.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["x-request-id"])


def test_inbound_id_never_becomes_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: Request) -> Response:
        collide_logging.get_logger("test.view").info("view.handled")
        return PlainTextResponse("ok")

    response = _make_client(view).get("/", headers={"X-Request-ID": "caller-1"})

    by_event = {r["event"]: r for r in _records(capsys.readouterr().out)}
    for event in ("http.request", "view.handled"):
        assert re.fullmatch(r"[0-9a-f]{16}", str(by_event[event]["request_id"]))
        assert by_event[event]["client_request_id"] == "caller-1"
    assert response.headers["x-request-id"] == by_event["http.request"]["request_id"]


def test_malformed_inbound_id_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    _make_client(_ok).get("/", headers={"X-Request-ID": "bad id"})

    assert all("client_request_id" not in r for r in _records(capsys.readouterr().out))


def test_non_ascii_inbound_id_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    _make_client(_ok).get("/", headers=[(b"x-request-id", "caf\xe9".encode("latin-1"))])

    assert all("client_request_id" not in r for r in _records(capsys.readouterr().out))


def test_rejects_oversize() -> None:
    client = _make_client(_ok)
    big = "x" * 10_000
    response = client.get("/", headers={"X-Request-ID": big})
    assert response.headers["x-request-id"] != big
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["x-request-id"])


def test_logs_inside_route_carry_id(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    seen: list[str] = []

    async def view(_request: Request) -> Response:
        log = collide_logging.get_logger("test.view")
        log.info("view.handled")
        seen.append(structlog.contextvars.get_contextvars()["request_id"])
        return PlainTextResponse("ok")

    client = _make_client(view)
    response = client.get("/")

    out = capsys.readouterr().out.strip().splitlines()
    view_line = next(
        json.loads(line) for line in out if json.loads(line)["event"] == "view.handled"
    )
    assert view_line["request_id"] == seen[0] == response.headers["x-request-id"]


def test_emits_http_request_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    client = _make_client(_ok)
    response = client.get("/")

    out = capsys.readouterr().out.strip().splitlines()
    record = next(
        json.loads(line) for line in out if json.loads(line)["event"] == "http.request"
    )
    assert record["method"] == "GET"
    assert record["path"] == "/"
    assert record["status"] == 200
    assert record["request_id"] == response.headers["x-request-id"]
    assert "duration_ms" in record


async def _drive(app: ASGIApp, *, fail: bool) -> dict[str, object]:
    """Run one request through the middleware in the current task and return
    the contextvars visible afterwards (TestClient runs the app in another
    thread, so it cannot observe a leak)."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-request-id", b"caller-1")],
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        return None

    try:
        await RequestLoggingMiddleware(app)(scope, receive, send)  # type: ignore[arg-type]
    except RuntimeError:
        assert fail
    return dict(structlog.contextvars.get_contextvars())


def test_clears_after_response() -> None:
    async def ok(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    ctx = asyncio.run(_drive(ok, fail=False))
    assert "request_id" not in ctx
    assert "client_request_id" not in ctx


def test_clears_on_exception() -> None:
    async def boom(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    ctx = asyncio.run(_drive(boom, fail=True))
    assert "request_id" not in ctx
    assert "client_request_id" not in ctx


def test_exception_emits_500_request_log(capsys: pytest.CaptureFixture[str]) -> None:
    """An unhandled exception in the wrapped app still produces an http.request
    line with the traceback, instead of vanishing (the #44 5xx case)."""
    collide_logging.configure(service="t", json=True)

    async def boom(_request: Request) -> Response:
        raise RuntimeError("boom")

    client = _make_client(boom)
    client.get("/", headers={"X-Request-ID": "errid"})

    out = capsys.readouterr().out.strip().splitlines()
    record = next(
        json.loads(line) for line in out if json.loads(line)["event"] == "http.request"
    )
    assert record["status"] == 500
    assert record["level"] == "error"
    assert record["path"] == "/"
    assert re.fullmatch(r"[0-9a-f]{16}", str(record["request_id"]))  # bound contextvar
    assert record["client_request_id"] == "errid"
    assert "builtins.RuntimeError" in record["exception"]
    assert 'File "' in record["exception"]
