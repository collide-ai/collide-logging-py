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

### Trace ids

Inside an active OpenTelemetry span, lines carry `trace_id` (32 hex characters) and `span_id` (16). OpenTelemetry stays optional: the library only reads the current span and never creates or exports one.

### Typed logger

`get_logger()` returns `collide_logging.Logger`, whose event names are `LiteralString`, so a type checker rejects prose and interpolated event names. Put variable data in keyword fields. An event that does not match the dot-notation rule is emitted as `event="log.message"` with the original text in `message`.

A field cannot be named `event`: `log.info("x.y", event=...)` raises `TypeError`, because `event` is the event-name parameter. Name it something else, such as `webhook_event`.

## Logging failures

A failure line answers three questions: what failed, where, and what the code was doing. The library carries the first two; `log_context` supplies the third.

### What a failure line carries

`log.exception(...)` and `exc_info=True` render:

- `exception_type`, the exception's qualified class name (`builtins.ValueError`), and the traceback frames (file, line and function, without source lines);
- the type name of every exception in the `__cause__` / `__context__` chain, and of each `ExceptionGroup` member;
- database fields when the exception has them: `sqlstate`, `constraint`, `db_schema`, `db_table`, `db_column` (psycopg-style `diag`);
- HTTP fields when it has them: `status_code` (from `exc.response`), `http_method` and `http_host` (from `exc.request`, as httpx errors do);
- the fields the exception declares (see below) and the context captured from `log_context` blocks it left.

Never rendered: exception messages and notes, a URL's path or query, and any other database detail (such as a row's values). Strings are capped at 128 characters, and each attribute is read behind its own guard, so one that raises omits only that field.

### Bind what the code is doing

`log_context(**fields)` binds fields to every log line in a block, and records them on an exception that leaves the block, so the failure line carries them even when it is logged elsewhere:

```python
from collide_logging import log_context

with log_context(operation="invoice.sync", invoice_id=invoice.id):
    sync(invoice)

@log_context(operation="invoice.sync")
def sync_invoice(invoice): ...

@log_context(operation="invoice.sync")
async def sync_invoice_async(invoice): ...

async with log_context(operation="invoice.sync"):
    await sync(invoice)
```

- Field names are checked when the block is built. A library-owned name (including `task`, `task_id`, `error_code` and anything starting with `exception`) raises `TypeError`, as do `exc_info`, `stack_info` and `stacklevel`. A malformed name (not `[a-z][a-z0-9_]*`) or more than 20 fields raises `ValueError`.
- Values must be a str, an int within 64 bits, a finite float, a bool, `None` or a UUID. Anything else is dropped and the rest of the block still binds; under `configure(strict=True)` it raises `StrictModeViolation`.
- With nested blocks the innermost value wins for a name set more than once.
- A generator or async-generator function cannot be decorated (`TypeError`), because the decorator would bind nothing while the generator runs. Use the block inside the generator.
- Blocks unwind safely: an exit also resets any block entered after it that is still open, so out-of-order exits, or generators finalized later or in another context, leave nothing bound.

### Log a failure once, at the boundary

Log a failure where it is handled: the request or task boundary, or the place that recovers from it. Do not log and re-raise at every layer; each layer would write another line for the same failure. Inside a request or task the outcome line (below) already carries the failure, so let the exception propagate.

Where you do log it yourself, bind the context around the work and pass the exception:

```python
try:
    with log_context(operation="report.render", report_id=report_id):
        render(report_id)
except RenderError:
    logger.exception("report.render_failed")  # carries operation and report_id
```

### Outcome lines

Each request (Django and Starlette middleware) and each django-q task is one unit of work that writes one outcome line, `http.request` or `task.finished`, with:

- `outcome`: `ok`, or `error` for an unhandled exception (or a request status of 500 or more, or a failed task);
- `duration_ms`;
- the fields added with `annotate()`;
- on failure, `error_type` (the bare class name, `ValueError`) plus `exception_type` and the other exception fields above, and the `log_context` fields captured where it was raised.

An error outcome logs at `error`.

`annotate(**fields)` adds fields to the current unit's outcome line:

```python
from collide_logging import annotate

annotate(rows_imported=len(rows), source="csv")
```

A later value for a name wins; a unit keeps at most 64 names. Names and values follow the same rule as `log_context`, except that outside strict mode bad names and values are dropped rather than raised, and the identity fields `account_id`, `user_id` and `agent_run_id` (bind those with `log_context`) and the log-call keywords are dropped too. An annotation never replaces the line's own fields (`method`, `status`, `duration_ms`, ...). Under strict mode a malformed or library-owned name, or an unsupported value, raises `StrictModeViolation`. Outside an open request or task `annotate()` does nothing, in every mode: that includes a task or thread started from a request that runs on after the response. For a Django `StreamingHttpResponse` the fields are taken when the view returns, so an `annotate()` inside the streaming body does nothing.

Django converts a view's exception into a 500 response before middleware sees it, so the middleware records it from the `got_request_exception` signal. Django's own `django.request` line for that exception, and uvicorn's `Exception in ASGI application` line on `uvicorn.error` for an exception the Starlette middleware re-raises, are then redundant. Both are dropped from the library's handler only; other handlers of those loggers (such as `mail_admins`) still receive them, and their other records pass.

### Exceptions describe themselves

An application exception can declare fields for its own failure line: an `error_code` string attribute and a `__log_fields__()` method returning a mapping. Values follow the `log_context` rule and are redacted like any other field. A method that raises contributes nothing. Translate third-party errors into your own at the integration boundary, so the declared fields are the ones you chose and the original stays in the `__cause__` chain:

```python
class StorageError(Exception):
    error_code = "storage_unavailable"

    def __init__(self, operation: str, bucket: str) -> None:
        super().__init__(operation)
        self.operation = operation
        self.bucket = bucket

    def __log_fields__(self) -> dict[str, str]:
        return {"storage_operation": self.operation, "storage_bucket": self.bucket}


def put_blob(key: str, data: bytes) -> None:
    try:
        client.put_object(Bucket="reports", Key=key, Body=data)
    except ClientError as error:
        raise StorageError("put", "reports") from error
```

The failure line then carries `error_code`, `storage_operation` and `storage_bucket`, plus the type name of `ClientError` from the chain.

### Precedence on a failure line

For a name that appears more than once, the first of these wins:

1. fields passed at the log call, and the context bound where the call is made;
2. fields the exception declares (`error_code`, `__log_fields__()`);
3. built-in exception attributes (database and HTTP fields);
4. context captured from `log_context` blocks the exception left.

Within the exception chain the outermost exception wins.

### Known limits

- The declared fields, attributes and captured context of `ExceptionGroup` members are not read; only their type names are rendered, and only the `__cause__` / `__context__` chain supplies fields.
- A Django `StreamingHttpResponse` whose body fails mid-stream logs `outcome="ok"` with the status the view returned, and `annotate()` inside a streamed body does nothing.
- An exception instance raised again in another request or task records that unit's context in place of the earlier one, and carries none of the earlier unit's context when no block surrounds it there. Context recorded outside a request or task, such as in a plain thread, merges, including into the first request or task the exception reaches.
- django-q needs django-q2 1.10 or later, and its failed-task lines carry `error_type` and the exception fields only with `Q_CLUSTER["error_reporter"] = {"collide_logging": {}}` (see django-q below).

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

It is sync- and async-capable: under ASGI with native-async views it runs as a coroutine and does not force a sync/async adaptation boundary. For `StreamingHttpResponse`, the `http.request` line is emitted when the body finishes streaming, so `duration_ms` reflects time to stream close. If the handler raises, a status-500 line with the traceback is emitted before the exception propagates. See [Logging failures](#logging-failures) for `outcome` and `annotate()`.

## FastAPI / Starlette

```python
from fastapi import FastAPI
import collide_logging
from collide_logging.starlette import RequestLoggingMiddleware

collide_logging.configure(service="my-service")

app = FastAPI()
app.add_middleware(RequestLoggingMiddleware)
```

It follows the same request-id rules as the Django middleware and emits one `http.request` line per request, including a status-500 line with the traceback when the app raises. It carries the same `outcome` and `annotate()` fields as Django. It does not log a user.

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

Each task then runs with a fresh `worker_run_id` plus `task_id` and `task` (the task's function as a dotted path). Each task is a unit of work: `annotate()` works inside it, and the worker that ran it writes one `task.finished` line with `outcome`, `duration_ms`, `task_name` and the annotations, never the task result. A failed task logs at `error` with `outcome="error"`.

django-q's own logger is narrowed to its cluster lifecycle errors, the only signal for a worker killed mid-task, which writes no `task.finished`. Each arrives as one event, with django-q's original text (process, cluster, schedule and task names, never a task result or exception message) in `detail` and any exception rendered as its type:

| Event | Level | When |
| --- | --- | --- |
| `django_q.process_reincarnated` | critical | the sentinel replaced a worker, monitor or pusher after its death or a task timeout; `detail` says which |
| `django_q.cluster_start_failed` | error | the cluster's sentinel exited during startup |
| `django_q.schedule_task_failed` | error | a schedule did not produce a task |
| `django_q.schedule_failed` | error | the scheduler raised while creating tasks from schedules |
| `django_q.pusher_failed` | error | the pusher could not pull from the broker, or could not unpack a task it pulled |
| `django_q.result_save_failed` | error | the monitor could not save a task result |
| `django_q.broker_unreachable` | error | the Redis broker did not answer a ping |
| `django_q.cpu_affinity_unavailable` | warning | `cpu_affinity` is set but cannot be applied |

Everything else from django-q's logger is dropped, including its per-task `Processed` and `Failed` lines and its return-hook errors; a task's outcome is its `task.finished` line. `connect()` removes every handler on the `django-q` logger, including django-q's default stderr handler, and turns propagation on, so these events go through the library's handler. The `django-q` logger's level is WARNING, or the root level when that is higher; a later `configure(level=...)` does not re-apply it.

To add `error_type` and the exception fields to a failed task's line, enable the library's error reporter in settings (django-q2 1.10 or later):

```python
Q_CLUSTER = {
    # ...
    "error_reporter": {"collide_logging": {}},
}
```

Without it a failed task still logs `outcome="error"`, without the exception, and the first `connect()` call logs one `collide_logging.django_q.reporter_not_configured` warning. The reporter lives in a module that imports neither Django nor django-q, because django-q loads it while `django.setup()` is still populating the app registry; `collide_logging.django_q.ErrorReporter` re-exports it. Tasks run in sync mode are skipped by the receivers: no `worker_run_id` is bound and no `task.finished` is logged. The receivers never raise: a failure inside one logs a single `logging.error` line instead of taking down the worker.

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

Code that calls `annotate()` outside a request or task, such as a task or service function under test, annotates nothing. Run it inside `unit_of_work()` to assert on what it added:

```python
from collide_logging.testing import unit_of_work

def test_import_annotates_row_count():
    with unit_of_work() as work:
        import_rows(rows)

    assert work.annotations == {"rows_imported": 3}
```

`work.annotations` is a copy of the unit's annotations and `work.exception` its recorded exception; an exception that escapes the block is recorded and propagates. Both keep their final values after the block. No outcome line is written.

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
- a reserved context field (`RESERVED_CONTEXT_FIELDS`: `request_id`, `worker_run_id`, `agent_run_id`, `client_request_id`, `trace_id`, `span_id`, `account_id`, `user_id`) whose value differs from the bound one;
- an exception (`log.exception(...)`, a truthy `exc_info`) and no field describing the operation. A call-site field, a field bound by an active `log_context` block, or a field the exception declares or captured from a block it left counts. Attributes read from the exception (`status_code`, `sqlstate`, ...) do not count; a field you pass yourself does. Neither do library-owned names, reserved context fields, `exc_info`, `stack_info`, `stacklevel`, a field bound with `structlog.contextvars.bind_contextvars()` rather than `log_context`, or the `task` and `task_id` a django-q task binds.

Strict mode also raises `StrictModeViolation` for a `log_context` value that would be dropped, for entering a `log_context` block that sets a reserved context field to a value other than the bound one, and for a malformed or library-owned `annotate()` name or an unsupported `annotate()` value (the identity fields and log-call keywords are still dropped silently). `annotate()` outside a request or task does nothing, as in production.

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
