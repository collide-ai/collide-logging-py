from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

pytest.importorskip("starlette")

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

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


def test_generates_when_absent() -> None:
    client = _make_client(_ok)
    response = client.get("/")
    assert response.status_code == 200
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) == 8


def test_uses_inbound() -> None:
    client = _make_client(_ok)
    response = client.get("/", headers={"X-Request-ID": "abc12345"})
    assert response.headers["x-request-id"] == "abc12345"


def test_rejects_oversize() -> None:
    client = _make_client(_ok)
    big = "x" * 10_000
    response = client.get("/", headers={"X-Request-ID": big})
    assert response.headers["x-request-id"] != big
    assert len(response.headers["x-request-id"]) == 8


def test_response_carries_id() -> None:
    client = _make_client(_ok)
    response = client.get("/", headers={"X-Request-ID": "ok-1"})
    assert response.headers["x-request-id"] == "ok-1"


def test_logs_inside_route_carry_id(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    seen: list[str] = []

    async def view(_request: Request) -> Response:
        log = collide_logging.get_logger("test.view")
        log.info("view.handled")
        seen.append(structlog.contextvars.get_contextvars()["request_id"])
        return PlainTextResponse("ok")

    client = _make_client(view)
    client.get("/", headers={"X-Request-ID": "seen-id"})

    out = capsys.readouterr().out.strip().splitlines()
    view_line = next(
        json.loads(line) for line in out if json.loads(line)["event"] == "view.handled"
    )
    assert view_line["request_id"] == "seen-id"
    assert seen == ["seen-id"]


def test_emits_http_request_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    client = _make_client(_ok)
    client.get("/", headers={"X-Request-ID": "rid-1"})

    out = capsys.readouterr().out.strip().splitlines()
    record = next(
        json.loads(line) for line in out if json.loads(line)["event"] == "http.request"
    )
    assert record["method"] == "GET"
    assert record["path"] == "/"
    assert record["status"] == 200
    assert record["request_id"] == "rid-1"
    assert "duration_ms" in record


def test_clears_on_exception() -> None:
    async def boom(_request: Request) -> Response:
        raise RuntimeError("boom")

    client = _make_client(boom)
    client.get("/")  # raise_server_exceptions=False so client gets 500
    assert "request_id" not in structlog.contextvars.get_contextvars()
