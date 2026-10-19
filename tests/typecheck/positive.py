from __future__ import annotations

import collide_logging
from collide_logging import Logger

log: Logger = collide_logging.get_logger("x")
log.info("a.b", n=1)
log.bind(k=1).warning("c.d")


def takes(logger: Logger) -> None:
    logger.error("e.f")


takes(collide_logging.get_logger("x"))


def _conforms(c: collide_logging.CollideLogger) -> Logger:
    return c
