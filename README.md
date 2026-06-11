# collide-logging

Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md).

Drop-in structured logging for Collide services: JSON output, secret redaction, correlation IDs, and thin adapters for Django / FastAPI / Flask. One `configure()` call sets everything up.

## Why this exists

Once one service has structured logging right, every other service needs to do the same thing the same way. Maintaining 20+ copies of the same 70-line structlog setup is a recipe for drift. This package is the canonical implementation: declare `logging.standard: collide/v1` in your `collide.yaml` and depend on `collide-logging`. It also bridges foreign stdlib loggers (Django's `django.request`, gunicorn, third-party libraries) through the same JSON processor chain, so every line on stdout is valid collide/v1 JSON — not just lines that originate from a `CollideLogger`.

## Install

This package is internal-only — install from this repo by tag, not from PyPI.

### Using `uv add`

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
uv add "collide-logging[django] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
uv add "collide-logging[fastapi] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
uv add "collide-logging[flask] @ git+https://github.com/collide-ai/collide-logging-py.git@v0.3.0"
```

### In `pyproject.toml`

The uv-idiomatic form: declare the dep by name and pin the git source separately. Lets you keep the dep entry clean and reference the same source in multiple places.

```toml
[project]
dependencies = [
    "collide-logging[django]",   # or [fastapi], [flask], or omit the extra for core
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

The default redact list covers `api_key`, `authorization`, `client_secret`, `cookie`, `password`, `secret`, `secret_key`, plus suffix matches for `*_token`, `*_api_token`, and `*_signing_secret`. Pass `extra_redact_keys=[...]` to `configure()` to extend it.

**Auto-redaction matches field *names* only — it never inspects values.** Sensitive free-text under an innocuous field name (a `query`, an email `subject`, a chat `transcript`) serializes verbatim. For those, redact at the call site with `digest_value()`, which emits the same `{"len": ..., "sha256": "<8 hex>"}` digest the events API uses:

```python
from collide_logging import digest_value

logger.info("search.completed", query_digest=digest_value(query), result_count=n)
# query_digest -> {"len": 42, "sha256": "a3f1c8d2"}
```

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

The middleware reads inbound `X-Request-ID`, generates one if absent, binds it to all logs emitted during the request, and echoes it back on the response.

## FastAPI / Starlette

```python
from fastapi import FastAPI
import collide_logging
from collide_logging.starlette import RequestLoggingMiddleware

collide_logging.configure(service="my-service")

app = FastAPI()
app.add_middleware(RequestLoggingMiddleware)
```

## Flask

```python
from flask import Flask
import collide_logging
from collide_logging.flask import init_app

collide_logging.configure(service="my-service")

app = Flask(__name__)
init_app(app)
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

## Writing a third-party adapter

If you are wrapping an external framework (e.g. an agent runtime) for use across Collide services, declare the events you emit as schemas at adapter import time and emit them via `logger.event(name, **fields)`. The library validates the call and redacts flagged fields; you do not touch internal processors.

```python
import collide_logging

collide_logging.register_event_schema(
    collide_logging.EventSchema(
        name="my_adapter.skill.invoke",
        fields={
            "skill_id": collide_logging.FieldSpec(type=str, required=True),
            "input_payload": collide_logging.FieldSpec(type=str, redact=True),
        },
        description="One skill invocation by the agent.",
    )
)

logger = collide_logging.get_logger(__name__)
logger.event("my_adapter.skill.invoke", skill_id="search", input_payload=request)
```

`input_payload` is replaced with `{"len": ..., "sha256": "<8 hex>"}` before emission. Non-`str`/`bytes` values are `repr()`d first; expect a stable, opaque digest, not the raw value. The same digest is available directly as `digest_value()` (see [Secret redaction](#secret-redaction)) for hand-curated redaction on plain `logger.info(...)` calls.

Events default to INFO. For error-path events, pass `level=` (any of `debug`/`info`/`warning`/`error`/`critical`) and `exc_info=` to carry a traceback — schema validation and redaction are unchanged:

```python
logger.event(
    "my_adapter.upload.failed",
    upload_id=upload_id,
    level="warning",
    exc_info=True,
)
```

Validation behavior is controlled by `COLLIDE_LOG_VALIDATE`. Unset or `raise` (dev default): unknown event names, missing required fields, or unknown field keys raise `EventValidationError` — surfaces bugs in tests. `lenient` (prod): the event is emitted **best-effort** under its real name — unknown fields are dropped, known fields keep their redaction, and a `_schema_violation` field records what was wrong — so the payload survives an incident instead of vanishing. A `collide_logging.schema_violation` meta-event is emitted alongside it as an alertable signal (alert on `event="collide_logging.schema_violation"`). Never crashes the host process. Any set-but-unrecognized `COLLIDE_LOG_VALIDATE` value (a typo like `leniant`) falls back to `lenient` so a misconfiguration can't start raising in prod; a one-time `collide_logging.invalid_validate_mode` warning flags it.

Avoid declaring fields named `event`, `timestamp`, `level`, `service`, `logger`, or `_schema_violation` — those are owned by the processor chain.

## Development

```bash
uv sync --extra django --extra fastapi --extra flask --group dev
uv run pytest
uv run ruff check src/ tests/
uv run mypy src/
```

The framework extras are required to run the full test suite. Without them, `test_django.py` / `test_starlette.py` / `test_flask.py` skip silently via `pytest.importorskip`.

## Spec

See [`docs/logging-spec.md`](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md) in the registry repo for the wire-level contract: required fields, event naming, correlation IDs, redaction list, log levels.
