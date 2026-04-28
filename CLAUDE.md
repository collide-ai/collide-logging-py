# collide-logging-py — Coding Agent Handoff

You are picking this repo up cold. This file gives you the context you need to pick an issue off the backlog and ship it without asking the human a bunch of questions.

## What this repo is

The canonical Python implementation of the [`collide/v1` logging spec](https://github.com/collide-ai/soc2-software-registry/blob/main/docs/logging-spec.md). Other Collide services depend on this package and call `collide_logging.configure()` to get structured logging that conforms to the spec.

## Reference implementation

A working reference already exists in the registry repo. **Read these files first** — they are the source material you'll be extracting and generalizing:

- `collide-ai/soc2-software-registry:src/collide/logging.py` — processor chain, redaction, `get_logger()`. ~70 lines.
- `collide-ai/soc2-software-registry:src/collide/middleware.py` — the Django `RequestLoggingMiddleware` that binds `request_id`. (You'll port this verbatim for the Django adapter.)
- `collide-ai/soc2-software-registry:src/collide/workers/management/commands/run_worker.py` — shows how `worker_run_id` is bound at the worker entrypoint.
- `collide-ai/soc2-software-registry:src/collide/settings/base.py` — the `LOGGING` dict and `structlog.configure()` call that wires the processors together.
- `collide-ai/soc2-software-registry:docs/logging-spec.md` — the contract this package exists to implement. Required fields, event naming, redaction list.
- `collide-ai/soc2-software-registry:docs/logging.md` — internal usage convention. Useful as quickstart material when you write this package's README.

The existing code is **Collide-specific**: it hard-codes `service="collide"` and the redact list lives at module load time. Your job when extracting is to **generalize** without losing the conformance properties.

## Conventions

- **Python 3.12+.** Use `from __future__ import annotations`.
- **`uv` everything.** Never `pip` directly. Never activate a venv. `uv sync --group dev` to install, `uv run pytest`, `uv run ruff check`, `uv run mypy src/`.
- **Strict typing.** mypy strict on `src/`, relaxed on `tests/`. Type every public function.
- **Tests use pytest.** No factory-boy here (no Django ORM). Use plain dataclasses or fixtures.
- **No Django, FastAPI, or Flask in the core package.** Framework adapters live in submodules (`collide_logging.django`, `.fastapi`, `.flask`) and depend on the framework via optional `[django]`, `[fastapi]`, `[flask]` extras. Importing the core (`collide_logging`) must work in any Python project, no framework installed.
- **Public API is what's documented.** Underscore-prefix anything internal.
- **No emojis in code, comments, or docs.** No comments unless the *why* is non-obvious.
- **Commit messages reference the issue number.** Branch `<num>-<slug>`. PR body contains `Closes #<num>`. Same SOC 2 change-mgmt rules as the registry repo.

## How to pick up work

1. `gh issue list` — pick the lowest-numbered open, unblocked issue.
2. The issues are ordered. Don't skip ahead unless an earlier issue is blocked on something external.
3. Each issue is self-contained: scope, files to touch, references, and acceptance criteria.
4. Cut a branch, implement, run `uv run ruff check && uv run mypy src/ && uv run pytest`, open a PR, mention `Closes #<num>`.

## What "done" looks like for the package as a whole

A service installs `collide-logging[django]`, calls `collide_logging.configure(service="my-svc")`, and gets log output that passes the registry's `structured_logging_configured` check unchanged. No regex scanning of source — the check just verifies the dep is declared and `configure()` is called somewhere.

## What you do NOT need to do

- Do not implement runtime log forwarding, transport, or aggregation. The spec is the contract; how logs get to Loki/CloudWatch/etc. is the operator's problem.
- Do not add OpenTelemetry, sentry-sdk, or other tracing. Out of scope.
- Do not implement non-Python language packages. Those go in sibling repos (`collide-logging-go`, etc.) when they exist.
- Do not modify the spec itself — that lives in `soc2-software-registry/docs/logging-spec.md`. If you find a spec ambiguity, file an issue against the registry repo and pause.

## Questions worth asking the human

If the spec is genuinely ambiguous on something blocking — e.g. "the redaction list says `*_token` but does that include `request_token` (which is sometimes a public ID)?" — ask. Otherwise, default to the conservative interpretation and proceed.
