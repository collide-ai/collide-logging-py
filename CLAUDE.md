# collide-logging-py — Coding Agent Handoff

You are picking this repo up cold. This file gives you the context you need to pick up work and ship without asking the human a bunch of questions.

## Status

**Latest: v0.2.0 (shipped 2026-05-11); v0.1.0 shipped 2026-04-30.** Internal-only — not on PyPI. Services install via tag:

```bash
uv add "git+https://github.com/collide-ai/collide-logging-py.git@v0.2.0"
```

The original ordered v0.1.0 backlog (issues #1–#10) is closed. New work is ad-hoc — no implied ordering across open issues.

## What this repo is

The canonical Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md). Other Collide services depend on this package and call `collide_logging.configure()` to get structured logging that conforms to the spec.

## Reference implementation (historical)

A working reference originally lived in `collide-ai/soc2-software-registry` (`src/collide/logging.py`, `src/collide/middleware.py`, `src/collide/workers/management/commands/run_worker.py`, `src/collide/settings/base.py`). That code is **historical** — as of v0.1.0, this package IS the canonical implementation. Services should depend on `collide-logging`, not copy from the registry.

A few intentional differences from the registry impl:

- **Token-based contextvar reset** (not bulk `clear_contextvars`) so outer bindings survive nested blocks — relevant for workers that bind a request-scoped ID inside a worker tick.
- **Suffix-based redaction** for `*_token` / `*_api_token` / `*_signing_secret` — services no longer have to enumerate every secret-shaped field name.
- **Caller-supplied service slug** rather than hard-coded `"collide"`.

The spec itself still lives at `soc2-software-registry/docs/logging-spec.md`. If you find a spec ambiguity, file an issue against that repo and pause.

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

This is achieved as of v0.1.0; "done" now means keeping it that way.

## What you do NOT need to do

- Do not implement runtime log forwarding, transport, or aggregation. The spec is the contract; how logs get to Loki/CloudWatch/etc. is the operator's problem.
- Do not add OpenTelemetry, sentry-sdk, or other tracing. Out of scope.
- Do not implement non-Python language packages. Those go in sibling repos (`collide-logging-go`, etc.) when they exist.
- Do not modify the spec itself — that lives in `soc2-software-registry/docs/logging-spec.md`. If you find a spec ambiguity, file an issue against the registry repo and pause.

## Questions worth asking the human

If the spec is genuinely ambiguous on something blocking — e.g. "the redaction list says `*_token` but does that include `request_token` (which is sometimes a public ID)?" — ask. Otherwise, default to the conservative interpretation and proceed.
