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

The middleware is sync- and async-capable: under ASGI with native-async views
it runs as a coroutine and does not force a sync/async adaptation boundary.

This module imports Django at the top — it lives behind the ``[django]``
extra. Do not import it from ``collide_logging.__init__``.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
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


def _resolve_request_id(request: HttpRequest) -> str:
    inbound = request.META.get(_REQUEST_ID_META_KEY, "")
    if isinstance(inbound, str) and _is_valid_inbound(inbound):
        return inbound
    return _generate_request_id()


def _username(request: HttpRequest) -> str:
    # get_username() returns the USERNAME_FIELD value, so this is correct for
    # email-auth models (USERNAME_FIELD="email", no username attr) as well as
    # the default username model. Reading user.username directly would log null
    # for the former.
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        username: str = user.get_username()
        return username
    return "anonymous"


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _emit_request_log(
    *,
    method: str | None,
    path: str,
    status: int,
    duration_ms: int,
    user: str,
    request_id: str | None = None,
    exc_info: bool = False,
) -> None:
    """Emit one ``http.request`` line at the status-appropriate level.

    ``request_id`` is passed explicitly only for the deferred streaming case,
    where the contextvar has already been reset by the time the body finishes;
    in the normal and exception paths it rides the bound contextvar instead.
    """
    fields: dict[str, Any] = {
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": duration_ms,
        "user": user,
    }
    if request_id is not None:
        fields["request_id"] = request_id
    extra = {"exc_info": True} if exc_info else {}
    if status >= 500:
        _logger.error("http.request", **extra, **fields)
    elif status >= 400:
        _logger.warning("http.request", **fields)
    else:
        _logger.info("http.request", **fields)


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
         duration_ms, and user. For a ``StreamingHttpResponse`` the line is
         emitted when the body finishes streaming, so ``duration_ms`` reflects
         time to stream close rather than time to first byte. If the handler
         raises, a status-500 line with the traceback is emitted before the
         exception propagates.
      6. Restores prior contextvar state in a ``finally`` so the ID does not
         leak past the request.

    Sync- and async-capable: an async ``get_response`` is awaited and the
    middleware presents itself as a coroutine, so it does not insert a
    sync/async boundary into an ASGI stack.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], Any]) -> None:
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> HttpResponse | Awaitable[HttpResponse]:
        if self.async_mode:
            return self.__acall__(request)

        request_id = _resolve_request_id(request)
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.monotonic()
        try:
            try:
                response = self.get_response(request)
            except Exception:
                self._emit_for_exception(request, start)
                raise
            self._finish(request, response, request_id, start)
            return response  # type: ignore[no-any-return]
        finally:
            structlog.contextvars.reset_contextvars(**tokens)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        request_id = _resolve_request_id(request)
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.monotonic()
        try:
            try:
                response = await self.get_response(request)
            except Exception:
                self._emit_for_exception(request, start)
                raise
            self._finish(request, response, request_id, start)
            return response  # type: ignore[no-any-return]
        finally:
            structlog.contextvars.reset_contextvars(**tokens)

    def _emit_for_exception(self, request: HttpRequest, start: float) -> None:
        # Emitted while the contextvar is still bound, so request_id rides it.
        # No response exists, so the X-Request-ID header cannot be set.
        # Suppress everything: a lazy request.user that raises (or any other
        # field-read failure) must never replace the real handler exception.
        with contextlib.suppress(Exception):
            _emit_request_log(
                method=request.method,
                path=request.path,
                status=500,
                duration_ms=_elapsed_ms(start),
                user=_username(request),
                exc_info=True,
            )

    def _finish(
        self,
        request: HttpRequest,
        response: Any,
        request_id: str,
        start: float,
    ) -> None:
        response[_REQUEST_ID_HEADER] = request_id
        if getattr(response, "streaming", False):
            self._instrument_stream(request, response, request_id, start)
        else:
            _emit_request_log(
                method=request.method,
                path=request.path,
                status=response.status_code,
                duration_ms=_elapsed_ms(start),
                user=_username(request),
            )

    def _instrument_stream(
        self,
        request: HttpRequest,
        response: Any,
        request_id: str,
        start: float,
    ) -> None:
        """Defer the ``http.request`` line until the response is closed.

        Hooks ``response.close()`` rather than wrapping the body iterator (an
        async body generator is not registered as a Django resource closer, so
        wrapping it misses abandonment). ``close()`` is invoked:
          - WSGI: by the server after the body iterable is exhausted or the
            client disconnects (PEP 3333) — covers abandonment.
          - ASGI: by Django after the body fully streams (and on errors).

        Known boundary: on ASGI, a client disconnect mid-stream cancels the
        request task and sends ``request_finished`` *without* calling
        ``response.close()`` (Django's ASGIHandler), so that one case does not
        log (issue #42); it is not currently fixable from middleware. Every
        other termination logs.

        The contextvar is reset when the middleware returns, before the body
        streams, so request metadata is snapshotted now and request_id passed
        explicitly. ``duration_ms`` is measured at close, i.e. stream end.
        """
        method = request.method
        path = request.path
        status = response.status_code
        user = _username(request)
        original_close = response.close
        emitted = False

        def close_and_log() -> None:
            nonlocal emitted
            try:
                original_close()
            finally:
                if not emitted:
                    emitted = True
                    _emit_request_log(
                        method=method,
                        path=path,
                        status=status,
                        duration_ms=_elapsed_ms(start),
                        user=user,
                        request_id=request_id,
                    )

        response.close = close_and_log
