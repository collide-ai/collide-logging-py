from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from typing import ClassVar

import pytest

pytest.importorskip("django")

import django

django.setup()

import structlog  # noqa: E402
from asgiref.sync import iscoroutinefunction  # noqa: E402
from django.http import (  # noqa: E402
    HttpRequest,
    HttpResponse,
    StreamingHttpResponse,
)
from django.test import AsyncClient, Client, RequestFactory, override_settings  # noqa: E402
from django.urls import URLPattern, path  # noqa: E402

import collide_logging  # noqa: E402
from collide_logging._unit import (  # noqa: E402
    RecordedExceptionFilter,
    close_unit,
    current_unit,
    open_unit,
)
from collide_logging.django import RequestLoggingMiddleware, _record_request_exception  # noqa: E402


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


def test_middleware_under_strict_accepts_int_pk_bound_by_consumer(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("app",))
    structlog.contextvars.bind_contextvars(user_id=5)
    request = rf.get("/")
    request.user = _FakeUser(pk=5, login="a@example.com")  # type: ignore[attr-defined,arg-type]
    RequestLoggingMiddleware(_ok)(request)

    lines = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert len(lines) == 1
    assert lines[0]["user_id"] == "5"


def _boom_view(_request: HttpRequest) -> HttpResponse:
    with collide_logging.log_context(step="load"):
        raise ValueError("SENTINEL")


def _unavailable_view(_request: HttpRequest) -> HttpResponse:
    return HttpResponse("down", status=503)


class _Urls:
    urlpatterns: ClassVar[list[URLPattern]] = [
        path("boom", _boom_view),
        path("unavailable", _unavailable_view),
    ]


_MIDDLEWARE = ["collide_logging.django.RequestLoggingMiddleware"]


@pytest.fixture
def django_stack() -> Iterator[None]:
    with override_settings(ROOT_URLCONF=_Urls, MIDDLEWARE=_MIDDLEWARE, DEBUG=False):
        yield


def _request_lines(captured: str) -> list[dict[str, object]]:
    return [r for r in _records(captured) if r["event"] == "http.request"]


def test_django_outcome_ok_with_annotations(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> HttpResponse:
        collide_logging.annotate(rows=3)
        return HttpResponse("ok")

    RequestLoggingMiddleware(view)(rf.get("/"))

    (record,) = _request_lines(capsys.readouterr().out)
    assert record["outcome"] == "ok"
    assert record["rows"] == 3
    assert record["level"] == "info"
    assert "error_type" not in record


def test_annotations_never_override_line_fields(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> HttpResponse:
        collide_logging.annotate(status=1, path="/fake", method="X", outcome="fake")
        return HttpResponse("ok")

    RequestLoggingMiddleware(view)(rf.get("/real"))

    (record,) = _request_lines(capsys.readouterr().out)
    assert (record["status"], record["path"], record["method"]) == (200, "/real", "GET")
    assert record["outcome"] == "ok"


def test_django_view_exception_outcome(
    capsys: pytest.CaptureFixture[str], django_stack: None
) -> None:
    collide_logging.configure(service="t", json=True)

    response = Client(raise_request_exception=False).get("/boom")

    assert response.status_code == 500
    captured = capsys.readouterr().out
    (record,) = _request_lines(captured)
    assert record["outcome"] == "error"
    assert record["status"] == 500
    assert record["level"] == "error"
    assert record["error_type"] == "ValueError"
    assert record["exception_type"] == "builtins.ValueError"
    assert record["step"] == "load"
    assert "SENTINEL" not in captured


def test_django_no_duplicate_request_error_line(
    capsys: pytest.CaptureFixture[str], django_stack: None
) -> None:
    collide_logging.configure(service="t", json=True)

    Client(raise_request_exception=False).get("/boom")

    records = _records(capsys.readouterr().out)
    assert [r for r in records if r.get("logger") == "django.request"] == []
    assert [r["event"] for r in records] == ["http.request"]


def test_django_request_filter_keeps_other_records(
    capsys: pytest.CaptureFixture[str], django_stack: None
) -> None:
    collide_logging.configure(service="t", json=True)

    Client().get("/unavailable")

    records = _records(capsys.readouterr().out)
    django_request = [r for r in records if r.get("logger") == "django.request"]
    assert [(r["level"], r["status_code"]) for r in django_request] == [("error", 503)]
    (record,) = [r for r in records if r["event"] == "http.request"]
    assert record["outcome"] == "error"
    assert record["status"] == 503
    assert "error_type" not in record


def test_django_async_view_exception_outcome(
    capsys: pytest.CaptureFixture[str], django_stack: None
) -> None:
    """Under ASGI the sync view, and Django's conversion of its exception, run in asgiref
    threads with copied contexts; the unit is shared through them."""
    collide_logging.configure(service="t", json=True)

    async def drive() -> int:
        response = await AsyncClient(raise_request_exception=False).get("/boom")
        return int(response.status_code)

    assert asyncio.run(drive()) == 500
    captured = capsys.readouterr().out
    records = _records(captured)
    assert [r["event"] for r in records] == ["http.request"]
    record = records[0]
    assert record["outcome"] == "error"
    assert record["error_type"] == "ValueError"
    assert record["step"] == "load"
    assert "SENTINEL" not in captured


def _django_request_record(exc: BaseException) -> logging.LogRecord:
    return logging.LogRecord(
        "django.request", logging.ERROR, "f", 1, "m", None, (type(exc), exc, None)
    )


def test_got_request_exception_receiver_never_raises() -> None:
    _record_request_exception(sender=None)
    try:
        raise ValueError("x")
    except ValueError as error:
        _record_request_exception(sender=None)
        assert current_unit() is None
        assert RecordedExceptionFilter().filter(_django_request_record(error))


def test_got_request_exception_receiver_swallows_a_failing_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_record(_exc: object) -> None:
        raise RuntimeError("record failed")

    monkeypatch.setattr("collide_logging.django.record_exception", failing_record)
    unit, token = open_unit()
    try:
        try:
            raise ValueError("x")
        except ValueError:
            _record_request_exception(sender=None)
    finally:
        close_unit(unit, token)
    assert unit.exc is None


def test_raw_exception_outcome_carries_error_fields(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> HttpResponse:
        collide_logging.annotate(rows=1)
        with collide_logging.log_context(step="save"):
            raise RuntimeError("SENTINEL")

    with pytest.raises(RuntimeError):
        RequestLoggingMiddleware(view)(rf.get("/x"))

    captured = capsys.readouterr().out
    (record,) = _request_lines(captured)
    assert record["outcome"] == "error"
    assert record["error_type"] == "RuntimeError"
    assert record["step"] == "save"
    assert record["rows"] == 1
    assert "SENTINEL" not in captured


def test_annotate_does_not_leak_between_units(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def first(_request: HttpRequest) -> HttpResponse:
        collide_logging.annotate(x=1)
        return HttpResponse("ok")

    RequestLoggingMiddleware(first)(rf.get("/1"))
    RequestLoggingMiddleware(_ok)(rf.get("/2"))

    one, two = _request_lines(capsys.readouterr().out)
    assert one["x"] == 1
    assert "x" not in two
    assert current_unit() is None


def test_unit_closed_after_exception(rf: RequestFactory) -> None:
    def view(_request: HttpRequest) -> HttpResponse:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        RequestLoggingMiddleware(view)(rf.get("/"))
    assert current_unit() is None


def test_async_annotate_does_not_leak_between_units(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    async def first(_request: HttpRequest) -> HttpResponse:
        collide_logging.annotate(x=1)
        return HttpResponse("ok")

    async def second(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    async def drive() -> None:
        await RequestLoggingMiddleware(first)(rf.get("/1"))
        await RequestLoggingMiddleware(second)(rf.get("/2"))
        assert current_unit() is None

    asyncio.run(drive())

    one, two = _request_lines(capsys.readouterr().out)
    assert one["x"] == 1
    assert one["outcome"] == "ok"
    assert "x" not in two


def test_shared_exception_instance_across_units(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    shared = ValueError("shared")

    def view_for(account_id: str) -> Callable[[HttpRequest], HttpResponse]:
        def view(_request: HttpRequest) -> HttpResponse:
            with collide_logging.log_context(account_id=account_id):
                raise shared

        return view

    for account_id in ("a", "b"):
        with pytest.raises(ValueError):
            RequestLoggingMiddleware(view_for(account_id))(rf.get("/"))

    one, two = _request_lines(capsys.readouterr().out)
    assert one["account_id"] == "a"
    assert two["account_id"] == "b"


def test_streaming_outcome_uses_annotations_at_finish(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> StreamingHttpResponse:
        collide_logging.annotate(rows=2)

        def body() -> Iterator[bytes]:
            collide_logging.annotate(late=1)
            yield b"a"

        return StreamingHttpResponse(body(), status=200)

    response = RequestLoggingMiddleware(view)(rf.get("/s"))
    b"".join(response.streaming_content)
    response.close()

    (record,) = _request_lines(capsys.readouterr().out)
    assert record["outcome"] == "ok"
    assert record["rows"] == 2
    assert "late" not in record


def test_annotate_in_a_streamed_body_under_strict_does_nothing(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("app",))

    def view(_request: HttpRequest) -> StreamingHttpResponse:
        def body() -> Iterator[bytes]:
            collide_logging.annotate(late=1)
            collide_logging.annotate(**{"Bad-Name": 1})
            yield b"a"

        return StreamingHttpResponse(body(), status=200)

    response = RequestLoggingMiddleware(view)(rf.get("/s"))
    b"".join(response.streaming_content)
    response.close()

    (record,) = _request_lines(capsys.readouterr().out)
    assert record["outcome"] == "ok"
    assert "late" not in record


def test_reraised_exception_without_a_block_carries_no_stale_context(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)
    shared = ValueError("shared")

    def first(_request: HttpRequest) -> HttpResponse:
        with collide_logging.log_context(account_id="a", step="one"):
            raise shared

    def second(_request: HttpRequest) -> HttpResponse:
        raise shared

    for view in (first, second):
        with pytest.raises(ValueError):
            RequestLoggingMiddleware(view)(rf.get("/"))

    one, two = _request_lines(capsys.readouterr().out)
    assert (one["account_id"], one["step"]) == ("a", "one")
    assert "account_id" not in two
    assert "step" not in two


async def _async_boom_view(_request: HttpRequest) -> HttpResponse:
    with collide_logging.log_context(step="fetch"):
        raise ValueError("SENTINEL")


class _AsyncUrls:
    urlpatterns: ClassVar[list[URLPattern]] = [path("aboom", _async_boom_view)]


def test_django_async_view_raising_gives_one_outcome_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)

    async def drive() -> int:
        response = await AsyncClient(raise_request_exception=False).get("/aboom")
        return int(response.status_code)

    with override_settings(ROOT_URLCONF=_AsyncUrls, MIDDLEWARE=_MIDDLEWARE, DEBUG=False):
        assert asyncio.run(drive()) == 500

    captured = capsys.readouterr().out
    records = _records(captured)
    assert [r["event"] for r in records] == ["http.request"]
    assert records[0]["outcome"] == "error"
    assert records[0]["error_type"] == "ValueError"
    assert records[0]["step"] == "fetch"
    assert "SENTINEL" not in captured


class _Collecting(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_other_django_request_handlers_still_receive_view_exceptions(
    capsys: pytest.CaptureFixture[str], django_stack: None
) -> None:
    collide_logging.configure(service="t", json=True)
    collecting = _Collecting()
    request_logger = logging.getLogger("django.request")
    request_logger.addHandler(collecting)
    try:
        Client(raise_request_exception=False).get("/boom")
    finally:
        request_logger.removeHandler(collecting)

    (record,) = collecting.records
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], ValueError)
    assert [r["event"] for r in _records(capsys.readouterr().out)] == ["http.request"]


def test_context_captured_in_a_thread_merges_into_the_request(
    capsys: pytest.CaptureFixture[str], rf: RequestFactory
) -> None:
    collide_logging.configure(service="t", json=True)

    def view(_request: HttpRequest) -> HttpResponse:
        errors: list[ValueError] = []

        def work() -> None:
            try:
                with collide_logging.log_context(inner="i"):
                    raise ValueError("x")
            except ValueError as error:
                errors.append(error)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        with collide_logging.log_context(outer="o"):
            raise errors[0]

    with pytest.raises(ValueError):
        RequestLoggingMiddleware(view)(rf.get("/"))

    (record,) = _request_lines(capsys.readouterr().out)
    assert (record["inner"], record["outer"]) == ("i", "o")


def _failing_close(_unit: object, _token: object) -> None:
    raise RuntimeError("close failed")


def test_ids_reset_even_if_closing_the_unit_fails(
    monkeypatch: pytest.MonkeyPatch, rf: RequestFactory
) -> None:
    monkeypatch.setattr("collide_logging.django.close_unit", _failing_close)

    def drive() -> dict[str, object]:
        # In a copied context, so the unit the failed close leaves open stays there.
        with pytest.raises(RuntimeError, match="close failed"):
            RequestLoggingMiddleware(_ok)(rf.get("/", HTTP_X_REQUEST_ID="caller-1"))
        return dict(structlog.contextvars.get_contextvars())

    ctx = contextvars.copy_context().run(drive)
    assert "request_id" not in ctx
    assert "client_request_id" not in ctx


def test_async_ids_reset_even_if_closing_the_unit_fails(
    monkeypatch: pytest.MonkeyPatch, rf: RequestFactory
) -> None:
    monkeypatch.setattr("collide_logging.django.close_unit", _failing_close)

    async def view(_request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    async def drive() -> dict[str, object]:
        with pytest.raises(RuntimeError, match="close failed"):
            await RequestLoggingMiddleware(view)(rf.get("/", HTTP_X_REQUEST_ID="caller-1"))
        return dict(structlog.contextvars.get_contextvars())

    ctx = asyncio.run(drive())
    assert "request_id" not in ctx
    assert "client_request_id" not in ctx


def test_library_outcome_lines_pass_strict(
    capsys: pytest.CaptureFixture[str], django_stack: None
) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("app",))

    response = Client(raise_request_exception=False).get("/boom")

    assert response.status_code == 500
    (record,) = _request_lines(capsys.readouterr().out)
    assert record["outcome"] == "error"
    assert record["error_type"] == "ValueError"
