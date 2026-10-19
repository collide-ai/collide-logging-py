from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
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
from asgiref.sync import iscoroutinefunction  # noqa: E402
from django.http import (  # noqa: E402
    HttpRequest,
    HttpResponse,
    StreamingHttpResponse,
)
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
    assert re.fullmatch(r"[0-9a-f]{16}", response["X-Request-ID"])


def _records(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.strip().splitlines()]


def test_inbound_id_never_becomes_request_id(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> HttpResponse:
        collide_logging.get_logger("test.view").info("view.handled")
        return HttpResponse("ok")

    response = RequestLoggingMiddleware(view)(rf.get("/", HTTP_X_REQUEST_ID="caller-1"))

    by_event = {r["event"]: r for r in _records(capsys.readouterr().out)}
    for event in ("http.request", "view.handled"):
        assert re.fullmatch(r"[0-9a-f]{16}", str(by_event[event]["request_id"]))
        assert by_event[event]["client_request_id"] == "caller-1"
    assert response["X-Request-ID"] == by_event["http.request"]["request_id"]


def test_malformed_inbound_id_dropped(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    RequestLoggingMiddleware(_ok)(rf.get("/", HTTP_X_REQUEST_ID="bad id"))

    assert all("client_request_id" not in r for r in _records(capsys.readouterr().out))


def test_rejects_oversize_header(rf: RequestFactory) -> None:
    big = "x" * 10_000
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.get("/", HTTP_X_REQUEST_ID=big))
    assert response["X-Request-ID"] != big
    assert re.fullmatch(r"[0-9a-f]{16}", response["X-Request-ID"])


def test_rejects_non_printable_header(rf: RequestFactory) -> None:
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.get("/", HTTP_X_REQUEST_ID="bad value"))
    assert response["X-Request-ID"] != "bad value"
    assert re.fullmatch(r"[0-9a-f]{16}", response["X-Request-ID"])


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
    response = middleware(rf.get("/"))

    lines = capsys.readouterr().out.strip().splitlines()
    view_line = next(
        json.loads(line) for line in lines if json.loads(line)["event"] == "view.handled"
    )
    assert view_line["request_id"] == seen[0] == response["X-Request-ID"]


def test_emits_http_request_log_line(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    middleware = RequestLoggingMiddleware(_ok)
    response = middleware(rf.post("/foo/bar"))

    lines = capsys.readouterr().out.strip().splitlines()
    record = next(
        json.loads(line) for line in lines if json.loads(line)["event"] == "http.request"
    )
    assert record["method"] == "POST"
    assert record["path"] == "/foo/bar"
    assert record["status"] == 200
    assert record["request_id"] == response["X-Request-ID"]
    assert "duration_ms" in record


class _FakeUser:
    def __init__(self, *, pk: uuid.UUID, login: str, is_authenticated: bool = True) -> None:
        self.pk = pk
        self._login = login
        self.is_authenticated = is_authenticated

    def get_username(self) -> str:
        return self._login


def _http_request_record(captured: str) -> dict[str, object]:
    return next(
        json.loads(line)
        for line in captured.strip().splitlines()
        if json.loads(line)["event"] == "http.request"
    )


def test_authenticated_request_logs_user_id_not_user(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    pk = uuid.UUID(int=7)
    request = rf.get("/")
    request.user = _FakeUser(pk=pk, login="a@example.com")  # type: ignore[attr-defined]
    RequestLoggingMiddleware(_ok)(request)

    captured = capsys.readouterr().out
    record = _http_request_record(captured)
    assert record["user_id"] == str(pk)
    assert "user" not in record
    assert "@" not in captured


def test_anonymous_request_has_no_user_id(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    request = rf.get("/")
    request.user = _FakeUser(  # type: ignore[attr-defined]
        pk=uuid.UUID(int=7), login="a@example.com", is_authenticated=False
    )
    RequestLoggingMiddleware(_ok)(request)
    RequestLoggingMiddleware(_ok)(rf.get("/"))

    for line in capsys.readouterr().out.strip().splitlines():
        record = json.loads(line)
        assert "user_id" not in record
        assert "user" not in record


def test_clears_after_response(rf: RequestFactory) -> None:
    middleware = RequestLoggingMiddleware(_ok)
    middleware(rf.get("/", HTTP_X_REQUEST_ID="caller-1"))
    ctx = structlog.contextvars.get_contextvars()
    assert "request_id" not in ctx
    assert "client_request_id" not in ctx


def test_clears_on_exception(rf: RequestFactory) -> None:
    def view(_request: HttpRequest) -> HttpResponse:
        raise RuntimeError("boom")

    middleware = RequestLoggingMiddleware(view)
    with pytest.raises(RuntimeError):
        middleware(rf.get("/", HTTP_X_REQUEST_ID="caller-1"))
    ctx = structlog.contextvars.get_contextvars()
    assert "request_id" not in ctx
    assert "client_request_id" not in ctx


# --- #41: exception path emits a request log -------------------------------


def test_exception_emits_500_request_log(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    """An unhandled handler exception still produces an http.request line with
    the traceback, instead of vanishing (the most-worth-logging 5xx case)."""
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> HttpResponse:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        RequestLoggingMiddleware(view)(rf.get("/x", HTTP_X_REQUEST_ID="errid"))

    record = _http_request_record(capsys.readouterr().out)
    assert record["status"] == 500
    assert record["level"] == "error"
    assert record["path"] == "/x"
    assert re.fullmatch(r"[0-9a-f]{16}", str(record["request_id"]))  # bound contextvar
    assert record["client_request_id"] == "errid"
    assert "builtins.RuntimeError" in record["exception"]
    assert "boom" not in record["exception"]
    assert 'File "' in record["exception"]


# --- #41: streaming responses log at stream close --------------------------


def test_streaming_response_logs_at_close(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> StreamingHttpResponse:
        def body() -> Iterator[bytes]:
            yield b"a"
            yield b"b"

        return StreamingHttpResponse(body(), status=200)

    pk = uuid.UUID(int=9)
    request = rf.get("/s", HTTP_X_REQUEST_ID="sid")
    request.user = _FakeUser(pk=pk, login="a@example.com")  # type: ignore[attr-defined]
    response = RequestLoggingMiddleware(view)(request)
    sid = response["X-Request-ID"]
    assert sid != "sid"

    body = b"".join(response.streaming_content)
    assert body == b"ab"
    # The line is deferred to response.close() (which the server calls after
    # streaming), not to body exhaustion, so nothing is logged yet.
    assert "http.request" not in capsys.readouterr().out

    response.close()
    record = _http_request_record(capsys.readouterr().out)
    assert record["status"] == 200
    assert record["path"] == "/s"
    assert record["request_id"] == sid  # passed explicitly; contextvar already reset
    assert record["client_request_id"] == "sid"
    assert record["user_id"] == str(pk)
    assert "duration_ms" in record


def test_streaming_response_logs_on_partial_consume_then_close(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    """Wrapper contract: close() after a partial consume still logs once. On
    WSGI this is the abandonment path (the server calls close() per PEP 3333).
    This drives close() directly rather than through a real server."""
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> StreamingHttpResponse:
        def body() -> Iterator[bytes]:
            yield b"a"
            yield b"b"
            yield b"c"

        return StreamingHttpResponse(body(), status=200)

    response = RequestLoggingMiddleware(view)(rf.get("/s", HTTP_X_REQUEST_ID="sid"))
    chunks = iter(response.streaming_content)
    assert next(chunks) == b"a"  # only the first chunk, then abandon
    response.close()

    record = _http_request_record(capsys.readouterr().out)
    assert record["status"] == 200
    assert re.fullmatch(r"[0-9a-f]{16}", str(record["request_id"]))
    assert record["client_request_id"] == "sid"


# --- #40: async support ----------------------------------------------------


def test_async_view_marks_middleware_coroutine() -> None:
    async def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    async_mw = RequestLoggingMiddleware(view)
    assert async_mw.async_mode is True
    # Django keys off iscoroutinefunction(middleware), not our attribute, so
    # assert the property markcoroutinefunction() is meant to provide.
    assert iscoroutinefunction(async_mw) is True

    sync_mw = RequestLoggingMiddleware(_ok)
    assert sync_mw.async_mode is False
    assert iscoroutinefunction(sync_mw) is False


def test_async_middleware_awaits_and_logs(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok", status=201)

    middleware = RequestLoggingMiddleware(view)
    response = asyncio.run(middleware(rf.get("/a", HTTP_X_REQUEST_ID="aid")))

    record = _http_request_record(capsys.readouterr().out)
    assert record["status"] == 201
    assert record["path"] == "/a"
    assert record["request_id"] == response["X-Request-ID"]
    assert record["client_request_id"] == "aid"
    # Contextvar does not leak past the awaited request.
    assert "request_id" not in structlog.contextvars.get_contextvars()


def test_async_streaming_logs_at_close(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: HttpRequest) -> StreamingHttpResponse:
        async def body() -> object:
            yield b"x"
            yield b"y"

        return StreamingHttpResponse(body(), status=200)

    middleware = RequestLoggingMiddleware(view)

    seen_ids: list[str] = []

    async def drive() -> bytes:
        response = await middleware(rf.get("/as", HTTP_X_REQUEST_ID="asid"))
        assert response["X-Request-ID"] != "asid"
        seen_ids.append(response["X-Request-ID"])
        chunks = b"".join([chunk async for chunk in response.streaming_content])
        response.close()
        return chunks

    assert asyncio.run(drive()) == b"xy"

    record = _http_request_record(capsys.readouterr().out)
    assert record["status"] == 200
    assert record["path"] == "/as"
    assert record["request_id"] == seen_ids[0]
    assert record["client_request_id"] == "asid"


def test_async_streaming_logs_on_partial_consume_then_close(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    """Wrapper contract for async responses: close() after a partial consume
    logs once. NOTE: a real ASGI client-disconnect cancels the task without
    calling close() (Django ASGIHandler), so that path is NOT covered here and
    is a known limitation (see issue tracker). This drives close() directly."""
    collide_logging.configure(service="t", json=True)

    async def view(_request: HttpRequest) -> StreamingHttpResponse:
        async def body() -> object:
            yield b"x"
            yield b"y"
            yield b"z"

        return StreamingHttpResponse(body(), status=200)

    middleware = RequestLoggingMiddleware(view)

    async def drive() -> None:
        response = await middleware(rf.get("/as", HTTP_X_REQUEST_ID="asid"))
        chunks = aiter(response.streaming_content)
        assert await anext(chunks) == b"x"  # one chunk, then abandon
        response.close()

    asyncio.run(drive())

    record = _http_request_record(capsys.readouterr().out)
    assert record["status"] == 200
    assert re.fullmatch(r"[0-9a-f]{16}", str(record["request_id"]))
    assert record["client_request_id"] == "asid"
