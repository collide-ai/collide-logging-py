from __future__ import annotations

import asyncio
import contextvars
import gc
import json as json_lib
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
import structlog

import collide_logging
from collide_logging import StrictModeViolation, log_context
from collide_logging._fields import LIBRARY_OWNED_NAMES


@pytest.fixture(autouse=True)
def _remove_handler() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _configure() -> None:
    # Configured inside each test: capsys only redirects stdout once the test body runs.
    collide_logging.configure(service="svc", json=True)


@pytest.fixture
def strict() -> Iterator[None]:
    collide_logging.configure(service="svc", json=True, strict=True)
    yield
    collide_logging.configure(service="svc", json=True)


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json_lib.loads(line) for line in out.splitlines() if line.strip()]


log = collide_logging.get_logger("t")


def test_log_context_binds_and_resets(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    with log_context(step="a"):
        log.info("op.inside")
    log.info("op.after")

    inside, after = _lines(capsys)
    assert inside["step"] == "a"
    assert "step" not in after


def test_log_context_nested(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    with log_context(step="a"):
        with log_context(step="b"):
            log.info("op.inner")
        log.info("op.outer")

    inner, outer = _lines(capsys)
    assert inner["step"] == "b"
    assert outer["step"] == "a"


def test_log_context_async_isolation(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()

    async def work(step: str) -> None:
        with log_context(step=step):
            await asyncio.sleep(0)
            log.info("op.work", expected=step)
            await asyncio.sleep(0)
            log.info("op.work", expected=step)

    async def main() -> None:
        await asyncio.gather(work("a"), work("b"))

    asyncio.run(main())

    lines = _lines(capsys)
    assert len(lines) == 4
    assert all(line["step"] == line["expected"] for line in lines)


def test_log_context_async_with(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()

    async def main() -> None:
        async with log_context(step="a"):
            log.info("op.inside")
        log.info("op.after")

    asyncio.run(main())

    inside, after = _lines(capsys)
    assert inside["step"] == "a"
    assert "step" not in after


def test_log_context_resets_on_raise(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    with pytest.raises(ValueError, match="boom"), log_context(step="a"):
        raise ValueError("boom")
    log.info("op.after")

    (after,) = _lines(capsys)
    assert "step" not in after


def test_log_context_refuses_library_names() -> None:
    _configure()
    with pytest.raises(TypeError):
        log_context(request_id="x")
    with pytest.raises(TypeError):
        log_context(exception_detail="x")
    with pytest.raises(TypeError):
        log_context(event="x")
    with pytest.raises(TypeError):
        log_context(task="x")
    with pytest.raises(TypeError):
        log_context(task_id="x")


@pytest.mark.parametrize("name", sorted(LIBRARY_OWNED_NAMES))
def test_log_context_refuses_every_owned_name(name: str) -> None:
    _configure()
    with pytest.raises(TypeError):
        log_context(**{name: "x"})


def test_log_context_allows_reserved_context_names(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    with log_context(account_id="acc", user_id="u", agent_run_id="run"):
        log.info("op.inside")

    (line,) = _lines(capsys)
    assert (line["account_id"], line["user_id"], line["agent_run_id"]) == ("acc", "u", "run")


@pytest.mark.parametrize("name", ["Bad", "1bad", "has-dash"])
def test_log_context_rejects_malformed_names(name: str) -> None:
    _configure()
    with pytest.raises(ValueError, match="must match"):
        log_context(**{name: 1})


def test_log_context_rejects_more_than_twenty_fields() -> None:
    _configure()
    with pytest.raises(ValueError, match="at most 20"):
        log_context(**{f"field_{index}": index for index in range(21)})


@pytest.mark.parametrize(
    "value",
    [Decimal("1.5"), object(), [1], float("nan"), 10**5000, 2**63, -(2**63) - 1],
    ids=["decimal", "object", "list", "nan", "huge-int", "above-int64", "below-int64"],
)
def test_unsupported_value_dropped_in_production(
    capsys: pytest.CaptureFixture[str], value: object
) -> None:
    _configure()
    with log_context(step="a", amount=value):  # type: ignore[arg-type]
        log.info("op.inside")

    (line,) = _lines(capsys)
    assert line["step"] == "a"
    assert "amount" not in line


def test_int64_bounds_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    with log_context(high=2**63 - 1, low=-(2**63)):
        log.info("op.inside")

    (line,) = _lines(capsys)
    assert (line["high"], line["low"]) == (2**63 - 1, -(2**63))


@pytest.mark.usefixtures("strict")
def test_unsupported_value_raises_under_strict() -> None:
    with pytest.raises(StrictModeViolation, match="amount"):
        log_context(amount=Decimal("1.5"))  # type: ignore[arg-type]


@pytest.mark.usefixtures("strict")
def test_reserved_context_conflict_raises_under_strict() -> None:
    outer = log_context(user_id="u1")
    with outer, pytest.raises(StrictModeViolation, match="user_id"), log_context(user_id="u2"):
        pass


@pytest.mark.usefixtures("strict")
def test_reserved_context_same_value_passes_under_strict() -> None:
    structlog.contextvars.bind_contextvars(account_id=7)
    with log_context(account_id="7"):
        assert structlog.contextvars.get_contextvars()["account_id"] == "7"


def test_reserved_context_conflict_binds_in_production(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    with log_context(user_id="u1"):
        with log_context(user_id="u2"):
            log.info("op.inner")
        log.info("op.outer")

    inner, outer = _lines(capsys)
    assert (inner["user_id"], outer["user_id"]) == ("u2", "u1")


def test_log_context_caps_strings_and_accepts_uuid(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    ident = uuid.uuid4()
    with log_context(long="x" * 500, ident=ident, flag=True, none=None, ratio=0.5):
        log.info("op.inside")

    (line,) = _lines(capsys)
    assert line["long"] == "x" * 128
    assert line["ident"] == str(ident)
    assert (line["flag"], line["none"], line["ratio"]) == (True, None, 0.5)


def test_decorating_a_generator_function_raises() -> None:
    def rows() -> Iterator[int]:
        yield 1

    with pytest.raises(TypeError, match="generator"):
        log_context(step="a")(rows)


def test_decorating_an_async_generator_function_raises() -> None:
    async def rows() -> AsyncIterator[int]:
        yield 1

    with pytest.raises(TypeError, match="generator"):
        log_context(step="a")(rows)


def test_log_context_decorator_sync_async(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()

    @log_context(step="sync")
    def sync_work() -> int:
        log.info("op.sync")
        return 1

    @log_context(step="async")
    async def async_work() -> int:
        log.info("op.async")
        return 2

    assert sync_work() == 1
    assert asyncio.run(async_work()) == 2
    log.info("op.after")

    sync_line, async_line, after = _lines(capsys)
    assert sync_line["step"] == "sync"
    assert async_line["step"] == "async"
    assert "step" not in after


def test_decorated_async_function_isolated_across_concurrent_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()

    @log_context(stage="outer")
    async def work(step: str) -> None:
        with log_context(step=step):
            await asyncio.sleep(0)
            log.info("op.work", expected=step)
            await asyncio.sleep(0)
            log.info("op.work", expected=step)

    async def main() -> None:
        await asyncio.gather(work("a"), work("b"))

    asyncio.run(main())
    log.info("op.after")

    *work_lines, after = _lines(capsys)
    assert len(work_lines) == 4
    assert all(line["step"] == line["expected"] for line in work_lines)
    assert all(line["stage"] == "outer" for line in work_lines)
    assert "stage" not in after and "step" not in after


def test_shared_instance_reentered_concurrently(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    shared = log_context(shared="yes")

    async def work() -> None:
        with shared:
            await asyncio.sleep(0)
            log.info("op.work")

    async def main() -> None:
        await asyncio.gather(work(), work(), work())

    asyncio.run(main())
    log.info("op.after")

    *work_lines, after = _lines(capsys)
    assert [line["shared"] for line in work_lines] == ["yes"] * 3
    assert "shared" not in after


def test_exception_carries_failure_context(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    try:
        with log_context(connector_id="c1"), log_context(step="revoke"):
            raise ValueError("SENTINEL")
    except ValueError:
        log.exception("drive.teardown.failed", attempt=1)

    out = capsys.readouterr().out
    assert "SENTINEL" not in out
    line = json_lib.loads(out.strip().splitlines()[-1])
    assert (line["connector_id"], line["step"], line["attempt"]) == ("c1", "revoke", 1)


def test_innermost_block_wins_on_the_exception(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    try:
        with log_context(step="outer", outer_only="o"), log_context(step="inner"):
            raise ValueError("x")
    except ValueError:
        log.exception("op.failed")

    (line,) = _lines(capsys)
    assert (line["step"], line["outer_only"]) == ("inner", "o")


def test_captured_context_is_below_call_site_and_declared_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()

    class DeclaredError(Exception):
        def __log_fields__(self) -> dict[str, str]:
            return {"step": "declared"}

    try:
        with log_context(step="ctx", other="ctx", third="ctx"):
            raise DeclaredError("x")
    except DeclaredError:
        log.exception("op.failed", third="call-site")

    (line,) = _lines(capsys)
    assert (line["step"], line["other"], line["third"]) == ("declared", "ctx", "call-site")


def test_captured_context_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    try:
        with log_context(password="SENTINEL"):
            raise ValueError("x")
    except ValueError:
        log.exception("op.failed")

    out = capsys.readouterr().out
    assert "SENTINEL" not in out
    assert json_lib.loads(out.strip().splitlines()[-1])["password"] == "***REDACTED***"


def test_exception_without_dict_still_renders(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()

    class RefusingError(Exception):
        def __setattr__(self, name: str, value: object) -> None:
            if name == "_collide_log_context":
                raise AttributeError(name)
            super().__setattr__(name, value)

    try:
        with log_context(step="a"):
            raise RefusingError("SENTINEL")
    except RefusingError:
        log.exception("op.failed")

    out = capsys.readouterr().out
    line = json_lib.loads(out.strip().splitlines()[-1])
    assert line["event"] == "op.failed"
    assert "step" not in line
    assert "SENTINEL" not in out


def test_malformed_captured_context_is_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    error = ValueError("x")
    error._collide_log_context = "not a mapping"  # type: ignore[attr-defined]
    try:
        raise error
    except ValueError:
        log.exception("op.failed")

    (line,) = _lines(capsys)
    assert line["event"] == "op.failed"


def _generator_inside_block() -> Any:
    with log_context(step="gen"):
        yield


@pytest.mark.parametrize(
    "run_elsewhere",
    [
        lambda call: contextvars.Context().run(call),
        lambda call: contextvars.copy_context().run(call),
    ],
    ids=["empty-context", "copied-context"],
)
def test_exit_in_another_context_never_raises(run_elsewhere: Any) -> None:
    _configure()
    generator = _generator_inside_block()
    next(generator)

    run_elsewhere(generator.close)


@pytest.mark.parametrize(
    "run_elsewhere",
    [
        lambda call: contextvars.Context().run(call),
        lambda call: contextvars.copy_context().run(call),
    ],
    ids=["empty-context", "copied-context"],
)
def test_exception_propagates_unchanged_when_exit_is_in_another_context(
    run_elsewhere: Any,
) -> None:
    _configure()
    generator = _generator_inside_block()
    next(generator)
    error = KeyError("original")

    with pytest.raises(KeyError) as raised:
        run_elsewhere(lambda: generator.throw(error))

    assert raised.value is error
    assert error._collide_log_context == {"step": "gen"}  # type: ignore[attr-defined]


def test_generator_closed_from_another_task_never_raises() -> None:
    _configure()
    generator = _generator_inside_block()
    next(generator)

    async def close_elsewhere() -> None:
        await asyncio.create_task(asyncio.to_thread(generator.close))

    asyncio.run(close_elsewhere())


def test_shared_instance_entered_from_threads(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    shared = log_context(shared="yes")
    barrier = threading.Barrier(4)

    def work(index: int) -> None:
        with shared:
            barrier.wait(timeout=5)
            log.info("op.work", index=index)
            barrier.wait(timeout=5)
        log.info("op.after", index=index)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(4)))

    lines = _lines(capsys)
    work_lines = [line for line in lines if line["event"] == "op.work"]
    after_lines = [line for line in lines if line["event"] == "op.after"]
    assert len(work_lines) == len(after_lines) == 4
    assert all(line["shared"] == "yes" for line in work_lines)
    assert all("shared" not in line for line in after_lines)


def test_prebound_key_is_restored_not_deleted(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    tokens = structlog.contextvars.bind_contextvars(step="outer")
    try:
        with log_context(step="inner"):
            log.info("op.inside")
        log.info("op.after")
    finally:
        structlog.contextvars.reset_contextvars(**tokens)

    inside, after = _lines(capsys)
    assert (inside["step"], after["step"]) == ("inner", "outer")


def test_captured_uuid_renders_as_canonical_string(capsys: pytest.CaptureFixture[str]) -> None:
    _configure()
    ident = uuid.uuid4()
    try:
        with log_context(ident=ident):
            raise ValueError("x")
    except ValueError:
        log.exception("op.failed")

    (line,) = _lines(capsys)
    assert line["ident"] == str(ident)


def _rows() -> Iterator[int]:
    with log_context(step="rows"):
        yield 1
        yield 2


def test_kept_generator_does_not_leak_the_outer_block() -> None:
    _configure()
    kept = []
    with log_context(account_id="tenant-a"):
        rows = _rows()
        next(rows)
        kept.append(rows)
    assert structlog.contextvars.get_contextvars() == {}

    kept.clear()
    assert structlog.contextvars.get_contextvars() == {}


def test_next_request_in_the_thread_does_not_carry_the_previous_account(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    kept = []

    def view(account_id: str) -> None:
        with log_context(account_id=account_id):
            rows = _rows()
            next(rows)
            kept.append(rows)
        log.info("view.done")

    view("tenant-a")
    log.info("next.request")
    kept.clear()

    done, next_request = _lines(capsys)
    assert "account_id" not in done
    assert "account_id" not in next_request
    assert structlog.contextvars.get_contextvars() == {}


def test_async_generator_left_with_break_does_not_leak() -> None:
    _configure()

    async def stream() -> AsyncIterator[int]:
        with log_context(step="stream"):
            for index in range(3):
                yield index

    async def handler() -> dict[str, Any]:
        async with log_context(account_id="tenant-a"):
            async for _ in stream():
                break
            gc.collect()
            await asyncio.sleep(0)
        return structlog.contextvars.get_contextvars()

    loop_errors: list[dict[str, Any]] = []

    async def main() -> dict[str, Any]:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        seen = await handler()
        gc.collect()
        await asyncio.sleep(0)
        return seen

    assert asyncio.run(main()) == {}
    assert loop_errors == []
    assert structlog.contextvars.get_contextvars() == {}


def test_generator_closed_in_a_copied_context_does_not_leak() -> None:
    _configure()
    with log_context(outer="o"):
        rows = _rows()
        next(rows)
        contextvars.copy_context().run(rows.close)
    assert structlog.contextvars.get_contextvars() == {}


def test_out_of_order_exits_restore_correctly() -> None:
    _configure()
    outer = log_context(step="outer")
    inner = log_context(step="inner", other="x")
    outer.__enter__()
    inner.__enter__()
    outer.__exit__(None, None, None)
    assert structlog.contextvars.get_contextvars() == {}
    inner.__exit__(None, None, None)
    assert structlog.contextvars.get_contextvars() == {}


def test_live_block_beats_declared_and_declared_beats_captured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()

    class DeclaredError(Exception):
        def __log_fields__(self) -> dict[str, str]:
            return {"step": "declared"}

    try:
        with log_context(step="ctx"):
            try:
                raise DeclaredError("x")
            except DeclaredError:
                log.exception("op.inside")
            raise DeclaredError("y")
    except DeclaredError:
        log.exception("op.outside")

    inside, outside = _lines(capsys)
    assert (inside["step"], outside["step"]) == ("ctx", "declared")
