# collide-logging

Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md).

Drop-in structured logging for Collide services: JSON output, secret redaction, correlation IDs, and thin adapters for Django / FastAPI. One `configure()` call sets everything up.

## Why this exists

Once one service has structured logging right, every other service needs to do the same thing the same way. Maintaining 20+ copies of the same 70-line structlog setup is a recipe for drift. This package is the canonical implementation: declare `logging.standard: collide/v1` in your `collide.yaml` and depend on `collide-logging`. It also bridges foreign stdlib loggers (Django's `django.request`, gunicorn, third-party libraries) through the same JSON processor chain, so every line on stdout is valid collide/v1 JSON — not just lines that originate from a `CollideLogger`.

## Install

This package is internal-only — install from this repo by tag, not from PyPI.

### Using `uv add`

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.6.0"
uv add "collide-logging[django] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.6.0"
uv add "collide-logging[fastapi] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.6.0"
uv add "collide-logging[django-q] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.6.0"
```

### In `pyproject.toml`

The uv-idiomatic form: declare the dep by name and pin the git source separately. Lets you keep the dep entry clean and reference the same source in multiple places.

```toml
[project]
dependencies = [
    "collide-logging[django]",   # or [fastapi], [django-q], or omit the extra for core
]

[tool.uv.sources]
collide-logging = { git = "https://github.com/collide-ai/collide-logging-py.git", tag = "v0.6.0" }
```

Inline PEP 508 form (works without `[tool.uv.sources]`):

```toml
[project]
dependencies = [
    "collide-logging[django] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.6.0",
]
```

Pin to a tag (`@v0.6.0`) rather than `main` so an upstream change does not silently re-resolve your service.

## Plain Python

```python
import collide_logging

collide_logging.configure(service="my-service")
logger = collide_logging.get_logger(__name__)

logger.info("startup.complete", port=8000)
```

`service` should match the `slug` in your service's `collide.yaml`. It is emitted on every log line so dashboards can group by service.

The line above produces one JSON object on stdout (formatted here for readability):

```json
{
  "timestamp": "2026-04-30T17:43:54.692241Z",
  "level": "info",
  "service": "my-service",
  "logger": "__main__",
  "event": "startup.complete",
  "port": 8000
}
```

### Secret redaction

Sensitive field names are redacted automatically, before the line leaves the process:

```python
logger.info("auth.attempt", api_key="hunter2", github_token="ghp_xxx")
```

```json
{
  "timestamp": "...",
  "level": "info",
  "service": "my-service",
  "logger": "__main__",
  "event": "auth.attempt",
  "api_key": "***REDACTED***",
  "github_token": "***REDACTED***"
}
```

The default redact list covers `api_key`, `authorization`, `client_secret`, `cookie`, `password`, `secret`, `secret_key`, `prompt`, `user_prompt`, `user_query`, `query`, `question`, `completion`, `ai_response`, `messages`, `text_delta`, `tool_input`, `tool_output`, `request_payload`, `response_body`, `email`, `user_email`, `recipient`, `request`, plus suffix matches for `*_token`, `*_api_token`, and `*_signing_secret`. Pass `extra_redact_keys=[...]` to `configure()` to extend it.

Redaction matches field *names* (at any nesting depth), never values. The one value-level scrub is for inline `Bearer <token>` strings. Free text under an innocuous field name, such as `note` or `detail`, is logged verbatim, so mask or omit it at the call site before logging it. Values that cannot be walked, or nest deeper than 32 levels, render as `***REDACTION_ERROR***`.

Only the handler that `configure()` installs gets these guarantees. A root handler added by something else formats stdlib records itself, so for those records the collide/v1 fields and redaction do not apply to its output.

### Exceptions

`log.exception(...)` and `exc_info=True` never log the exception message or notes, which can carry arbitrary data. A line carries `exception_type`, traceback frames (file, line and function, without source lines), the type name of every exception in the `__cause__` / `__context__` chain (and of each `ExceptionGroup` member), and the safe attributes `status_code`, `sqlstate` and `constraint` when the exception has them.

### Trace ids

Inside an active OpenTelemetry span, lines carry `trace_id` (32 hex characters) and `span_id` (16). OpenTelemetry stays optional: the library only reads the current span and never creates or exports one.

### Typed logger

`get_logger()` returns `collide_logging.Logger`, whose event names are `LiteralString`, so a type checker rejects prose and interpolated event names. Put variable data in keyword fields. An event that does not match the dot-notation rule is emitted as `event="log.message"` with the original text in `message`.

A field cannot be named `event`: `log.info("x.y", event=...)` raises `TypeError`, because `event` is the event-name parameter. Name it something else, such as `webhook_event`.

## Django

`settings.py`:

```python
import collide_logging

collide_logging.configure(service="my-service")

MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "collide_logging.django.RequestLoggingMiddleware",
    # ...
]
```

The middleware always generates a 16-hex `request_id`, binds it to all logs emitted during the request, and returns it in the `X-Request-ID` response header. An inbound `X-Request-ID` is never adopted as `request_id`; a well-formed one (`[A-Za-z0-9._-]{1,64}`) is logged as `client_request_id` and a malformed one is dropped. It also emits an `http.request` line with method, path, status, `duration_ms`, and `user_id` (the authenticated user's primary key as a string; omitted when anonymous).

It is sync- and async-capable: under ASGI with native-async views it runs as a coroutine and does not force a sync/async adaptation boundary. For `StreamingHttpResponse`, the `http.request` line is emitted when the body finishes streaming, so `duration_ms` reflects time to stream close. If the handler raises, a status-500 line with the traceback is emitted before the exception propagates.

## FastAPI / Starlette

```python
from fastapi import FastAPI
import collide_logging
from collide_logging.starlette import RequestLoggingMiddleware

collide_logging.configure(service="my-service")

app = FastAPI()
app.add_middleware(RequestLoggingMiddleware)
```

It follows the same request-id rules as the Django middleware and emits one `http.request` line per request, including a status-500 line with the traceback when the app raises. It does not log a user.

## Workers / one-shot scripts

```python
from collide_logging import bind_worker_run_id, get_logger

logger = get_logger(__name__)

with bind_worker_run_id(worker="github_collector") as run_id:
    logger.info("worker.tick.started")  # carries worker_run_id
    do_work()
    logger.info("worker.tick.completed")
```

The decorator form `@with_worker_run_id` wraps a function so each call gets a fresh `worker_run_id`.

Threads started by `ThreadPoolExecutor` or `threading.Timer` do not inherit `contextvars`. Wrap the callable with `run_in_context()` at the point of submission, inside the request or task, so its lines keep `request_id` and any other bound fields:

```python
from collide_logging import run_in_context

executor.submit(run_in_context(send_receipt), order_id)
```

Wrapping at import time captures an empty context.

### django-q

Install the `[django-q]` extra and call `connect()` from `AppConfig.ready()`:

```python
from collide_logging import django_q

django_q.connect()
```

Each task then runs with a fresh `worker_run_id` plus `task_id` and `task` (the task's function as a dotted path), and django-q's own prose logger is silenced. A failed task logs `task.failed` without the task result. `task.failed` is emitted from django-q's monitor process, not the worker that ran the task. Tasks run in sync mode are skipped by the receivers: no `worker_run_id` is bound and no `task.failed` is logged. The receivers never raise: a failure inside one logs a single `logging.error` line instead of taking down the worker.

## Starting uvicorn

Under the `uvicorn` CLI, `configure()` takes over uvicorn's `uvicorn` and `uvicorn.error` loggers and turns its access log off (the middleware's `http.request` line replaces it), so no extra flags are needed.

Starting uvicorn programmatically needs `log_config=None`, after `configure()`:

```python
import uvicorn
import collide_logging

collide_logging.configure(service="my-service")
uvicorn.run(app, log_config=None, access_log=False)
```

`configure()` followed by uvicorn running with its default `log_config` is unsupported: uvicorn applies its own logging config at startup and silently restores plain-text output.

With `--workers` above 1 or `--reload`, uvicorn's supervisor process never imports the app, so `configure()` never runs there and its few startup and shutdown lines stay plain text. Pass a `--log-config` file to format them.

## Declaring conformance

In your service's `collide.yaml`:

```yaml
logging:
  standard: collide/v1
```

The registry's `structured_logging_configured` check passes once `collide-logging` is declared as a dependency and `configure()` is called somewhere in the codebase.

## Testing your output

Call `configure()` first, then capture the output and check it before drift reaches production:

```python
from collide_logging.testing import captured_logs

def test_logs_conform():
    with captured_logs() as captured:
        my_service.do_thing()

    captured.assert_conforms()
    captured.assert_absent("super-secret-value")
```

`captured_logs()` captures stdout (including the configured handler) and stderr, and `captured.text` is live, so assertions inside the block see the output so far. `assert_conforms()` fails on any non-JSON line on either stream. `captured.records` holds the parsed events, and `assert_collide_v1(record)` checks a single one. The helpers raise `AssertionError` with a specific message on missing fields, malformed timestamps, invalid levels, or unredacted secrets.

### Strict mode

In test settings, `strict=True` turns call-site mistakes into `StrictModeViolation` (an `AssertionError`):

```python
collide_logging.configure(
    service="my-service",
    strict=True,
    first_party=("my_service", "my_app"),
    strict_exempt=("my_service.legacy",),
)
```

A log call raises when it has:

- an event name that is not dot-notation (prose, hyphens, camelCase, a non-string);
- positional arguments (`log.info("x %s", value)`);
- a `message` or `task` field;
- a reserved context field (`RESERVED_CONTEXT_FIELDS`: `request_id`, `worker_run_id`, `agent_run_id`, `client_request_id`, `trace_id`, `span_id`, `account_id`, `user_id`) whose value differs from the bound one.

Calls through `get_logger()` are always checked. Stdlib loggers are checked only when their name starts with a `first_party` prefix. A logger whose name starts with a `strict_exempt` prefix is skipped. `first_party` and `strict_exempt` take a tuple or list of prefixes; a bare `str` raises `TypeError`.

Strict mode is set per `configure()` call, so a later `configure()` without `strict=True` turns it off. The rules may tighten in minor releases; pin the dependency by tag. Leave strict mode off in production.

## Development

```bash
uv sync --extra django --extra fastapi --extra django-q --group dev
uv run pytest
uv run ruff check src/ tests/
uv run mypy src/
```

The framework extras are required to run the full test suite. Without them, `test_django.py` / `test_starlette.py` / `test_django_q.py` skip silently via `pytest.importorskip`.

## Spec

See [`docs/logging-spec.md`](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md) in the registry repo for the wire-level contract: required fields, event naming, correlation IDs, redaction list, log levels.
