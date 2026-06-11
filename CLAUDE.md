# collide-logging-py — Coding Agent Handoff

You are picking this repo up cold. This file holds the facts you need to ship without asking a bunch of questions. Deeper, situational detail lives where it loads on demand:

- **Events API reference** → `.claude/rules/events-api.md` (loads when you edit events code or an adapter).
- **Request-logging middleware contract** → `.claude/rules/middleware-contract.md` (loads when you edit `django.py` / `starlette.py`).
- **Cutting a release** → run the `/release` skill (`.claude/skills/release/`).

## Status

**Latest: v0.5.1.** Internal-only — not on PyPI. Services install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.5.1"
```

See `CHANGELOG.md` for the per-version history. The original ordered v0.1.0 backlog (issues #1–#10) is closed. New work is ad-hoc — no implied ordering across open issues.

## What this repo is

The canonical Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md). Other Collide services depend on this package and call `collide_logging.configure()` to get structured logging that conforms to the spec. As of v0.2.0 it also provides a validated events API (`EventSchema`, `register_event_schema()`, `CollideLogger.event()`) for adapter authors emitting structured, schema-checked events.

The spec itself lives at `soc2-software-registry/docs/logging-spec.md`. A working reference implementation originally lived in that repo too (`src/collide/logging.py`, `middleware.py`, etc.), but as of v0.1.0 **this package is canonical** — services depend on `collide-logging`, they do not copy from the registry. Do not modify the spec here; if you find a spec ambiguity, file an issue against the registry repo and pause.

A few intentional differences from that historical registry impl (non-obvious, do not "fix" them):

- **Token-based contextvar reset** (not bulk `clear_contextvars`) so outer bindings survive nested blocks — relevant for workers that bind a request-scoped ID inside a worker tick.
- **Suffix-based redaction** for `*_token` / `*_api_token` / `*_signing_secret` — services no longer enumerate every secret-shaped field name.
- **Caller-supplied service slug** rather than hard-coded `"collide"`.

## Conventions

- **Python 3.12+.** Use `from __future__ import annotations`.
- **`uv` everything.** Never `pip` directly. Never activate a venv.
  - Install: `uv sync --extra django --extra fastapi --extra flask --group dev`
  - Without all three extras, `test_django.py` / `test_starlette.py` / `test_flask.py` skip silently via `pytest.importorskip` — your green local run won't actually exercise the adapters.
  - Gates: `uv run ruff check src/ tests/`, `uv run mypy src/`, `uv run pytest`.
- **Strict typing.** mypy strict on `src/`, relaxed on `tests/`. Type every public function.
- **Type stubs over silence.** When mypy complains about missing stubs for a framework dep, install `<package>-stubs` (or `types-<package>`) as a dev dependency rather than `ignore_missing_imports = true`. Silencing the diagnostic turns off type checking at the boundary.
- **Tests use pytest.** No factory-boy here (no Django ORM). Use plain dataclasses or fixtures.
- **No Django, FastAPI, or Flask in the core package.** Framework adapters live in submodules (`collide_logging.django`, `.starlette`, `.flask`) behind optional `[django]`, `[fastapi]`, `[flask]` extras. Importing the core (`collide_logging`) must work with no framework installed.
- **Public API is what's documented.** Underscore-prefix anything internal.
- **No emojis in code, comments, or docs.** No comments unless the *why* is non-obvious.
- **Branches: `<num>-<slug>`. Commit messages reference the issue number. PR body must contain `Closes #<num>` / `Fixes #<num>` / `Resolves #<num>`** — the `check-issue-link` CI requires those exact verbs (`Refs #N` is not enough). The check only re-fires on push, not on `gh pr edit`, so fix the body before the first push or push an empty commit.
- **CHANGELOG entry per user-visible change.** Bump `version` in `pyproject.toml` and `__version__` in `src/collide_logging/__init__.py` per semver, add a `CHANGELOG.md` entry — all in the feature PR. The release itself is the `/release` skill.

## How to pick up work

1. `gh issue list` — pick an open, unblocked issue.
2. Cut a branch `<num>-<slug>`.
3. Implement; run `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest`.
4. Open a PR with `Closes #<num>` in the body.

## What "done" looks like

A service installs `collide-logging[django]`, calls `collide_logging.configure(service="my-svc")`, and gets log output that passes the registry's `structured_logging_configured` check unchanged. Achieved as of v0.1.0; "done" now means keeping it that way. As of v0.2.0 it also means adapter authors can call `register_event_schema()` + `logger.event()` and get schema-validated, redactable events through the same processor chain.

## What you do NOT need to do

- Do not implement runtime log forwarding, transport, or aggregation. How logs reach Loki/CloudWatch/etc. is the operator's problem.
- Do not add OpenTelemetry, sentry-sdk, or other tracing. Out of scope.
- Do not implement non-Python language packages — those go in sibling repos (`collide-logging-go`, etc.).
- Do not modify the spec (it lives in `soc2-software-registry`). File an issue there and pause if it's ambiguous.

## Questions worth asking the human

If the spec is genuinely ambiguous on something blocking — e.g. "the redaction list says `*_token` but does that include `request_token` (sometimes a public ID)?" — ask. Otherwise default to the conservative interpretation and proceed.
