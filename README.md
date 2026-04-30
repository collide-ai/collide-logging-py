# collide-logging

Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md).

Drop-in structured logging for Collide services: JSON output, secret redaction, correlation IDs, and thin adapters for Django / FastAPI / Flask. One `configure()` call sets everything up.

## Install

```bash
uv add collide-logging              # core only
uv add "collide-logging[django]"    # + Django RequestLoggingMiddleware
uv add "collide-logging[fastapi]"   # + Starlette/FastAPI ASGI middleware
uv add "collide-logging[flask]"     # + Flask request hooks
```

## Plain Python

```python
import collide_logging

collide_logging.configure(service="my-service")
logger = collide_logging.get_logger(__name__)

logger.info("startup.complete", port=8000)
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

## Spec

See [`docs/logging-spec.md`](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md) in the registry repo for the wire-level contract: required fields, event naming, correlation IDs, redaction list, log levels.
