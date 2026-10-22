# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Releases are also published to the `collide-artifacts` Azure Artifacts feed, the preferred install source; git tags stay installable.

## [0.6.0] - 2026-10-20

Typed, strictly checkable logging: context-first failure logging (`log_context`, one outcome line per request or task, `annotate()`), a typed `Logger` contract, a strict mode for test settings, `captured_logs()` and `unit_of_work()` test helpers, django-q task binding, and a tighter redaction and exception pipeline.

Install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.6.0"
```

### Compatibility

Breaking changes. Review each before upgrading.

- **Events API removed.** `CollideLogger.event()`, `EventSchema`, `FieldSpec`, `EventValidationError`, `register_event_schema`, `list_schemas` and `digest_value` are gone, along with the `COLLIDE_LOG_VALIDATE` setting. Call `log.info("event.name", field=value)` instead.
- **Flask adapter removed.** `collide_logging.flask` and the `[flask]` extra are gone.
- **`CollideLogger` is no longer a `structlog.stdlib.BoundLogger`.** It provides `debug`, `info`, `warning`, `error`, `critical`, `exception`, `bind`, `unbind`, `try_unbind` and `new`. The `warn`, `fatal` and `log` methods, the async `a*` variants (`ainfo`, `aexception`, ...), the `name` and `level` attributes and the other stdlib pass-throughs (`setLevel`, `isEnabledFor`, `addHandler`, and similar) are gone. Because `configure()` sets structlog's wrapper class process-wide, this applies to every `structlog.get_logger()` logger after `configure()`, not only `collide_logging.get_logger()`.
- **`positional_args` is now `positional_args_count`.** A stray positional argument to a structlog call is no longer rendered; the line carries only how many there were.
- **Strict mode checks structlog calls from any logger**, not only those under `first_party`; the prefixes apply to stdlib records.
- **`event` is positional-only.** `log.info(event="x")` and a call with no event raise `TypeError`. Positional format args are still accepted at runtime.
- **Non-conforming event names are rewritten.** An event that does not match the event-name rule is emitted as `event="log.message"` with the original text in `message`.
- **Request ids.** The Django and Starlette middlewares never adopt an inbound `X-Request-ID`. `request_id` is always server-generated (16 hex characters, was 8) and the response header carries it. A well-formed inbound id (`[A-Za-z0-9._-]{1,64}`) is logged as `client_request_id`; a malformed one is dropped.
- **Django view exceptions log once.** Django turns a view's exception into a 500 response before middleware sees it; the middleware now records it through `got_request_exception`, puts it on the `http.request` line, and the library's handler skips Django's own `django.request` line for that same exception. Other handlers of `django.request`, such as `mail_admins`, still receive it, and other `django.request` lines are unchanged.
- **`http.request` level follows `outcome`.** An error outcome logs at `error`; otherwise 4xx logs at `warning` and the rest at `info`, as before.
- **Django `http.request` carries `user_id`** (the primary key as a string, omitted when anonymous) instead of `user`.
- **Generated `worker_run_id` is 16 hex characters** (was 8).
- **Exception messages and notes are never logged.** Lines carry `exception_type`, traceback frames without source lines, the type name of each exception in the chain (and of each `ExceptionGroup` member), and the built-in attributes listed under Added, never a URL path or query or other database detail.
- **More default redact keys.** `prompt`, `user_prompt`, `user_query`, `query`, `question`, `completion`, `ai_response`, `messages`, `text_delta`, `tool_input`, `tool_output`, `request_payload`, `response_body`, `email`, `user_email`, `recipient` and `request` now mask fields with those exact names at any depth.
- **Strict mode has new rules** (see Added): a call that carries an exception needs a field describing the operation, and a `log_context` value that would be dropped, or a malformed or library-owned `annotate()` name or an unsupported `annotate()` value, raises.
- **Third-party loggers.** `configure()` sets `httpx` and `httpcore` to WARNING (or the configured level, if higher), overriding a level set before it, routes uvicorn's loggers through the library and turns the uvicorn access log off.
- **Test helpers.** `collide_logging.testing.assert_collide_v1` raises `AssertionError` for an event name that is not dot-notation (it warned before) and checks redaction against `DEFAULT_REDACT_KEYS` and the suffix rules instead of its own copy.

### Added

- `log_context(**fields)`: binds fields to every log line in a block (context manager, async context manager, or decorator for sync and async functions) and records them on an exception that escapes the block, so a failure line logged outside the block still carries them. The innermost block wins and the fields are redacted like any other.
  - **Precedence on a failure line:** call-site fields and the bound context in scope at the log call, then the exception's declared fields, then built-in exception attributes, then the context captured on the exception.
  - **Names** are checked when the block is built: a library-owned name (including `task` and `task_id`) raises `TypeError` (so do `exc_info`, `stack_info` and `stacklevel`); a malformed name or more than 20 fields raises `ValueError`.
  - **Values** that are not a str, an int within 64 bits, a finite float, a bool, None or a UUID are dropped, and the rest of the block still binds. Under `configure(strict=True)` they raise `StrictModeViolation`, as does entering a block that sets a reserved context field (`account_id`, `user_id`, `agent_run_id`) to a value other than the bound one.
  - **Decorating a generator or async-generator function raises `TypeError`**, since the decorator would bind nothing while the generator runs; use the block inside the generator instead.
  - **Exits unwind by identity:** a block's exit also resets any block entered after it that is still open, such as one inside a generator kept past the block, so out-of-order exits and generators finalized later or in another context leave nothing bound.
  - **Known limits:** only the cause and context chain is read, so the declared and captured fields of `ExceptionGroup` members are not. An exception instance raised again in another request or task records that unit's context in place of the earlier one, and carries none of the earlier unit's context when no block surrounds it there. Records made outside a request or task, such as in a plain thread, merge, including into the first request or task the exception reaches.
- One outcome line per request: `http.request` (Django and Starlette) carries `outcome` (`error` for an unhandled exception or a status of 500 or more, `ok` otherwise) and the fields recorded with `annotate()`. After an unhandled exception it also carries `error_type` (the class name), the exception's traceback, attributes and declared fields, and the `log_context` fields active where it was raised. The library's handler skips the server's own record of that same exception: Django's `django.request` line and uvicorn's `Exception in ASGI application` line on `uvicorn.error`. Other handlers of those loggers still receive them, and their other records are unchanged.
- `annotate(**fields)`: adds fields to the outcome line of the current request or django-q task. A later value for the same name wins, a unit keeps at most 64 names (later new names are dropped), and names and values follow the field rule: library-owned or malformed names, the identity fields `account_id`, `user_id` and `agent_run_id` (bind those with `log_context`), the log-call keywords `exc_info`, `stack_info` and `stacklevel`, and unsupported values are dropped. An annotation never replaces the line's own fields. Under `configure(strict=True)` a malformed or library-owned name, or an unsupported value, raises `StrictModeViolation`; the identity fields and log-call keywords are still dropped silently. Outside an open request or task it does nothing, in every mode, including in a task or thread that outlives its request.
- Built-in exception fields on failure lines: `status_code` (from `exc.response`), `http_method` and `http_host` (from `exc.request`), `sqlstate`, `constraint`, `db_schema`, `db_table` and `db_column` (from `exc.diag`). Each is read behind its own guard, so one that raises omits only that field, strings are capped at 128 characters, and they never overwrite fields passed to the log call.
- Exceptions declare their own log fields: an `error_code` string attribute and a `__log_fields__()` method returning a mapping are rendered on the failure line. Values follow the same field rule as call-site fields, are redacted like any other field, and a method that raises contributes nothing.
- `collide_logging.testing.captured_logs()`: captures stdout (including the configured handler's stream) and stderr, with a live `text`; `assert_conforms()` checks every line, `assert_absent(sentinel)` checks the text for a value that must not be logged.
- `configure(strict=True)` for test settings: log calls raise `StrictModeViolation` for a prose event name, positional arguments, a `message` or `task` field, or a reserved context field (`RESERVED_CONTEXT_FIELDS`) whose value differs from the bound one. `first_party` names the stdlib logger prefixes that are checked and `strict_exempt` the prefixes that are skipped.
  - **Exception logs need operation context.** A call that carries an exception (`log.exception(...)`, a truthy `exc_info`, or a first-party stdlib record with `exc_info`) raises `StrictModeViolation` unless it has a field that describes the operation: a call-site field, a field bound by an active `log_context` block, or a field the exception declares or captured from a `log_context` block it left. Library-owned names, reserved context fields (`account_id`, `user_id`, ...), `exc_info`, `stack_info`, `stacklevel` and the built-in exception attributes do not count; neither do the `task` and `task_id` a django-q task binds.
  - **`annotate()` with a malformed or library-owned name, or an unsupported value, raises `StrictModeViolation`.**
- `collide_logging.Logger`, the typed logging contract (event names are `LiteralString`); `get_logger()` returns it.
- `run_in_context()`: wraps a callable so it runs with the logging context (such as `request_id`) captured when it was wrapped, for threads that do not inherit `contextvars`.
- `client_request_id` on request lines when the inbound `X-Request-ID` is well-formed.
- `trace_id` and `span_id` on lines logged inside an active OpenTelemetry span. OpenTelemetry stays optional; the library only reads the current span.
- `collide_logging.django_q.connect()` (new `[django-q]` extra, django-q2 1.10 or later): binds a fresh `worker_run_id`, the `task_id` and `task` (the task's function, as a dotted path) for each django-q task, and clears the context and any `log_context` block left open at the start and end of each task in the worker. Each non-sync task gets one `task.finished` outcome line, written from the worker that ran it, with `outcome`, `duration_ms`, `task_name`, the task's annotations and its bound context; it never carries the task result. A failed task logs at `error` with `outcome="error"`; to add its `error_type` and exception fields, enable the library's error reporter with `Q_CLUSTER["error_reporter"] = {"collide_logging": {}}`. The first `connect()` call logs one `collide_logging.django_q.reporter_not_configured` warning when that reporter is not enabled. django-q's own logger is narrowed to its cluster lifecycle errors, each kept as a `django_q.*` event (`process_reincarnated`, `cluster_start_failed`, `schedule_task_failed`, `schedule_failed`, `pusher_failed`, `result_save_failed`, `broker_unreachable`, `cpu_affinity_unavailable`) with django-q's text in `detail`; records are matched by call site, not text, and every other django-q record, including the per-task lines that carry task results and the return-hook errors, is dropped. `connect()` removes every handler on the `django-q` logger, including django-q's default stderr handler, and turns propagation on, so these events go through the library's handler. The `django-q` logger's level is WARNING, or the root level when that is higher; a later `configure(level=...)` does not re-apply it.
- `collide_logging.testing.unit_of_work()`: runs a block as one unit of work and yields its `annotations` (a copy) and `exception`, so a test of code that calls `annotate()` can assert on what it added.

### Changed

- With uvicorn's `--workers` above 1 or `--reload`, the supervisor process never imports the app, so its few startup and shutdown lines stay plain text.
- Non-string mapping keys (UUID, bytes, tuple, datetime, Decimal) are rendered as strings instead of dropping the log line; secret-key matching uses the rendered string.
- Redaction walks nested dicts, lists, tuples and sets (to depth 32) without mutating the caller's objects, and scrubs inline `Bearer <token>` values in any string. Values that cannot be walked, or nest too deeply, render as `***REDACTION_ERROR***`. Self-referencing containers and values past a per-event budget of 10,000 visited values also render as `***REDACTION_ERROR***`.
- UUID values render as canonical strings in JSON output.
- Supports Python 3.11.

### Fixed

- A logging failure no longer raises into the caller (except `StrictModeViolation` under `configure(strict=True)`) or prints the raw record to stderr; it emits one `logging.error` line carrying only the exception type.

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
