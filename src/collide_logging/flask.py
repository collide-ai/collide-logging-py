"""Flask adapter for collide-logging.

`init_app(app)` registers before/after/teardown hooks that bind ``request_id``
to structlog contextvars for the duration of each request, propagate it on
the response as ``X-Request-ID``, and emit an ``http.request`` log line.
Cleanup runs in `teardown_request`, which fires even when the view raises.

Wire-up::

    from flask import Flask
    from collide_logging.flask import init_app

    app = Flask(__name__)
    init_app(app)

This module imports Flask at the top — it lives behind the ``[flask]``
extra. Do not import it from ``collide_logging.__init__``.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from flask import Flask, Response, g, request

from collide_logging import get_logger

_logger = get_logger("collide_logging.flask")

_REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LEN = 64
_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]+$")

_TOKENS_ATTR = "_collide_logging_tokens"
_REQUEST_ID_ATTR = "_collide_logging_request_id"
_START_ATTR = "_collide_logging_start"


def _generate_request_id() -> str:
    return uuid.uuid4().hex[:8]


def _is_valid_inbound(value: str) -> bool:
    return 0 < len(value) <= _MAX_REQUEST_ID_LEN and _VISIBLE_ASCII.fullmatch(value) is not None


def init_app(app: Flask) -> None:
    """Register request hooks that bind ``request_id`` per request."""

    @app.before_request
    def _before() -> None:
        inbound = request.headers.get(_REQUEST_ID_HEADER, "")
        request_id = inbound if _is_valid_inbound(inbound) else _generate_request_id()
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        setattr(g, _TOKENS_ATTR, tokens)
        setattr(g, _REQUEST_ID_ATTR, request_id)
        setattr(g, _START_ATTR, time.monotonic())

    @app.after_request
    def _after(response: Response) -> Response:
        request_id = getattr(g, _REQUEST_ID_ATTR, None)
        if request_id is not None:
            response.headers[_REQUEST_ID_HEADER] = request_id

        start = getattr(g, _START_ATTR, None)
        if start is not None:
            duration_ms = int((time.monotonic() - start) * 1000)
            status = response.status_code
            fields = {
                "method": request.method,
                "path": request.path,
                "status": status,
                "duration_ms": duration_ms,
            }
            if status >= 500:
                _logger.error("http.request", **fields)
            elif status >= 400:
                _logger.warning("http.request", **fields)
            else:
                _logger.info("http.request", **fields)
        return response

    @app.teardown_request
    def _teardown(_exc: BaseException | None) -> None:
        tokens = getattr(g, _TOKENS_ATTR, None)
        if tokens is not None:
            structlog.contextvars.reset_contextvars(**tokens)
