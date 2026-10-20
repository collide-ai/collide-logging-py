"""django-q task logging: a fresh ``worker_run_id``, ``task_id`` and ``task`` per task, and one
``task.finished`` outcome line per task, written from the worker that ran it.

Call :func:`connect` from ``AppConfig.ready()``. Requires the ``[django-q]`` extra (django-q2
1.10 or later). To put a failed task's exception on its ``task.finished`` line, enable the
library's error reporter (``connect()`` logs a warning when it is not enabled)::

    Q_CLUSTER = {..., "error_reporter": {"collide_logging": {}}}
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable

import structlog
from django_q import conf as django_q_conf
from django_q.signals import post_execute_in_worker, pre_execute
from django_q.utils import get_func_repr

import collide_logging
from collide_logging._context import reset_log_context
from collide_logging._django_q_reporter import ErrorReporter, task_unit, worker_task_unit
from collide_logging._ids import generate_id
from collide_logging._unit import close_unit, discard_exception, emit_outcome, open_unit

__all__ = ["ErrorReporter", "connect"]

_PRE_UID = "collide_logging.django_q.pre_execute"
_POST_IN_WORKER_UID = "collide_logging.django_q.post_execute_in_worker"

_log = collide_logging.get_logger("collide_logging.django_q")


def _never_raises(receiver: Callable[..., None]) -> Callable[..., None]:
    # A raising pre_execute receiver kills the django-q worker process.
    @functools.wraps(receiver)
    def guarded(**kwargs: object) -> None:
        try:
            receiver(**kwargs)
        except Exception as exc:
            _log.error("logging.error", error_type=type(exc).__name__)

    return guarded


@_never_raises
def _on_pre_execute(*, task: dict[str, object], **_: object) -> None:
    if task.get("sync"):
        return
    structlog.contextvars.clear_contextvars()
    reset_log_context()
    task_unit.set(open_unit())
    structlog.contextvars.bind_contextvars(
        worker_run_id=generate_id(), task=get_func_repr(task.get("func")), task_id=task.get("id")
    )


@_never_raises
def _on_post_execute_in_worker(*, task: dict[str, object], **_: object) -> None:
    # Runs under the worker's timer lock: write one line and return.
    if task.get("sync"):
        return
    opened = task_unit.get()
    try:
        unit = worker_task_unit()
        if unit is None:
            return
        succeeded = bool(task.get("success"))
        if succeeded:
            # The reporter also runs for a sync task the task ran and caught; that exception
            # is not this task's outcome. On a real failure report() runs last, so it holds
            # the task's own exception.
            discard_exception(unit)
        emit_outcome(
            _log,
            "task.finished",
            unit,
            failed=not succeeded,
            fields={"task_name": task.get("name")},
        )
    finally:
        structlog.contextvars.clear_contextvars()
        task_unit.set(None)
        if opened is not None:
            close_unit(*opened)


def _reporter_configured() -> bool:
    try:
        reporter = django_q_conf.error_reporter
        if reporter is None:
            return False
        return any(isinstance(target, ErrorReporter) for target in reporter.targets)
    except Exception:
        return False


def connect() -> None:
    """Connect the task receivers and silence django-q's own prose logger.

    Logs one ``collide_logging.django_q.reporter_not_configured`` warning when django-q's
    error reporters do not include the library's, since failed tasks then log without their
    exception. Safe to call more than once.
    """
    pre_execute.connect(_on_pre_execute, dispatch_uid=_PRE_UID, weak=False)
    post_execute_in_worker.connect(
        _on_post_execute_in_worker, dispatch_uid=_POST_IN_WORKER_UID, weak=False
    )
    logging.getLogger("django-q").setLevel(logging.CRITICAL)
    if not _reporter_configured():
        _log.warning("collide_logging.django_q.reporter_not_configured")
