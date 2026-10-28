"""ASGI adapter for collide-logging.

Pure-ASGI middleware. Works for Starlette, FastAPI, and any other ASGI
framework. Binds ``request_id`` (and ``client_request_id`` when the inbound
header is well-formed) to structlog contextvars for the lifetime of one HTTP
request, propagates it on the response as ``X-Request-ID``, and
emits an ``http.request`` log line. The ID is always server-generated; a
well-formed inbound ``X-Request-ID`` is logged as ``client_request_id``.

Wire-up (Starlette / FastAPI)::

    from collide_logging.starlette import RequestLoggingMiddleware

    app.add_middleware(RequestLoggingMiddleware)

Each request is a unit of work: ``collide_logging.annotate`` adds fields to its
``http.request`` line, which carries ``outcome`` and, when the app raised, that
exception's fields.

This module imports starlette types at the top — it lives behind the
``[fastapi]`` extra. Do not import it from ``collide_logging.__init__``.
"""

from __future__ import annotations

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from collide_logging import get_logger
from collide_logging._ids import generate_id, parse_inbound_id
from collide_logging._unit import Unit, close_unit, emit_outcome, open_unit, record_exception

_logger = get_logger("collide_logging.starlette")

_REQUEST_ID_HEADER = b"x-request-id"


def _emit_request_log(unit: Unit, *, scope: Scope, status: int) -> None:
    """Emit one ``http.request`` line for ``unit``."""
    fields: dict[str, object] = {
        "method": scope.get("method", ""),
        "path": scope.get("path", ""),
        "status": status,
    }
    emit_outcome(_logger, "http.request", unit, failed=status >= 500, fields=fields)


def _read_client_request_id(scope: Scope) -> str | None:
    for name, raw_value in scope.get("headers") or []:
        if name == _REQUEST_ID_HEADER:
            return parse_inbound_id(raw_value.decode("latin-1"))
    return None


class RequestLoggingMiddleware:
    """ASGI middleware. Binds ``request_id`` and ``client_request_id`` for one request.

    Each request is a unit of work. Emits exactly one ``http.request`` line per
    HTTP request, carrying ``outcome`` and the request's annotations:
      - On normal completion, once ``self.app`` returns: at ``error`` for 5xx,
        otherwise at ``info`` (a 4xx is the client's error, not degraded service).
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

        ids = {"request_id": generate_id()}
        client_request_id = _read_client_request_id(scope)
        if client_request_id is not None:
            ids["client_request_id"] = client_request_id
        request_id_bytes = ids["request_id"].encode("latin-1")

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

        tokens = structlog.contextvars.bind_contextvars(**ids)
        unit, unit_token = open_unit()
        try:
            try:
                await self.app(scope, receive, send_with_header)
            except Exception as exc:
                # An unhandled exception reached the middleware. Emit a 500 line
                # with the traceback before re-raising so the failure is logged.
                # Status is reported as 500 even if http.response.start already
                # fired: the request failed, and that is the truthful signal.
                record_exception(exc)
                _emit_request_log(unit, scope=scope, status=500)
                raise
            _emit_request_log(unit, scope=scope, status=status_holder["status"])
        finally:
            try:
                close_unit(unit, unit_token)
            finally:
                structlog.contextvars.reset_contextvars(**tokens)
