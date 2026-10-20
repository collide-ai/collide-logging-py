from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
import structlog

import collide_logging
from collide_logging.workers import bind_worker_run_id, run_in_context, with_worker_run_id


@pytest.fixture(autouse=True)
def _clear_contextvars() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _bound() -> dict[str, object]:
    return dict(structlog.contextvars.get_contextvars())


def test_binds_id_inside_block() -> None:
    assert "worker_run_id" not in _bound()
    with bind_worker_run_id("abc12345"):
        assert _bound()["worker_run_id"] == "abc12345"
    assert "worker_run_id" not in _bound()


def test_auto_generates_id() -> None:
    with bind_worker_run_id() as run_id:
        assert re.fullmatch(r"[0-9a-f]{16}", run_id)
        assert _bound()["worker_run_id"] == run_id


def test_explicit_id_used() -> None:
    with bind_worker_run_id("explicit-id") as run_id:
        assert run_id == "explicit-id"
        assert _bound()["worker_run_id"] == "explicit-id"


def test_extras_bound() -> None:
    with bind_worker_run_id("id1", worker="github_collector"):
        ctx = _bound()
        assert ctx["worker_run_id"] == "id1"
        assert ctx["worker"] == "github_collector"
    assert "worker" not in _bound()


def test_clears_on_exception() -> None:
    with pytest.raises(RuntimeError):  # noqa: SIM117
        with bind_worker_run_id("id1", worker="x"):
            raise RuntimeError("boom")
    assert _bound() == {}


def test_decorator() -> None:
    seen: dict[str, object] = {}

    @with_worker_run_id
    def job(x: int) -> int:
        seen.update(_bound())
        return x * 2

    result = job(21)
    assert result == 42
    assert re.fullmatch(r"[0-9a-f]{16}", str(seen["worker_run_id"]))
    assert "worker_run_id" not in _bound()


def test_decorator_preserves_signature_and_returns() -> None:
    @with_worker_run_id
    def add(a: int, b: int) -> int:
        return a + b

    assert add.__name__ == "add"
    assert add(2, 3) == 5


def test_nested_binds() -> None:
    with bind_worker_run_id("outer", worker="A"):
        assert _bound()["worker_run_id"] == "outer"
        assert _bound()["worker"] == "A"

        with bind_worker_run_id("inner", worker="B"):
            assert _bound()["worker_run_id"] == "inner"
            assert _bound()["worker"] == "B"

        # Outer values restored after inner exit
        assert _bound()["worker_run_id"] == "outer"
        assert _bound()["worker"] == "A"

    assert _bound() == {}


def test_reexported_from_package() -> None:
    assert collide_logging.bind_worker_run_id is bind_worker_run_id
    assert collide_logging.with_worker_run_id is with_worker_run_id


def test_with_worker_run_id_types_preserved() -> None:
    seen: list[object] = []

    @with_worker_run_id
    def add(a: int, b: int = 1) -> int:
        seen.append(_bound().get("worker_run_id"))
        return a + b

    assert add(2) == 3
    assert add.__name__ == "add"
    assert isinstance(seen[0], str)
    assert "worker_run_id" not in _bound()


def test_thread_line_carries_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t")
    structlog.contextvars.bind_contextvars(request_id="r1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run_in_context(lambda: log.info("t.x"))).result()
    out = capsys.readouterr()
    lines = [json.loads(x) for x in (out.out + out.err).splitlines() if x.startswith("{")]
    assert next(x for x in lines if x["event"] == "t.x")["request_id"] == "r1"


def test_run_in_context_captures_at_wrap_time() -> None:
    structlog.contextvars.bind_contextvars(request_id="r1")
    wrapped = run_in_context(lambda: _bound().get("request_id"))
    structlog.contextvars.bind_contextvars(request_id="r2")
    assert wrapped() == "r1"


def test_run_in_context_concurrent_calls_do_not_raise() -> None:
    barrier = threading.Barrier(2)

    @run_in_context
    def work(n: int) -> int:
        barrier.wait(timeout=5)
        return n

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work, 1), pool.submit(work, 2)]
        assert sorted(f.result() for f in futures) == [1, 2]


def test_run_in_context_preserves_signature_and_exports() -> None:
    @run_in_context
    def add(a: int, b: int = 1) -> int:
        return a + b

    assert add.__name__ == "add"
    assert add(2) == 3
    assert collide_logging.run_in_context is run_in_context


def test_run_in_context_isolates_bindings_made_inside_calls() -> None:
    seen: list[object] = []

    @run_in_context
    def work() -> None:
        seen.append(_bound().get("leak"))
        structlog.contextvars.bind_contextvars(leak="x")

    work()
    work()
    assert seen == [None, None]
    assert "leak" not in _bound()
