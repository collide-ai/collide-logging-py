---
paths:
  - "src/collide_logging/events.py"
  - "src/collide_logging/__init__.py"
  - "src/collide_logging/django.py"
  - "src/collide_logging/starlette.py"
  - "src/collide_logging/flask.py"
  - "src/collide_logging/workers.py"
  - "tests/test_events.py"
---

# Events API (v0.2.0)

Adapter authors emit structured events through the validated events API rather than raw log calls. The full public surface lives in `collide_logging` (no submodule needed):

- **`EventSchema(name, fields, description="")`** — declares one named event type. `name` is a dotted string (e.g. `"hermes.skill.invoke"`); `fields` maps field names to `FieldSpec`.
- **`FieldSpec(type, required=False, redact=False)`** — declares one field. `type` is documentary (not enforced at runtime). `required=True` means the field must be supplied on every call. `redact=True` replaces the value with a digest object `{"len": …, "sha256": "…"}` before emission.
- **`register_event_schema(schema)`** — registers a schema in the module-global registry. Idempotent on identical re-registration; raises `ValueError` on name collision with a different shape. Call this at import time in your adapter module.
- **`list_schemas()`** — returns all registered schemas sorted by name. Useful for introspection and test assertions.
- **`EventValidationError`** — raised on schema violations in strict mode (see below).
- **`CollideLogger.event(name, *, level="info", exc_info=False, **fields)`** — emits a validated event. `CollideLogger` is returned by `get_logger()`; do not construct it directly. `level` selects the structlog method (`debug`/`info`/`warning`/`error`/`critical`); `exc_info` is threaded to the underlying call when truthy so error-path events carry a traceback (use `level="error", exc_info=True`). Both default to current behavior (INFO, no exc_info) — a bare `event(name, **fields)` record is unchanged.
- **`digest_value(value)`** — returns the `{"len": …, "sha256": "…"}` digest used by `FieldSpec(redact=True)`, exposed for hand-curated redaction of sensitive free-text on the plain `log.info(...)` path (auto-redaction is name-based only and never inspects values).

**Adapter pattern:** call `register_event_schema()` once at module import, then emit via `logger.event(name, **fields)` wherever the event occurs.

**Validation mode** is controlled by the `COLLIDE_LOG_VALIDATE` environment variable:
- Unset or `"raise"` (dev default): unknown event names, missing required fields, and unknown field keys raise `EventValidationError`.
- `"lenient"` (prod): the event is emitted **best-effort** under its real name (unknown fields dropped, known fields still redacted, a `_schema_violation` field added recording `violation`/`missing`/`unknown`), so the payload survives. A `collide_logging.schema_violation` meta-event is emitted alongside it as an alertable signal — alert on `event="collide_logging.schema_violation"`. The process never crashes. (Pre-v0.4.0 this dropped the offending event entirely — issue #36.) Any set-but-unrecognized value (e.g. a typo) resolves to `lenient` rather than `raise`, so a misconfigured prod var can't start crashing the host; a one-time `collide_logging.invalid_validate_mode` warning surfaces the bad value (issue #37, v0.4.1).

**Redaction layering:** `FieldSpec(redact=True)` field-level redaction and the global suffix-based redaction (`*_token`, `*_api_token`, `*_signing_secret`) operate independently — both can fire on the same record. `digest_value()` produces the same digest as `FieldSpec(redact=True)`.
