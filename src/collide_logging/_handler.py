"""Stream handler that reports a failed log call as one ``logging.error`` line."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from collide_logging._processors import _logging_error_event


class CollideStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """``StreamHandler`` whose failure path never raises and never echoes the record.

    The stdlib default prints the raw message and arguments to stderr; this writes a single
    JSON line with the exception class name only.
    """

    def __init__(self, stream: Any, service: str) -> None:
        super().__init__(stream)
        self._service = service

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        try:
            exc_type = sys.exc_info()[0]
            line = _logging_error_event(
                exc_type.__qualname__ if exc_type else "UnknownError",
                str(record.name),
                self._service,
            )
            self.stream.write(json.dumps(line) + self.terminator)
            self.flush()
        except Exception:
            # Last resort: the stream itself is unusable, so there is nowhere left to report.
            pass
