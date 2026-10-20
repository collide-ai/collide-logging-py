"""django-q error reporter, importable without Django or django-q.

django-q loads error reporters from the ``djangoq.errorreporters`` entry point while
``django.setup()`` populates the app registry, before models can be imported. So this module,
which the entry point names, imports neither; ``collide_logging.django_q`` re-exports the class.
"""

from __future__ import annotations

import contextlib
import contextvars
import sys

from collide_logging._unit import Unit, current_unit, record_exception

# The unit pre_execute opened for the task the worker is running. A worker process runs one
# task at a time, so a single slot is enough.
task_unit: contextvars.ContextVar[tuple[Unit, contextvars.Token[Unit | None]] | None] = (
    contextvars.ContextVar("collide_django_q_task_unit", default=None)
)


def worker_task_unit() -> Unit | None:
    """The unit of the worker task being run, when it is the current unit."""
    opened = task_unit.get()
    if opened is None or current_unit() is not opened[0]:
        return None
    return opened[0]


class ErrorReporter:
    """django-q error reporter that records a failed task's exception for its outcome line.

    django-q calls ``report()`` in the worker while the task's exception is being handled.
    Registered as the ``collide_logging`` entry point in the ``djangoq.errorreporters`` group.
    """

    def __init__(self, **_: object) -> None:
        pass

    def report(self) -> None:
        # Only the worker task's own unit: a sync task's exception propagates to its caller,
        # which decides whether its own unit failed.
        with contextlib.suppress(Exception):
            if worker_task_unit() is not None:
                record_exception(sys.exc_info()[1])
