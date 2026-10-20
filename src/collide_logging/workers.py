"""Worker / background-job correlation helpers.

A worker tick (or any one-shot background job) should bind ``worker_run_id``
at its entrypoint so every log line emitted during the tick can be filtered
back to that single invocation. This module provides a context manager and a
decorator equivalent, plus :func:`run_in_context` for carrying the caller's
logging context into a thread that does not inherit ``contextvars``.
"""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

import structlog

from collide_logging._ids import generate_id

P = ParamSpec("P")
T = TypeVar("T")


@contextmanager
def bind_worker_run_id(
    run_id: str | None = None,
    **extra: Any,
) -> Iterator[str]:
    """Bind ``worker_run_id`` (and optional extras) to all logs in the block.

    Args:
        run_id: Explicit ID to bind. If ``None``, a 16-hex-char ID is
            generated.
        **extra: Additional fields to bind for the duration of the block
            (e.g. ``worker="github_collector"``).

    Yields:
        The bound ``run_id``.

    Outer bindings shadowed by the new values are restored on exit, including
    when the block raises.
    """
    if run_id is None:
        run_id = generate_id()

    bindings: dict[str, Any] = {"worker_run_id": run_id, **extra}
    tokens = structlog.contextvars.bind_contextvars(**bindings)
    try:
        yield run_id
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def with_worker_run_id(fn: Callable[P, T]) -> Callable[P, T]:
    """Decorator: bind a fresh ``worker_run_id`` for the duration of each call."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        with bind_worker_run_id():
            return fn(*args, **kwargs)

    return wrapper


def run_in_context(fn: Callable[P, T]) -> Callable[P, T]:
    """Wrap ``fn`` so it runs with the logging context captured at wrap time.

    Threads started by ``ThreadPoolExecutor`` or ``threading.Timer`` do not
    inherit ``contextvars``; wrap the callable before handing it over so its
    log lines keep ``request_id`` and any other bound fields. Wrap at the
    point of submission (inside the request or task): wrapping at import time
    captures an empty context.
    """
    captured = contextvars.copy_context()

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        # A Context cannot be entered twice at once, so each call runs in its own copy.
        return captured.copy().run(fn, *args, **kwargs)

    return wrapper
