"""ASGI adapter for collide-logging.

Pure-ASGI middleware. Works for Starlette, FastAPI, and any other ASGI
framework. Binds ``request_id`` to structlog contextvars for the lifetime of
one HTTP request, propagates it on the response as ``X-Request-ID``, and
emits an ``http.request`` log line.

Wire-up (Starlette / FastAPI)::

    from collide_logging.starlette import RequestLoggingMiddleware

    app.add_middleware(RequestLoggingMiddleware)

This module imports starlette types at the top — it lives behind the
``[fastapi]`` extra. Do not import it from ``collide_logging.__init__``.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from collide_logging import get_logger

_logger = get_logger("collide_logging.starlette")

_REQUEST_ID_HEADER = b"x-request-id"
_MAX_REQUEST_ID_LEN = 64
_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]+$")


def _generate_request_id() -> str:
    return uuid.uuid4().hex[:8]


def _is_valid_inbound(value: str) -> bool:
    return 0 < len(value) <= _MAX_REQUEST_ID_LEN and _VISIBLE_ASCII.fullmatch(value) is not None


def _emit_request_log(
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    exc_info: bool = False,
) -> None:
    """Emit one ``http.request`` line at the status-appropriate level.

    ``exc_info=True`` (the exception path) threads a traceback onto the
    ``error``-level record.
    """
    fields = {
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": duration_ms,
    }
    extra = {"exc_info": True} if exc_info else {}
    if status >= 500:
        _logger.error("http.request", **extra, **fields)
    elif status >= 400:
        _logger.warning("http.request", **fields)
    else:
        _logger.info("http.request", **fields)


def _read_inbound_request_id(scope: Scope) -> str | None:
    headers = scope.get("headers") or []
    for name, raw_value in headers:
        if name == _REQUEST_ID_HEADER:
            try:
                value = raw_value.decode("latin-1")
            except UnicodeDecodeError:
                return None
            return value if _is_valid_inbound(value) else None
    return None


class RequestLoggingMiddleware:
    """ASGI middleware. Binds ``request_id`` for the lifetime of one request.

    Emits exactly one ``http.request`` line per HTTP request:
      - On normal completion, at the status-appropriate level (``info`` < 400,
        ``warning`` for 4xx, ``error`` for 5xx) once ``self.app`` returns.
      - When ``self.app`` raises an unhandled exception, a status-500 line at
        ``error`` level carrying the traceback (``exc_info``), emitted before
        the exception re-propagates. No response exists on that path, so the
        ``X-Request-ID`` response header is not set and the status is reported
        as 500 even if ``http.response.start`` already fired. ``request_id``
        still rides the bound contextvar.

    ``duration_ms`` is measured when ``self.app`` returns. For pure-ASGI this
    is correct for streaming responses too: the ``await`` returns only after
    the body has drained, so the Django streaming-deferral and client-disconnect
    boundary (issue #42) do not apply here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = _read_inbound_request_id(scope)
        request_id = inbound if inbound is not None else _generate_request_id()
        request_id_bytes = request_id.encode("latin-1")

        status_holder: dict[str, int] = {"status": 0}

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
                headers = [
                    (n, v)
                    for n, v in message.get("headers", [])
                    if n.lower() != _REQUEST_ID_HEADER
                ]
                headers.append((_REQUEST_ID_HEADER, request_id_bytes))
                message["headers"] = headers
            await send(message)

        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.monotonic()
        try:
            try:
                await self.app(scope, receive, send_with_header)
            except Exception:
                # An unhandled exception reached the middleware. Emit a 500 line
                # with the traceback before re-raising so the failure is logged.
                # Status is reported as 500 even if http.response.start already
                # fired: the request failed, and that is the truthful signal.
                _emit_request_log(
                    method=scope.get("method", ""),
                    path=scope.get("path", ""),
                    status=500,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    exc_info=True,
                )
                raise
            _emit_request_log(
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status=status_holder["status"],
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
