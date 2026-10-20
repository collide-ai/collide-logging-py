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


result: int = collide_logging.run_in_context(lambda: 1)()


@collide_logging.log_context(step="a")
def _decorated(value: int) -> int:
    return value


@collide_logging.log_context(step="a")
async def _decorated_async(value: int) -> int:
    return value


decorated_result: int = _decorated(1)


async def _use_context() -> None:
    with collide_logging.log_context(step="a"):
        pass
    async with collide_logging.log_context(step="b"):
        pass
    awaited: int = await _decorated_async(1)
    assert awaited


collide_logging.annotate(rows=1, ratio=0.5, ok=True, account_id=None)
