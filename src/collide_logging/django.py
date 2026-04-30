"""Django adapter for collide-logging.

Drop-in middleware that binds ``request_id`` to structlog contextvars for the
duration of a request, then echoes it back as the ``X-Request-ID`` response
header. Reads any well-formed inbound ``X-Request-ID`` so caller-supplied IDs
propagate; otherwise generates a fresh 8-hex-char ID.

Wire-up::

    MIDDLEWARE = [
        ...
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "collide_logging.django.RequestLoggingMiddleware",
        ...
    ]

This module imports Django at the top — it lives behind the ``[django]``
extra. Do not import it from ``collide_logging.__init__``.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse

from collide_logging import get_logger

_logger = get_logger("collide_logging.django")

_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_META_KEY = "HTTP_X_REQUEST_ID"
_MAX_REQUEST_ID_LEN = 64
_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]+$")


def _generate_request_id() -> str:
    return uuid.uuid4().hex[:8]


def _is_valid_inbound(value: str) -> bool:
    return 0 < len(value) <= _MAX_REQUEST_ID_LEN and _VISIBLE_ASCII.fullmatch(value) is not None


class RequestLoggingMiddleware:
    """Bind ``request_id`` to logs for the duration of a Django request.

    On each request:
      1. Reads ``X-Request-ID`` from the inbound request when present and
         well-formed (printable ASCII, ``len <= 64``); otherwise generates a
         fresh 8-hex-char ID.
      2. Binds ``request_id`` to structlog's contextvars for the request.
      3. Calls the next handler.
      4. Sets ``X-Request-ID`` on the response to the bound ID.
      5. Emits an ``http.request`` log line with method, path, status,
         duration_ms, and user.
      6. Restores prior contextvar state in a ``finally`` so the ID does not
         leak past the request.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        inbound = request.META.get(_REQUEST_ID_META_KEY, "")
        if isinstance(inbound, str) and _is_valid_inbound(inbound):
            request_id = inbound
        else:
            request_id = _generate_request_id()

        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.monotonic()
        try:
            response = self.get_response(request)

            duration_ms = int((time.monotonic() - start) * 1000)
            response[_REQUEST_ID_HEADER] = request_id

            user = getattr(request, "user", None)
            username = (
                user.username
                if user is not None and getattr(user, "is_authenticated", False)
                else "anonymous"
            )
            fields = {
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "user": username,
            }
            if response.status_code >= 500:
                _logger.error("http.request", **fields)
            elif response.status_code >= 400:
                _logger.warning("http.request", **fields)
            else:
                _logger.info("http.request", **fields)

            return response
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
