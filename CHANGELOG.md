# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-06-11

Mirrors the v0.5.0 Django exception-path fix onto the Starlette/pure-ASGI middleware (#44).

Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.5.1"
```

### Fixed

- **`starlette.RequestLoggingMiddleware` skipped the `http.request` line when the wrapped app raised** (#44). The log line was emitted after `await self.app(...)` returned; an unhandled exception jumped past it into the `finally`, so the genuinely-unhandled 500 — the case most worth logging — produced no request log. The middleware now emits a status-500 `http.request` line at `error` level with the traceback (`exc_info`) before re-raising. No response exists on that path, so the `X-Request-ID` response header is not set and the status is reported as 500 even if `http.response.start` already fired; `request_id` still rides the bound contextvar. This is the ASGI analogue of the Django exception-path fix shipped in v0.5.0. The streaming-duration and client-disconnect concerns of #41/#42 do not apply to pure-ASGI: `await self.app(...)` returns only after the body drains.

## [0.5.0] - 2026-06-10

Overhauls the Django `RequestLoggingMiddleware`: correct user attribution for email-auth services, native async support, accurate streaming durations, and a request log on the exception path. Bundles #34, #40, and #41.

Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.5.0"
```

### Fixed

- **`RequestLoggingMiddleware` logged `user: null` for email-auth users** (#34). The middleware read `user.username`, which is `None` on models that replace the username field with email auth (`USERNAME_FIELD = "email"`, `username = None`) — so every authenticated `http.request` line emitted `"user": null`, defeating request-to-user correlation. It now reads `user.get_username()`, which returns the `USERNAME_FIELD` value and is correct for both default-username and email-auth models. Anonymous and missing-user requests still log `"anonymous"`.
- **`duration_ms` was measured at stream open for streaming responses** (#41). For a `StreamingHttpResponse` the `http.request` line was emitted as soon as the response object was returned — i.e. before the body streamed — logging a near-zero duration. The line is now deferred until the response body finishes streaming (the iterator is instrumented, sync and async), so `duration_ms` reflects time to stream close. The `X-Request-ID` header is still set at open.
- **Unhandled handler exceptions produced no request log** (#41). When `get_response()` raised, only contextvars were reset — the `http.request` line and `X-Request-ID` header were skipped, so the most-worth-logging 5xx requests vanished. The middleware now emits a status-500 `http.request` line carrying the traceback (`exc_info`) before re-raising. (No response exists, so the `X-Request-ID` header cannot be set on that path.)

### Added

- **`RequestLoggingMiddleware` is now async-capable** (#40). It declares `sync_capable` and `async_capable`, detects an async `get_response` at init, and awaits it on a coroutine path — so under ASGI with native-async views it no longer forces a sync/async adaptation boundary that would push downstream async views through thread coordination. Sync deployments are unchanged.

### Notes

- The deferred streaming log passes `request_id` explicitly (the request contextvar is reset when the middleware returns, before the server consumes the body), so streaming `http.request` lines still carry the correct `request_id` but do not inherit any other request-scoped contextvars.
- Streaming responses are logged by hooking `response.close()`. This covers WSGI (the server calls `close()` after the body is exhausted or abandoned, per PEP 3333), ASGI normal completion, and errors. **Known limitation:** an ASGI client-disconnect mid-stream cancels the request task without calling `response.close()` (Django's ASGIHandler), so that one case is not logged (#42). All WSGI, all non-streaming, and ASGI streams that complete normally are unaffected.

## [0.4.1] - 2026-06-10

Hardens validation-mode resolution so a misconfigured prod environment cannot start crashing the host.

Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.4.1"
```

### Fixed

- **Unrecognized `COLLIDE_LOG_VALIDATE` values now fail safe to `lenient`** (#37). Previously only the literal `"lenient"` enabled lenient mode; any other set value (a typo like `leniant`, or a stray trailing space) silently fell through to `raise` mode, where a schema violation throws `EventValidationError` into the host — defeating v0.4.0's "never crashes in prod" guarantee. Now `COLLIDE_LOG_VALIDATE` is normalized (whitespace stripped, case-insensitive); unset or `raise` stays raise-mode, `lenient` is lenient, and any other set value resolves to `lenient` with a one-time `collide_logging.invalid_validate_mode` warning (carrying the offending `value`) so the misconfiguration is visible rather than silent. Note this includes the empty string: `COLLIDE_LOG_VALIDATE=""` now resolves to `lenient` (previously it fell through to raise-mode). Leave the variable unset for raise-mode.

## [0.4.0] - 2026-06-10

Makes the typed-events API safe to recommend for high-value and error-path events. Fully additive at call sites; one prod-mode behavior change (lenient mode no longer drops events).

Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.4.0"
```

### Fixed

- **Lenient mode no longer discards the offending event** (#36). Under `COLLIDE_LOG_VALIDATE=lenient`, a schema violation previously dropped the entire payload and emitted only a tiny `collide_logging.schema_violation` meta-event — so high-value events vanished during incidents. Now the event is emitted best-effort under its real name: unknown fields are dropped, known fields keep their field-level redaction, and a `_schema_violation` field records `violation`/`missing`/`unknown`. The `schema_violation` meta-event is still emitted alongside as an alertable signal. `raise` mode is unchanged.

### Added

- **`CollideLogger.event()` accepts `level=` and `exc_info=`** (#33). Schema-validated events can now be emitted at any level (`debug`/`info`/`warning`/`error`/`critical`/`exception`) and carry a traceback, so error-path events no longer regress to INFO with the traceback dropped. Both default to current behavior — a bare `event(name, **fields)` record is byte-identical to before. An unknown `level` raises `ValueError`.
- **Public `digest_value(value)` helper** (#35). Exposes the exact `{"len": …, "sha256": "<8 hex>"}` digest behind `FieldSpec(redact=True)` for hand-curated redaction of sensitive free-text (search queries, transcripts) on the plain `log.info(...)` path. Auto-redaction is name-based only and never inspects values; `digest_value()` is the supported way to redact by value, consistent with the library's own field-level redaction.

### Upgrading

No call-site changes required. Services running `lenient` in prod should add (or confirm) an alert on `event="collide_logging.schema_violation"`, and may now query the best-effort records under their real event names. Avoid declaring an event field named `_schema_violation` — it is now reserved.

## [0.3.0] - 2026-05-12

Bridges foreign stdlib loggers (Django's `django.request`, gunicorn, third-party libraries) through the structlog processor chain. Every line on stdout is now valid collide/v1 JSON — not just lines that originate from a `CollideLogger`.

Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
```

### Behavior

- `configure()` now installs a `structlog.stdlib.ProcessorFormatter`-driven handler. Foreign stdlib records (anything not emitted through `get_logger()`) receive the same timestamp / level / service / logger fields and redaction pass as structlog-originated records. The `event` field will be the rendered log message string rather than a dot-notation event name, which is valid per spec (a `warnings.warn` rather than a failure in `assert_collide_v1`).
- `extra={}` fields on foreign stdlib calls are merged into the event dict before redaction, so `logging.getLogger(...).info("x", extra={"api_key": "secret"})` is redacted the same way a `CollideLogger` call would be.
- Exception info on foreign records (`exc_info=True`) is formatted into the `exception` field, same as for structlog-originated records.
- Existing `CollideLogger` output shape is unchanged.

### Upgrading

No changes required at call sites. Services that were manually silencing `django.request` or other noisy loggers to avoid non-JSON stdout can now remove those workarounds — the records will appear as JSON instead of being dropped. (Silencing is still valid policy if the records are genuinely uninteresting.)

## [0.2.0] - 2026-05-11

Adds a public events API so adapter authors (the first is `collide-logging-hermes`) can declare event schemas and emit validated records without reaching into private internals. Fully additive — every v0.1.0 call site keeps working.

Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.2.0"
```

### Public API

- `collide_logging.FieldSpec(type, required=False, redact=False)` — one field on an event schema.
- `collide_logging.EventSchema(name, fields, description="")` — one named event type.
- `collide_logging.register_event_schema(schema)` — idempotent on identical re-registration; raises `ValueError` on a conflicting redefinition.
- `collide_logging.list_schemas()` — returns registered schemas sorted by name.
- `collide_logging.EventValidationError` — raised on schema violations in raise mode.
- `collide_logging.CollideLogger` — `BoundLogger` subclass returned by `get_logger()`. Adds `event(name, **fields)` that validates the call, redacts flagged fields, and emits through the standard processor chain.

### Behavior

- Validation mode is controlled by `COLLIDE_LOG_VALIDATE`. Unset or `raise` (dev default): unknown event names, missing required fields, and unknown field keys raise `EventValidationError`. `lenient` (prod): drops the offending record and emits a `collide_logging.schema_violation` meta-event instead. Never crashes the host process.
- Fields flagged `redact=True` are replaced with `{"len": <bytes>, "sha256": "<first 8 hex>"}` before emission. Non-`str`/`bytes` values are coerced through `repr()` first. Global suffix-based redaction (`*_token`, etc.) still applies on top, unchanged from v0.1.0.

## [0.1.0] - 2026-04-30

Initial release for internal consumption. Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.1.0"
```

Implements the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md).

### Public API

- `collide_logging.configure(service, *, level, json, extra_redact_keys)` — wires structlog and stdlib logging to emit `collide/v1`-conformant JSON (or a developer-friendly console renderer for TTYs).
- `collide_logging.get_logger(name)` — thin wrapper over `structlog.get_logger`.
- `collide_logging.bind_worker_run_id(run_id=None, **extra)` — context manager that binds `worker_run_id` (8-hex-char auto-generation) and arbitrary extras to all logs emitted in the block. Restores prior bindings on exit, including on exceptions.
- `collide_logging.with_worker_run_id` — decorator equivalent.
- `collide_logging.testing.assert_collide_v1(record)` — conformance assertion suitable for use with `structlog.testing.capture_logs` in downstream test suites.

### Framework adapters

- `collide_logging.django.RequestLoggingMiddleware` (`[django]` extra) — binds `request_id`, propagates `X-Request-ID`, emits `http.request`.
- `collide_logging.starlette.RequestLoggingMiddleware` (`[fastapi]` extra) — pure-ASGI; works with Starlette and FastAPI.
- `collide_logging.flask.init_app(app)` (`[flask]` extra) — registers before / after / teardown hooks.

### Spec coverage

- Required fields (`timestamp`, `level`, `service`, `logger`, `event`) emitted on every log line.
- ISO-8601 timestamps with timezone.
- Secret redaction by exact field name (case-insensitive) plus the spec-mandated suffix rules `*_token`, `*_api_token`, `*_signing_secret`.
- Correlation ID flow via structlog contextvars (`request_id` from middleware, `worker_run_id` from helpers).

[0.3.0]: https://github.com/collide-ai/collide-logging-py/releases/tag/v0.3.0
[0.2.0]: https://github.com/collide-ai/collide-logging-py/releases/tag/v0.2.0
[0.1.0]: https://github.com/collide-ai/collide-logging-py/releases/tag/v0.1.0
