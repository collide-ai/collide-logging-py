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

# django-q formats (and may translate) each message before logging it, so records are matched
# by call site, not text: (module, function, whether the record carries an exception) ->
# (minimum level, event name). A call site missing here is dropped, including one a later
# django-q version adds; a new message inside an allowlisted function is caught by
# test_allowlist_matches_installed_django_q, which pins every django-q call site.
_LIFECYCLE_EVENTS: dict[tuple[str, str, bool], tuple[int, str]] = {
    ("cluster", "start", False): (logging.ERROR, "django_q.cluster_start_failed"),
    ("cluster", "reincarnate", False): (logging.CRITICAL, "django_q.process_reincarnated"),
    ("cluster", "set_cpu_affinity", False): (logging.WARNING, "django_q.cpu_affinity_unavailable"),
    ("pusher", "pusher", True): (logging.ERROR, "django_q.pusher_failed"),
    ("monitor", "save_task", True): (logging.ERROR, "django_q.result_save_failed"),
    ("monitor", "save_cached", True): (logging.ERROR, "django_q.result_save_failed"),
    ("scheduler", "scheduler", False): (logging.ERROR, "django_q.schedule_task_failed"),
    ("scheduler", "scheduler", True): (logging.ERROR, "django_q.schedule_failed"),
    ("redis_broker", "ping", False): (logging.ERROR, "django_q.broker_unreachable"),
}


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


def _keep_lifecycle_record(record: logging.LogRecord) -> bool:
    """Rewrite an allowlisted django-q record into its event, keeping its text as ``detail``."""
    site = (record.module, record.funcName, bool(record.exc_info))
    lifecycle = _LIFECYCLE_EVENTS.get(site)
    if lifecycle is None:
        return False
    min_level, event = lifecycle
    if record.levelno < min_level:
        return False
    record.detail = record.getMessage()
    record.msg = event
    record.args = ()
    return True


def _reporter_configured() -> bool:
    try:
        reporter = django_q_conf.error_reporter
        if reporter is None:
            return False
        return any(isinstance(target, ErrorReporter) for target in reporter.targets)
    except Exception:
        return False


def connect() -> None:
    """Connect the task receivers and turn django-q's lifecycle errors into events.

    django-q's lifecycle errors (a process reincarnated, the cluster failing to start,
    schedule, broker and result-save failures) become ``django_q.*`` events with django-q's
    text in ``detail``; every other django-q record is dropped. ``connect()`` removes every
    handler on the ``django-q`` logger, including django-q's default stderr handler, and turns
    propagation on, so these events go through the library's handler. The logger's level is
    WARNING, or the root level when that is higher; a later ``configure(level=...)`` does not
    re-apply it.

    The first call logs one ``collide_logging.django_q.reporter_not_configured`` warning when
    django-q's error reporters do not include the library's, since failed tasks then log
    without their exception. Safe to call more than once.
    """
    pre_execute.connect(_on_pre_execute, dispatch_uid=_PRE_UID, weak=False)
    post_execute_in_worker.connect(
        _on_post_execute_in_worker, dispatch_uid=_POST_IN_WORKER_UID, weak=False
    )
    django_q_logger = logging.getLogger("django-q")
    django_q_logger.handlers = []
    django_q_logger.propagate = True
    django_q_logger.setLevel(max(logging.WARNING, logging.getLogger().level))
    if _keep_lifecycle_record in django_q_logger.filters:
        return
    django_q_logger.addFilter(_keep_lifecycle_record)
    if not _reporter_configured():
        _log.warning("collide_logging.django_q.reporter_not_configured")
