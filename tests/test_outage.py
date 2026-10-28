"""ERROR only for a dependency that could not be reached; any other failure is a WARNING."""

from __future__ import annotations

import asyncio
import importlib
import logging
import pathlib
import socket
import sys

import aiohttp
import asyncpg
import httpx
import psycopg
import psycopg_pool
import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from structlog.testing import capture_logs

import collide_logging
from collide_logging import get_logger, is_outage, log_failure_by_cause
from collide_logging import outage as outage_module


def _wrapped(cause: BaseException) -> Exception:
    try:
        raise RuntimeError("wrapper") from cause
    except RuntimeError as error:
        return error


@pytest.mark.parametrize(
    "error",
    [
        ServiceRequestError("down"),
        ServiceResponseError("reset"),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        aiohttp.ClientConnectionError("refused"),
        asyncpg.exceptions.CannotConnectNowError("starting"),
        asyncpg.exceptions.TooManyConnectionsError("full"),
        asyncpg.exceptions.PostgresConnectionError("lost"),
        psycopg.OperationalError("refused"),
        ConnectionRefusedError("refused"),
        socket.gaierror("no such host"),
        _wrapped(ConnectionRefusedError("refused")),
        _wrapped(ServiceRequestError("down")),
    ],
)
def test_unreachable_dependency_is_an_outage(error: BaseException) -> None:
    assert is_outage(error)


@pytest.mark.parametrize(
    "error",
    [
        HttpResponseError("forbidden"),
        ValueError("bad"),
        KeyError("missing"),
        FileNotFoundError("local file"),
        psycopg.errors.QueryCanceled(),
        psycopg.errors.LockNotAvailable(),
        psycopg.errors.ProgramLimitExceeded(),
        psycopg.errors.ObjectInUse(),
        psycopg_pool.PoolTimeout("pool busy"),
        httpx.UnsupportedProtocol("ftp"),
        httpx.LocalProtocolError("bad request line"),
        asyncio.CancelledError(),
        TimeoutError(),
        _wrapped(ValueError("bad")),
        _wrapped(TimeoutError()),
    ],
)
def test_other_failures_are_not_an_outage(error: BaseException) -> None:
    assert not is_outage(error)


def _timeout_expiry() -> TimeoutError:
    async def expire() -> None:
        async with asyncio.timeout(0):
            await asyncio.sleep(1)

    try:
        asyncio.run(expire())
    except TimeoutError as error:
        return error
    raise AssertionError("asyncio.timeout did not expire")


def test_our_own_budget_expiring_is_not_an_outage() -> None:
    assert not is_outage(_timeout_expiry())


def test_a_cause_hidden_by_from_none_is_not_followed() -> None:
    try:
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError:
            raise ValueError("translated") from None
    except ValueError as error:
        assert not is_outage(error)


def test_an_implicit_context_is_followed() -> None:
    try:
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError:
            raise ValueError("while handling")  # noqa: B904
    except ValueError as error:
        assert is_outage(error)


def test_a_self_referential_chain_terminates() -> None:
    error = ValueError("loop")
    error.__context__ = error
    assert not is_outage(error)


def test_the_cause_walk_stops_at_its_depth_cap() -> None:
    error: BaseException = ConnectionRefusedError("deep")
    for _ in range(20):
        outer = RuntimeError("wrapper")
        outer.__cause__ = error
        error = outer
    assert not is_outage(error)


@pytest.mark.parametrize(
    ("error", "level"),
    [
        (ServiceRequestError("down"), "error"),
        (httpx.ConnectError("refused"), "error"),
        (HttpResponseError("forbidden"), "warning"),
        (ValueError("bad"), "warning"),
    ],
)
def test_log_failure_by_cause_picks_the_level_and_keeps_the_traceback(
    error: BaseException, level: str
) -> None:
    with capture_logs() as logs:
        log_failure_by_cause(
            get_logger("tests.outage"),
            "tests.outage.failed",
            error=error,
            operation="probe",
        )
    (entry,) = logs
    assert entry["log_level"] == level
    assert entry["event"] == "tests.outage.failed"
    assert entry["operation"] == "probe"
    assert entry["exc_info"] is error


def test_both_names_are_exported() -> None:
    assert {"is_outage", "log_failure_by_cause"} <= set(collide_logging.__all__)


@pytest.fixture
def unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outage_module, "_resolved", None)
    monkeypatch.setattr(outage_module, "_warned_modules", set())


def test_a_missing_optional_dependency_is_skipped(
    monkeypatch: pytest.MonkeyPatch, unresolved: None
) -> None:
    # A None entry in sys.modules makes importing that name raise ModuleNotFoundError.
    monkeypatch.setitem(sys.modules, "httpx", None)
    assert not is_outage(httpx.ConnectError("refused"))
    assert is_outage(ConnectionRefusedError("refused"))
    assert is_outage(ServiceRequestError("down"))
    assert not is_outage(psycopg.errors.QueryCanceled())
    assert outage_module._resolved is not None


def test_configure_resolves_the_types_at_startup(unresolved: None) -> None:
    collide_logging.configure(service="t", json=True)
    assert outage_module._resolved is not None
    unreachable, not_an_outage = outage_module._resolved
    assert httpx.TransportError in unreachable
    assert httpx.UnsupportedProtocol in not_an_outage


def test_a_module_that_fails_to_import_is_retried_and_reported_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    unresolved: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module_file = tmp_path / "collide_broken_client.py"
    module_file.write_text("raise RuntimeError('broken install')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        outage_module,
        "_UNREACHABLE_ERRORS",
        (*outage_module._UNREACHABLE_ERRORS, ("collide_broken_client", "Unreachable")),
    )

    with caplog.at_level(logging.WARNING, logger="collide_logging.outage"):
        assert is_outage(ConnectionRefusedError("refused"))
        assert is_outage(httpx.ConnectError("refused"))
        assert not is_outage(ValueError("bad"))
    assert outage_module._resolved is None
    warnings = [r for r in caplog.records if r.name == "collide_logging.outage"]
    assert [(r.levelno, r.getMessage()) for r in warnings] == [
        (logging.WARNING, "collide_logging.outage.import_failed")
    ]
    assert warnings[0].outage_module == "collide_broken_client"

    module_file.write_text("class Unreachable(Exception):\n    pass\n")
    importlib.invalidate_caches()
    fixed = importlib.import_module("collide_broken_client")
    assert is_outage(fixed.Unreachable())
    assert outage_module._resolved is not None


@pytest.mark.parametrize("name", ["NoSuchError", "AF_INET"])
def test_a_missing_or_non_class_name_is_skipped(
    monkeypatch: pytest.MonkeyPatch, unresolved: None, name: str
) -> None:
    monkeypatch.setattr(
        outage_module,
        "_UNREACHABLE_ERRORS",
        (*outage_module._UNREACHABLE_ERRORS, ("socket", name)),
    )
    assert is_outage(ConnectionRefusedError("refused"))
    assert not is_outage(ValueError("bad"))
    assert outage_module._resolved is not None


_OPTIONAL = ("azure", "httpx", "aiohttp", "asyncpg", "psycopg", "psycopg_pool")


def _forget(monkeypatch: pytest.MonkeyPatch, roots: tuple[str, ...]) -> None:
    for name in list(sys.modules):
        if name.split(".")[0] in roots:
            monkeypatch.delitem(sys.modules, name)


def test_the_core_package_imports_with_no_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forget(monkeypatch, (*_OPTIONAL, "collide_logging"))
    for name in _OPTIONAL:
        monkeypatch.setitem(sys.modules, name, None)
    fresh = importlib.import_module("collide_logging")
    assert fresh.is_outage(ConnectionRefusedError("refused"))
    assert not fresh.is_outage(ValueError("bad"))


def test_importing_the_core_package_imports_no_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forget(monkeypatch, (*_OPTIONAL, "collide_logging"))
    importlib.import_module("collide_logging")
    assert not [name for name in sys.modules if name.split(".")[0] in _OPTIONAL]
