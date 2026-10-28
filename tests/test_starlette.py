from __future__ import annotations

import asyncio
import gc
import json
import logging
import re
import socket
import threading
import time
import weakref
from collections.abc import Iterator

import pytest

pytest.importorskip("starlette")

import httpx
import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import collide_logging
from collide_logging._unit import current_unit, record_exception
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
    assert record["level"] == "warning"
    assert record["path"] == "/"
    assert re.fullmatch(r"[0-9a-f]{16}", str(record["request_id"]))  # bound contextvar
    assert record["client_request_id"] == "errid"
    assert "builtins.RuntimeError" in record["exception"]
    assert 'File "' in record["exception"]


def test_middleware_under_strict_emits_one_http_request_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("app",))
    response = _make_client(_ok).get("/")

    assert response.status_code == 200
    lines = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert len(lines) == 1


def test_starlette_outcome_error(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)

    async def boom(_request: Request) -> Response:
        collide_logging.annotate(rows=4)
        with collide_logging.log_context(step="load"):
            raise ValueError("SENTINEL")

    response = _make_client(boom).get("/")

    assert response.status_code == 500
    captured = capsys.readouterr().out
    (record,) = [r for r in _records(captured) if r["event"] == "http.request"]
    assert record["outcome"] == "error"
    assert record["status"] == 500
    assert record["error_type"] == "ValueError"
    assert record["step"] == "load"
    assert record["rows"] == 4
    assert "SENTINEL" not in captured


def test_starlette_outcome_ok_with_annotations(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: Request) -> Response:
        collide_logging.annotate(rows=3)
        return PlainTextResponse("missing", status_code=404)

    _make_client(view).get("/")

    (record,) = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert record["outcome"] == "ok"
    assert record["rows"] == 3
    assert record["level"] == "info"
    assert "error_type" not in record


@pytest.mark.parametrize("status", [400, 401, 404, 409, 499])
def test_starlette_4xx_logs_at_info(capsys: pytest.CaptureFixture[str], status: int) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: Request) -> Response:
        return PlainTextResponse("no", status_code=status)

    _make_client(view).get("/")

    (record,) = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert (record["status"], record["outcome"], record["level"]) == (status, "ok", "info")


def test_starlette_4xx_with_recorded_exception_logs_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: Request) -> Response:
        try:
            raise LookupError("SECRET")
        except LookupError as exc:
            record_exception(exc)
        return PlainTextResponse("missing", status_code=404)

    _make_client(view).get("/")

    (record,) = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert (record["status"], record["outcome"], record["level"]) == (404, "error", "warning")
    assert record["exception_type"] == "builtins.LookupError"
    assert re.search(r'File ".*test_starlette\.py", line \d+, in view', str(record["exception"]))


def test_starlette_status_500_is_error_outcome(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: Request) -> Response:
        return PlainTextResponse("down", status_code=503)

    _make_client(view).get("/")

    (record,) = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert (record["outcome"], record["level"]) == ("error", "warning")
    assert "error_type" not in record


def test_starlette_outage_logs_error(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)

    async def view(_request: Request) -> Response:
        try:
            raise ConnectionRefusedError("SECRET")
        except ConnectionRefusedError as exc:
            raise RuntimeError("search unavailable") from exc

    _make_client(view).get("/")

    (record,) = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert (record["status"], record["outcome"], record["level"]) == (500, "error", "error")
    assert record["exception_type"] == "builtins.RuntimeError"
    assert "builtins.ConnectionRefusedError" in record["exception"]


def test_starlette_concurrent_requests_keep_own_annotations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)
    both_annotated = asyncio.Event()
    annotated: list[str] = []

    async def view(request: Request) -> Response:
        name = request.query_params["n"]
        collide_logging.annotate(**{f"only_{name}": 1, "who": name})
        annotated.append(name)
        if len(annotated) == 2:
            both_annotated.set()
        await both_annotated.wait()
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", view)])
    app.add_middleware(RequestLoggingMiddleware)

    async def drive() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            await asyncio.gather(client.get("/?n=a"), client.get("/?n=b"))
        assert current_unit() is None

    asyncio.run(drive())

    lines = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    by_who = {r["who"]: r for r in lines}
    assert set(by_who) == {"a", "b"}
    assert "only_b" not in by_who["a"]
    assert "only_a" not in by_who["b"]


def test_starlette_unit_closed_after_exception() -> None:
    async def boom(scope: Scope, receive: Receive, send: Send) -> None:
        collide_logging.annotate(rows=1)
        raise RuntimeError("boom")

    async def drive() -> None:
        await _drive(boom, fail=True)
        assert current_unit() is None

    asyncio.run(drive())


def test_starlette_ids_reset_even_if_closing_the_unit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_close(_unit: object, _token: object) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr("collide_logging.starlette.close_unit", failing_close)

    async def ok(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    async def drive() -> dict[str, object]:
        scope = {"type": "http", "method": "GET", "path": "/", "headers": []}

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message: Message) -> None:
            return None

        with pytest.raises(RuntimeError, match="close failed"):
            await RequestLoggingMiddleware(ok)(scope, receive, send)  # type: ignore[arg-type]
        return dict(structlog.contextvars.get_contextvars())

    assert "request_id" not in asyncio.run(drive())


def test_library_outcome_lines_pass_strict(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("app",))

    async def boom(_request: Request) -> Response:
        raise ValueError("SENTINEL")

    response = _make_client(boom).get("/")

    assert response.status_code == 500
    (record,) = [r for r in _records(capsys.readouterr().out) if r["event"] == "http.request"]
    assert record["outcome"] == "error"
    assert record["error_type"] == "ValueError"


def _serve_with_uvicorn(app: ASGIApp, path: str) -> int:
    """Serve ``app`` with a real uvicorn server for one request; return the response status."""
    uvicorn = pytest.importorskip("uvicorn")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.01)
        return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=10).status_code
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def test_uvicorn_failure_writes_one_failure_line(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)

    async def boom(_request: Request) -> Response:
        with collide_logging.log_context(step="s"):
            raise ValueError("SENTINEL")

    app = Starlette(routes=[Route("/", boom)])
    app.add_middleware(RequestLoggingMiddleware)

    assert _serve_with_uvicorn(app, "/") == 500

    captured = capsys.readouterr()
    records = _records(captured.out)
    errors = [r for r in records if r["level"] in ("warning", "error")]
    assert [(r["event"], r["level"]) for r in errors] == [("http.request", "warning")]
    assert errors[0]["step"] == "s"
    uvicorn_lines = [r for r in records if r["logger"] == "uvicorn.error"]
    assert uvicorn_lines, "uvicorn's own startup lines still pass"
    assert not any("exception" in r for r in uvicorn_lines)
    assert "SENTINEL" not in captured.out + captured.err


async def _refused(_request: Request) -> Response:
    raise ConnectionRefusedError("SENTINEL")


async def _failed(_request: Request) -> Response:
    raise ValueError("SENTINEL")


@pytest.mark.parametrize(
    ("view", "logger", "exception_type"),
    [
        (_refused, "collide_logging.starlette", "builtins.ConnectionRefusedError"),
        (_failed, "uvicorn.error", "builtins.ValueError"),
    ],
)
def test_uvicorn_failure_at_error_level_writes_one_line_with_traceback(
    capsys: pytest.CaptureFixture[str], view: object, logger: str, exception_type: str
) -> None:
    """A non-outage outcome line is a warning, so at level=error uvicorn's own line must pass."""
    collide_logging.configure(service="t", json=True, level="error")
    app = Starlette(routes=[Route("/", view)])  # type: ignore[arg-type]
    app.add_middleware(RequestLoggingMiddleware)

    assert _serve_with_uvicorn(app, "/") == 500

    captured = capsys.readouterr()
    (record,) = _records(captured.out)
    assert (record["logger"], record["level"]) == (logger, "error")
    assert record["exception_type"] == exception_type
    assert 'File "' in record["exception"]
    assert "SENTINEL" not in captured.out + captured.err


class _LateError(Exception):
    pass


def test_child_task_after_the_response_reaches_no_unit(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    response_sent = asyncio.Event()
    children: list[asyncio.Task[None]] = []
    units: list[object] = []
    late_errors: list[weakref.ref[_LateError]] = []

    async def late_work() -> None:
        await response_sent.wait()
        collide_logging.annotate(late=1)
        error = _LateError("late")
        late_errors.append(weakref.ref(error))
        record_exception(error)

    async def view(_request: Request) -> Response:
        units.append(current_unit())
        children.append(asyncio.create_task(late_work()))
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", view)])
    app.add_middleware(RequestLoggingMiddleware)

    async def drive() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            await client.get("/")
        response_sent.set()
        await children[0]

    asyncio.run(drive())
    gc.collect()

    (unit,) = units
    assert unit.annotations == {}  # type: ignore[attr-defined]
    assert unit.exc is None  # type: ignore[attr-defined]
    assert late_errors[0]() is None
    (record,) = _records(capsys.readouterr().out)
    assert (record["event"], record["outcome"]) == ("http.request", "ok")
    assert "late" not in record
