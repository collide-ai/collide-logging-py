# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-04-30

First public release. Implements the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md).

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

[0.1.0]: https://github.com/collide-ai/collide-logging-py/releases/tag/v0.1.0
