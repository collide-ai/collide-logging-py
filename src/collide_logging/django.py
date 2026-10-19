"""Django adapter for collide-logging.

Drop-in middleware that binds ``request_id`` (and ``client_request_id`` when the
inbound header is well-formed) to structlog contextvars for the
duration of a request, then echoes it back as the ``X-Request-ID`` response
header. The ID is always server-generated (16 hex chars); a well-formed inbound
``X-Request-ID`` is never adopted and is logged as ``client_request_id``.

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
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse

from collide_logging import get_logger
from collide_logging._ids import generate_id, parse_inbound_id

_logger = get_logger("collide_logging.django")

_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_META_KEY = "HTTP_X_REQUEST_ID"


def _request_ids(request: HttpRequest) -> dict[str, str]:
    ids = {"request_id": generate_id()}
    client_request_id = parse_inbound_id(request.META.get(_REQUEST_ID_META_KEY))
    if client_request_id is not None:
        ids["client_request_id"] = client_request_id
    return ids


def _user_id(request: HttpRequest) -> str | None:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return str(user.pk)
    return None


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _emit_request_log(
    *,
    method: str | None,
    path: str,
    status: int,
    duration_ms: int,
    user_id: str | None,
    ids: dict[str, str] | None = None,
    exc_info: bool = False,
) -> None:
    """Emit one ``http.request`` line at the status-appropriate level.

    ``ids`` (``request_id`` and any ``client_request_id``) is passed explicitly
    only for the deferred streaming case, where the contextvar has already been
    reset by the time the body finishes; in the normal and exception paths it
    rides the bound contextvar instead.
    """
    fields: dict[str, Any] = {
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": duration_ms,
    }
    if user_id is not None:
        fields["user_id"] = user_id
    if ids is not None:
        fields.update(ids)
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
      1. Generates a 16-hex-char ``request_id``. A well-formed inbound
         ``X-Request-ID`` is never adopted; it is recorded as
         ``client_request_id``.
      2. Binds both to structlog's contextvars for the request.
      3. Calls the next handler.
      4. Sets ``X-Request-ID`` on the response to the bound ID.
      5. Emits an ``http.request`` log line with method, path, status,
         duration_ms, and ``user_id`` when authenticated. For a
         ``StreamingHttpResponse`` the line is emitted when the body finishes
         streaming, so ``duration_ms`` reflects
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

        ids = _request_ids(request)
        tokens = structlog.contextvars.bind_contextvars(**ids)
        start = time.monotonic()
        try:
            try:
                response = self.get_response(request)
            except Exception:
                self._emit_for_exception(request, start)
                raise
            self._finish(request, response, ids, start)
            return response  # type: ignore[no-any-return]
        finally:
            structlog.contextvars.reset_contextvars(**tokens)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        ids = _request_ids(request)
        tokens = structlog.contextvars.bind_contextvars(**ids)
        start = time.monotonic()
        try:
            try:
                response = await self.get_response(request)
            except Exception:
                self._emit_for_exception(request, start)
                raise
            self._finish(request, response, ids, start)
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
                user_id=_user_id(request),
                exc_info=True,
            )

    def _finish(
        self,
        request: HttpRequest,
        response: Any,
        ids: dict[str, str],
        start: float,
    ) -> None:
        response[_REQUEST_ID_HEADER] = ids["request_id"]
        if getattr(response, "streaming", False):
            self._instrument_stream(request, response, ids, start)
        else:
            _emit_request_log(
                method=request.method,
                path=request.path,
                status=response.status_code,
                duration_ms=_elapsed_ms(start),
                user_id=_user_id(request),
            )

    def _instrument_stream(
        self,
        request: HttpRequest,
        response: Any,
        ids: dict[str, str],
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
        streams, so request metadata is snapshotted now and the ids passed
        explicitly. ``duration_ms`` is measured at close, i.e. stream end.
        """
        method = request.method
        path = request.path
        status = response.status_code
        user_id = _user_id(request)
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
                        user_id=user_id,
                        ids=ids,
                    )

        response.close = close_and_log
