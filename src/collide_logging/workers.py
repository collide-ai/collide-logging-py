"""Worker / background-job correlation helpers.

A worker tick (or any one-shot background job) should bind ``worker_run_id``
at its entrypoint so every log line emitted during the tick can be filtered
back to that single invocation. This module provides a context manager and a
decorator equivalent.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import structlog


def _generate_run_id() -> str:
    return uuid.uuid4().hex[:8]


@contextmanager
def bind_worker_run_id(
    run_id: str | None = None,
    **extra: Any,
) -> Iterator[str]:
    """Bind ``worker_run_id`` (and optional extras) to all logs in the block.

    Args:
        run_id: Explicit ID to bind. If ``None``, an 8-hex-char ID is
            generated.
        **extra: Additional fields to bind for the duration of the block
            (e.g. ``worker="github_collector"``).

    Yields:
        The bound ``run_id``.

    Outer bindings shadowed by the new values are restored on exit, including
    when the block raises.
    """
    if run_id is None:
        run_id = _generate_run_id()

    bindings: dict[str, Any] = {"worker_run_id": run_id, **extra}
    tokens = structlog.contextvars.bind_contextvars(**bindings)
    try:
        yield run_id
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def with_worker_run_id[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    """Decorator: bind a fresh ``worker_run_id`` for the duration of each call."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        with bind_worker_run_id():
            return fn(*args, **kwargs)

    return wrapper
