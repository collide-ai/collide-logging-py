"""Log a failure at the level its cause earns: ERROR only when a dependency was unreachable.

Every third-party type is optional: a library that is not installed contributes nothing. None is
imported with the package; ``configure()`` resolves them, or else the first check does.
"""

from __future__ import annotations

import importlib
import logging
import socket
from typing import LiteralString

from collide_logging.contract import Logger

# Builtin TimeoutError is not here: our own budgets (asyncio.timeout, resume timeouts) raise it.
# Client libraries report their connect and read timeouts through the transport errors below.
_UNREACHABLE_ERRORS: tuple[tuple[str, str], ...] = (
    ("azure.core.exceptions", "ServiceRequestError"),
    ("azure.core.exceptions", "ServiceResponseError"),
    ("httpx", "TransportError"),
    ("aiohttp", "ClientConnectionError"),
    ("asyncpg.exceptions", "PostgresConnectionError"),
    ("asyncpg.exceptions", "CannotConnectNowError"),
    ("asyncpg.exceptions", "TooManyConnectionsError"),
    ("psycopg", "OperationalError"),
)
# Subclasses of the above that a reachable dependency, or our own code, raised.
_NOT_AN_OUTAGE: tuple[tuple[str, str], ...] = (
    ("psycopg.errors", "QueryCanceled"),
    ("psycopg.errors", "LockNotAvailable"),
    ("psycopg.errors", "ProgramLimitExceeded"),
    ("psycopg.errors", "ObjectInUse"),
    ("psycopg_pool", "PoolTimeout"),
    ("httpx", "UnsupportedProtocol"),
    ("httpx", "LocalProtocolError"),
)
_MAX_CHAIN_DEPTH = 8


_OutageTypes = tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]

_resolved: _OutageTypes | None = None
_warned_modules: set[str] = set()
_log = logging.getLogger(__name__)


def _importable_types(
    names: tuple[tuple[str, str], ...],
) -> tuple[tuple[type[BaseException], ...], bool]:
    """The named exception types that exist, and whether every module gave a definite answer."""
    found: list[type[BaseException]] = []
    definite = True
    for module_name, type_name in names:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        # A broken install must not break the line that asks; it is retried on the next call.
        except Exception as error:
            definite = False
            if module_name not in _warned_modules:
                _warned_modules.add(module_name)
                _log.warning(
                    "collide_logging.outage.import_failed",
                    extra={"outage_module": module_name, "error_type": type(error).__name__},
                )
            continue
        candidate = getattr(module, type_name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found.append(candidate)
    return tuple(found), definite


def _outage_types() -> _OutageTypes:
    """Resolve the outage types; ``configure()`` calls this so a request never pays the imports."""
    global _resolved
    if _resolved is not None:
        return _resolved
    builtin: tuple[type[BaseException], ...] = (ConnectionError, socket.gaierror)
    unreachable, unreachable_definite = _importable_types(_UNREACHABLE_ERRORS)
    not_an_outage, not_an_outage_definite = _importable_types(_NOT_AN_OUTAGE)
    types = (builtin + unreachable, not_an_outage)
    if unreachable_definite and not_an_outage_definite:
        _resolved = types
    return types


def is_outage(error: BaseException) -> bool:
    """Whether ``error``, or an error it was raised from, says a dependency could not be reached.

    Follows ``__cause__``, then ``__context__`` unless it was suppressed (``raise ... from None``),
    for at most eight exceptions.
    """
    unreachable, not_an_outage = _outage_types()
    current: BaseException | None = error
    for _ in range(_MAX_CHAIN_DEPTH):
        if current is None:
            return False
        if isinstance(current, unreachable) and not isinstance(current, not_an_outage):
            return True
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return False


def log_failure_by_cause(
    log: Logger,
    event: LiteralString,
    *,
    error: BaseException,
    **fields: object,
) -> None:
    """Log ``error`` with its traceback: ERROR for an outage, WARNING for anything else.

    Pass an operation field (or call inside a ``log_context`` that binds one), as for any
    exception line. ``exc_info`` is the instance, so this also works outside an ``except`` block.
    """
    emit = log.error if is_outage(error) else log.warning
    emit(event, exc_info=error, **fields)
