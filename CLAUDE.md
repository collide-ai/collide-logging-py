# collide-logging-py — Coding Agent Handoff

You are picking this repo up cold. This file gives you the context you need to pick up work and ship without asking the human a bunch of questions.

## Status

**Latest: v0.4.1 (validate-mode fail-safe: unrecognized `COLLIDE_LOG_VALIDATE` resolves to lenient). v0.4.0 added events API safety: best-effort lenient mode, `.event()` level/exc_info, public `digest_value()`.** Internal-only — not on PyPI. Services install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.4.1"
```

The original ordered v0.1.0 backlog (issues #1–#10) is closed. New work is ad-hoc — no implied ordering across open issues.

## What this repo is

The canonical Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md). Other Collide services depend on this package and call `collide_logging.configure()` to get structured logging that conforms to the spec. As of v0.2.0, the package also provides a validated events API (`EventSchema`, `register_event_schema()`, `CollideLogger.event()`) for adapter authors who need to emit structured, schema-checked events.

## Reference implementation (historical)

A working reference originally lived in `collide-ai/soc2-software-registry` (`src/collide/logging.py`, `src/collide/middleware.py`, `src/collide/workers/management/commands/run_worker.py`, `src/collide/settings/base.py`). That code is **historical** — as of v0.1.0, this package IS the canonical implementation. Services should depend on `collide-logging`, not copy from the registry.

A few intentional differences from the registry impl:

- **Token-based contextvar reset** (not bulk `clear_contextvars`) so outer bindings survive nested blocks — relevant for workers that bind a request-scoped ID inside a worker tick.
- **Suffix-based redaction** for `*_token` / `*_api_token` / `*_signing_secret` — services no longer have to enumerate every secret-shaped field name.
- **Caller-supplied service slug** rather than hard-coded `"collide"`.

The spec itself still lives at `soc2-software-registry/docs/logging-spec.md`. If you find a spec ambiguity, file an issue against that repo and pause.

## Events API (v0.2.0)

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

## Conventions

- **Python 3.12+.** Use `from __future__ import annotations`.
- **`uv` everything.** Never `pip` directly. Never activate a venv.
  - Install: `uv sync --extra django --extra fastapi --extra flask --group dev`
  - Without all three extras, `test_django.py` / `test_starlette.py` / `test_flask.py` skip silently via `pytest.importorskip` — your green local run won't actually exercise the adapters.
  - Gates: `uv run ruff check src/ tests/`, `uv run mypy src/`, `uv run pytest`.
- **Strict typing.** mypy strict on `src/`, relaxed on `tests/`. Type every public function.
- **Type stubs over silence.** When mypy complains about missing stubs for a framework dep, install `<package>-stubs` (or `types-<package>`) as a dev dependency rather than reaching for `[[tool.mypy.overrides]] ignore_missing_imports = true`. Silencing the diagnostic also turns off type checking at the boundary.
- **Tests use pytest.** No factory-boy here (no Django ORM). Use plain dataclasses or fixtures.
- **No Django, FastAPI, or Flask in the core package.** Framework adapters live in submodules (`collide_logging.django`, `.starlette`, `.flask`) and depend on the framework via optional `[django]`, `[fastapi]`, `[flask]` extras. Importing the core (`collide_logging`) must work in any Python project, no framework installed.
- **Public API is what's documented.** Underscore-prefix anything internal.
- **No emojis in code, comments, or docs.** No comments unless the *why* is non-obvious.
- **Branches: `<num>-<slug>`. Commit messages reference the issue number. PR body must contain `Closes #<num>` / `Fixes #<num>` / `Resolves #<num>`** — the `check-issue-link` CI requires those exact verbs (`Refs #N` is not enough). The check only re-fires on push, not on `gh pr edit`, so fix the body before merging the first commit or push an empty commit.
- **CHANGELOG entry per user-visible change.** Bump `version` in `pyproject.toml` and `__version__` in `src/collide_logging/__init__.py` per semver, add a `CHANGELOG.md` entry, then tag and create a GitHub release.

## How to pick up work

1. `gh issue list` — pick an open, unblocked issue.
2. Cut a branch `<num>-<slug>`.
3. Implement; run `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest`.
4. Open a PR with `Closes #<num>` in the body.

## What "done" looks like for the package as a whole

A service installs `collide-logging[django]`, calls `collide_logging.configure(service="my-svc")`, and gets log output that passes the registry's `structured_logging_configured` check unchanged. No regex scanning of source — the check just verifies the dep is declared and `configure()` is called somewhere.

This is achieved as of v0.1.0; "done" now means keeping it that way. As of v0.2.0, "done" also means that adapter authors can call `register_event_schema()` + `logger.event()` and get schema-validated, redactable events through the same processor chain.

## What you do NOT need to do

- Do not implement runtime log forwarding, transport, or aggregation. The spec is the contract; how logs get to Loki/CloudWatch/etc. is the operator's problem.
- Do not add OpenTelemetry, sentry-sdk, or other tracing. Out of scope.
- Do not implement non-Python language packages. Those go in sibling repos (`collide-logging-go`, etc.) when they exist.
- Do not modify the spec itself — that lives in `soc2-software-registry/docs/logging-spec.md`. If you find a spec ambiguity, file an issue against the registry repo and pause.

## Questions worth asking the human

If the spec is genuinely ambiguous on something blocking — e.g. "the redaction list says `*_token` but does that include `request_token` (which is sometimes a public ID)?" — ask. Otherwise, default to the conservative interpretation and proceed.
