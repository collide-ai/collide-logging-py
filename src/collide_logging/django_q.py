"""django-q task binding: a fresh ``worker_run_id``, ``task_id`` and ``task`` per task.

Call :func:`connect` from ``AppConfig.ready()``. Requires the ``[django-q]`` extra.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

import structlog
from django_q.signals import post_execute, pre_execute
from django_q.utils import get_func_repr

import collide_logging
from collide_logging._ids import generate_id

_PRE_UID = "collide_logging.django_q.pre_execute"
_POST_UID = "collide_logging.django_q.post_execute"

_log = collide_logging.get_logger("collide_logging.django_q")


def _never_raises(receiver: Callable[..., None]) -> Callable[..., None]:
    # A raising pre_execute receiver kills the django-q worker process.
    @functools.wraps(receiver)
    def guarded(**kwargs: Any) -> None:
        try:
            receiver(**kwargs)
        except Exception as exc:
            _log.error("logging.error", error_type=type(exc).__name__)

    return guarded


@_never_raises
def _on_pre_execute(*, task: dict[str, Any], **_: Any) -> None:
    if task.get("sync"):
        return
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        worker_run_id=generate_id(), task=get_func_repr(task.get("func")), task_id=task.get("id")
    )


@_never_raises
def _on_post_execute(*, task: dict[str, Any], **_: Any) -> None:
    if task.get("sync"):
        return
    try:
        if not task.get("success"):
            with structlog.contextvars.bound_contextvars(
                task=get_func_repr(task.get("func")), task_id=task.get("id")
            ):
                _log.error("task.failed", task_name=task.get("name"))
    finally:
        structlog.contextvars.clear_contextvars()


def connect() -> None:
    """Connect the task receivers and silence django-q's own prose logger.

    Safe to call more than once.
    """
    pre_execute.connect(_on_pre_execute, dispatch_uid=_PRE_UID, weak=False)
    post_execute.connect(_on_post_execute, dispatch_uid=_POST_UID, weak=False)
    logging.getLogger("django-q").setLevel(logging.CRITICAL)
