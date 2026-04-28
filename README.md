# collide-logging

Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md).

Drop-in structured logging for Collide services: JSON output, secret redaction, correlation IDs, and thin adapters for Django / FastAPI / Flask. One `configure()` call sets everything up.

> **Status:** Scaffolding. See open [issues](https://github.com/collide-ai/collide-logging-py/issues) for the implementation backlog.

## Install (planned)

```bash
uv add "collide-logging[django]"
uv add "collide-logging[fastapi]"
uv add "collide-logging[flask]"
uv add collide-logging          # core only (no framework adapter)
```

## Quickstart (planned)

```python
import collide_logging

collide_logging.configure(service="my-service")
logger = collide_logging.get_logger(__name__)

logger.info("startup.complete", port=8000)
```

For frameworks, install the appropriate middleware/hook (see issues for exact API).

## Why this exists

Once one service has structured logging right, every other service needs to do the same thing the same way. Maintaining 20+ copies of the same 70-line structlog setup is a recipe for drift. This package is the canonical implementation; declaring `logging.standard: collide/v1` in your `collide.yaml` and depending on `collide-logging` is how you opt in.

## Spec

See [`docs/logging-spec.md`](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md) in the registry repo for the wire-level contract: required fields, event naming, correlation IDs, redaction list, log levels.
