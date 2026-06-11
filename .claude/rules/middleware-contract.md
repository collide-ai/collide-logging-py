---
paths:
  - "src/collide_logging/django.py"
  - "src/collide_logging/starlette.py"
  - "tests/test_django.py"
  - "tests/test_starlette.py"
---

# Request logging guarantees (the middleware contract)

Both `RequestLoggingMiddleware` adapters (`collide_logging.django`, `collide_logging.starlette`) hold the same contract. Know it before touching either — the edge cases below are deliberate, not bugs to "fix."

- **One `http.request` line per HTTP request.** On normal completion it is emitted at the status-appropriate level: `info` < 400, `warning` for 4xx, `error` for 5xx. It always carries `request_id` (from a well-formed inbound `X-Request-ID` or a generated 8-hex-char ID), `method`, `path`, `status`, and `duration_ms`. The Django line also carries `user` (`get_username()`, so email-auth models attribute correctly; `"anonymous"` otherwise); the Starlette/ASGI adapter does not log a user.
- **Exception path (both adapters, #41 Django / #44 Starlette).** When the wrapped handler/app raises an unhandled exception, the middleware emits a status-500 line at `error` level carrying the traceback (`exc_info`) before re-raising. No response exists on that path, so the `X-Request-ID` response header is **not** set and the status is reported as **500 even if the response start already fired** — the request failed, and that is the truthful signal. `request_id` still appears because it rides the bound contextvar (reset happens in a `finally`, after the emit).
- **`request_id` never leaks past a request.** It is bound on entry and reset in a `finally`, on every path including the exception path.
- **Django streaming responses defer the line until stream close** (`response.close()`), so `duration_ms` reflects time-to-close rather than time-to-first-byte, and abandonment still logs. This deferral is Django-only — pure-ASGI (`starlette.py`) has no streaming special case because `await self.app(...)` returns only after the body drains, so its `duration_ms` is already correct.
- **One known un-loggable case — Django ASGI client-disconnect mid-stream (#42, open).** On an ASGI `StreamingHttpResponse` whose client disconnects before the body completes, Django's `ASGIHandler` cancels the request task and sends `request_finished` **without** calling `response.close()`, so the deferred line never fires and that one request does not log. Every other termination (WSGI anything, all non-streaming, ASGI streaming that completes or errors) logs. This is not currently fixable from a standard `MIDDLEWARE` entry — see #42 before attempting it, and do not assume the Starlette adapter shares it (it does not: pure-ASGI drains before the `await` returns).
