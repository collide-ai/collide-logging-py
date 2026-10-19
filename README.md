# collide-logging

Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md).

Drop-in structured logging for Collide services: JSON output, secret redaction, correlation IDs, and thin adapters for Django / FastAPI. One `configure()` call sets everything up.

## Why this exists

Once one service has structured logging right, every other service needs to do the same thing the same way. Maintaining 20+ copies of the same 70-line structlog setup is a recipe for drift. This package is the canonical implementation: declare `logging.standard: collide/v1` in your `collide.yaml` and depend on `collide-logging`. It also bridges foreign stdlib loggers (Django's `django.request`, gunicorn, third-party libraries) through the same JSON processor chain, so every line on stdout is valid collide/v1 JSON — not just lines that originate from a `CollideLogger`.

## Install

This package is internal-only — install from this repo by tag, not from PyPI.

### Using `uv add`

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
uv add "collide-logging[django] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
uv add "collide-logging[fastapi] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
```

### In `pyproject.toml`

The uv-idiomatic form: declare the dep by name and pin the git source separately. Lets you keep the dep entry clean and reference the same source in multiple places.

```toml
[project]
dependencies = [
    "collide-logging[django]",   # or [fastapi], or omit the extra for core
]

[tool.uv.sources]
collide-logging = { git = "https://github.com/collide-ai/collide-logging-py.git", tag = "v0.3.0" }
```

Inline PEP 508 form (works without `[tool.uv.sources]`):

```toml
[project]
dependencies = [
    "collide-logging[django] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0",
]
```

Pin to a tag (`@v0.3.0`) rather than `main` so an upstream change does not silently re-resolve your service.

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

Redaction matches field *names* (at any nesting depth). The one value-level scrub is for inline `Bearer <token>` strings. Sensitive free text under an innocuous field name is otherwise logged verbatim, so keep such values out of logs at the call site.

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

The middleware always generates a 16-hex `request_id`, binds it to all logs emitted during the request, and returns it in the `X-Request-ID` response header. A well-formed inbound `X-Request-ID` is never adopted; it is logged as `client_request_id`. It also emits an `http.request` line with method, path, status, `duration_ms`, and `user_id` (the authenticated user's primary key as a string; omitted when anonymous).

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

## Declaring conformance

In your service's `collide.yaml`:

```yaml
logging:
  standard: collide/v1
```

The registry's `structured_logging_configured` check passes once `collide-logging` is declared as a dependency and `configure()` is called somewhere in the codebase.

## Testing your output

Pipe captured records through `assert_collide_v1` to catch drift before it reaches production:

```python
from structlog.testing import capture_logs
from collide_logging.testing import assert_collide_v1

def test_logs_conform():
    with capture_logs() as records:
        my_service.do_thing()

    for record in records:
        assert_collide_v1(record)
```

The helper raises `AssertionError` with a specific message on missing fields, malformed timestamps, invalid levels, or unredacted secrets.

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
