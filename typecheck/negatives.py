from __future__ import annotations

import logging
from decimal import Decimal

from django.http import HttpRequest

import collide_logging
from collide_logging import Logger
from collide_logging.django import RequestLoggingMiddleware

log: Logger = collide_logging.get_logger("x")


def takes(logger: Logger) -> None:
    logger.error("e.f")


def interpolated(name: str) -> None:
    takes(logging.getLogger("x"))  # expect-error
    # mypy does not enforce LiteralString, so only pyright and ty reject these two.
    log.info(f"user {name}")  # noqa: G004  # deliberate misuse the typing test must reject  # expect-error[pyright,ty]
    log.info(name)  # expect-error[pyright,ty]
    log.info("x %s", name)  # expect-error


def unsupported_context_value() -> None:
    collide_logging.log_context(amount=Decimal("1.5"))  # expect-error


def _not_a_view(request: HttpRequest) -> int:
    return 1


def middleware_around_a_non_view() -> None:
    RequestLoggingMiddleware(_not_a_view)  # expect-error
