from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

pytest.importorskip("django")

from django.conf import settings as django_settings

if not django_settings.configured:
    django_settings.configure(
        DEBUG=True,
        ALLOWED_HOSTS=["*"],
        DATABASES={},
        INSTALLED_APPS=[],
        USE_TZ=True,
        DEFAULT_CHARSET="utf-8",
    )

import django

django.setup()

import structlog  # noqa: E402
from django.http import HttpRequest, HttpResponse  # noqa: E402
from django.test import RequestFactory  # noqa: E402

import collide_logging  # noqa: E402
from collide_logging.django import RequestLoggingMiddleware  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)
    ]


@pytest.fixture
def rf() -> RequestFactory:
    return RequestFactory()


def _ok(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok", status=200)


def test_generates_request_id_when_absent(rf: RequestFactory) -> None:
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.get("/"))
    assert "X-Request-ID" in response
    assert len(response["X-Request-ID"]) == 8


def test_uses_inbound_header_when_valid(rf: RequestFactory) -> None:
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.get("/", HTTP_X_REQUEST_ID="abc123"))
    assert response["X-Request-ID"] == "abc123"


def test_rejects_oversize_header(rf: RequestFactory) -> None:
    big = "x" * 10_000
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.get("/", HTTP_X_REQUEST_ID=big))
    assert response["X-Request-ID"] != big
    assert len(response["X-Request-ID"]) == 8


def test_rejects_non_printable_header(rf: RequestFactory) -> None:
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.get("/", HTTP_X_REQUEST_ID="bad value"))
    assert response["X-Request-ID"] != "bad value"
    assert len(response["X-Request-ID"]) == 8


def test_response_carries_request_id(rf: RequestFactory) -> None:
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.get("/", HTTP_X_REQUEST_ID="ok-id-1"))
    assert response["X-Request-ID"] == "ok-id-1"


def test_request_id_in_logs_during_request(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    seen: list[str] = []

    def view(_request: HttpRequest) -> HttpResponse:
        log = collide_logging.get_logger("test.view")
        log.info("view.handled")
        seen.append(structlog.contextvars.get_contextvars()["request_id"])
        return HttpResponse("ok")

    middleware = RequestLoggingMiddleware(view)
    middleware(rf.get("/", HTTP_X_REQUEST_ID="seen-id"))

    lines = capsys.readouterr().out.strip().splitlines()
    view_line = next(
        json.loads(line) for line in lines if json.loads(line)["event"] == "view.handled"
    )
    assert view_line["request_id"] == "seen-id"
    assert seen == ["seen-id"]


def test_emits_http_request_log_line(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    middleware = RequestLoggingMiddleware(_ok)
    middleware(rf.post("/foo/bar", HTTP_X_REQUEST_ID="rid-1"))

    lines = capsys.readouterr().out.strip().splitlines()
    record = next(
        json.loads(line) for line in lines if json.loads(line)["event"] == "http.request"
    )
    assert record["method"] == "POST"
    assert record["path"] == "/foo/bar"
    assert record["status"] == 200
    assert record["request_id"] == "rid-1"
    assert "duration_ms" in record


def test_clears_after_response(rf: RequestFactory) -> None:
    middleware = RequestLoggingMiddleware(_ok)
    middleware(rf.get("/"))
    assert "request_id" not in structlog.contextvars.get_contextvars()


def test_clears_on_exception(rf: RequestFactory) -> None:
    def view(_request: HttpRequest) -> HttpResponse:
        raise RuntimeError("boom")

    middleware = RequestLoggingMiddleware(view)
    with pytest.raises(RuntimeError):
        middleware(rf.get("/"))
    assert "request_id" not in structlog.contextvars.get_contextvars()
