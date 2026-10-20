from __future__ import annotations

import contextvars
import json
import logging
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

import collide_logging
from collide_logging import annotate, log_context
from collide_logging._exception_fields import exception_fields
from collide_logging._unit import (
    RecordedExceptionFilter,
    close_unit,
    current_unit,
    discard_exception,
    open_unit,
    record_exception,
)
from collide_logging.testing import unit_of_work


@pytest.fixture(autouse=True)
def _remove_handler() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_annotate_last_wins_and_cap() -> None:
    with unit_of_work() as work:
        annotate(a=1)
        annotate(a=2)
        annotate(**{f"name_{index}": index for index in range(70)})
        annotate(a=3)

    assert work.annotations["a"] == 3
    assert len(work.annotations) == 64
    assert "name_62" in work.annotations
    assert "name_63" not in work.annotations


def test_annotate_drops_owned_names_and_bad_values() -> None:
    with unit_of_work() as work:
        annotate(outcome="x", duration_ms=1, rows=2, blob=b"raw", ratio=float("nan"))  # type: ignore[arg-type]

    assert work.annotations == {"rows": 2}


def test_annotate_outside_unit_noop(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    assert current_unit() is None
    annotate(rows=3)
    assert current_unit() is None
    assert capsys.readouterr().out == ""


def test_close_unit_restores_previous_state() -> None:
    unit, token = open_unit()
    assert current_unit() is unit
    close_unit(unit, token)
    assert current_unit() is None


def test_closed_unit_is_not_current_in_a_copied_context() -> None:
    unit, token = open_unit()
    annotate(rows=1)
    copied = contextvars.copy_context()
    close_unit(unit, token)

    def late() -> object:
        annotate(late=1)
        record_exception(ValueError("late"))
        return current_unit()

    assert copied.run(late) is None
    assert (unit.closed, unit.annotations, unit.exc) == (True, {}, None)


def test_record_exception_sets_current_unit_exc() -> None:
    error = ValueError("x")
    with unit_of_work() as work:
        record_exception(None)
        record_exception(error)
    assert work.exception is error
    record_exception(error)  # outside a unit: nothing to record on, and no error


def _raise_in_unit(error: Exception, **fields: str) -> dict[str, object]:
    unit, token = open_unit()
    try:
        with pytest.raises(ValueError), log_context(**fields):
            raise error
    finally:
        close_unit(unit, token)
    return dict(exception_fields([error]))


def test_captured_context_is_replaced_by_a_new_unit() -> None:
    error = ValueError("shared")
    assert _raise_in_unit(error, account_id="a", step="one") == {"account_id": "a", "step": "one"}
    assert _raise_in_unit(error, account_id="b") == {"account_id": "b"}


def test_captured_context_merges_within_one_unit() -> None:
    error = ValueError("x")
    unit, token = open_unit()
    try:
        with (
            pytest.raises(ValueError),
            log_context(outer="o", step="outer"),
            log_context(step="inner"),
        ):
            raise error
    finally:
        close_unit(unit, token)
    assert exception_fields([error]) == {"step": "inner", "outer": "o"}


def test_captured_context_merges_outside_units() -> None:
    error = ValueError("x")
    for fields in ({"step": "one"}, {"step": "two", "extra": "e"}):
        with pytest.raises(ValueError), log_context(**fields):
            raise error
    assert exception_fields([error]) == {"step": "one", "extra": "e"}


def test_annotate_drops_logging_call_keywords() -> None:
    with unit_of_work() as work:
        annotate(exc_info=True, stack_info=True, stacklevel=2, rows=1)

    assert work.annotations == {"rows": 1}


def test_context_captured_outside_a_unit_merges_into_the_first_unit() -> None:
    errors: list[ValueError] = []

    def worker() -> None:
        try:
            with log_context(inner="i"):
                raise ValueError("x")
        except ValueError as error:
            errors.append(error)

    unit, token = open_unit()
    try:
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        with pytest.raises(ValueError), log_context(outer="o"):
            raise errors[0]
    finally:
        close_unit(unit, token)

    assert exception_fields([errors[0]]) == {"inner": "i", "outer": "o"}


def test_close_unit_from_another_context_does_not_raise() -> None:
    unit, token = contextvars.copy_context().run(open_unit)
    close_unit(unit, token)
    assert current_unit() is None


def _record(name: str, exc: BaseException | None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc is not None else None
    return logging.LogRecord(name, logging.ERROR, "f", 1, "m", None, exc_info)


def test_recorded_exception_filter_drops_server_records_of_the_outcome_exception() -> None:
    error = ValueError("x")
    record_filter = RecordedExceptionFilter()
    assert record_filter.filter(_record("django.request", error))
    assert record_filter.filter(_record("uvicorn.error", error))

    unit, token = open_unit()
    try:
        record_exception(error)
    finally:
        close_unit(unit, token)

    assert current_unit() is None
    assert not record_filter.filter(_record("django.request", error))
    assert not record_filter.filter(_record("uvicorn.error", error))
    assert record_filter.filter(_record("app", error))


def test_recorded_exception_filter_keeps_every_other_record() -> None:
    record_filter = RecordedExceptionFilter()
    unit, token = open_unit()
    try:
        record_exception(ValueError("recorded"))
        for name in ("django.request", "uvicorn.error"):
            assert record_filter.filter(_record(name, ValueError("other")))
            assert record_filter.filter(_record(name, None))
    finally:
        close_unit(unit, token)
    startup = logging.LogRecord("uvicorn.error", logging.INFO, "f", 1, "started", None, None)
    assert record_filter.filter(startup)


def test_discarded_exception_is_no_longer_filtered() -> None:
    error = ValueError("x")
    unit, token = open_unit()
    try:
        record_exception(error)
        discard_exception(unit)
    finally:
        close_unit(unit, token)
    assert RecordedExceptionFilter().filter(_record("uvicorn.error", error))


def test_stale_capture_from_another_unit_is_ignored() -> None:
    error = ValueError("shared")
    first, first_token = open_unit()
    try:
        with pytest.raises(ValueError), log_context(account_id="A", step="one"):
            raise error
    finally:
        close_unit(first, first_token)

    second, second_token = open_unit()
    try:
        with pytest.raises(ValueError):
            raise error
        assert exception_fields([error]) == {}
    finally:
        close_unit(second, second_token)
    assert exception_fields([error]) == {"account_id": "A", "step": "one"}


def test_annotate_drops_identity_names() -> None:
    with unit_of_work() as work:
        annotate(account_id="a", user_id="u", agent_run_id="r", rows=2)

    assert work.annotations == {"rows": 2}
