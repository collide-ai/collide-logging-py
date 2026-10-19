from __future__ import annotations

import logging

import collide_logging
from collide_logging import Logger

log: Logger = collide_logging.get_logger("x")


def takes(logger: Logger) -> None:
    logger.error("e.f")


def interpolated(name: str) -> None:
    takes(logging.getLogger("x"))  # expect-error
    log.info(f"user {name}")  # expect-error
    log.info(name)  # expect-error
    log.info("x %s", name)  # expect-error
