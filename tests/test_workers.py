from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
import structlog

import collide_logging
from collide_logging.workers import bind_worker_run_id, with_worker_run_id


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
        assert re.fullmatch(r"[0-9a-f]{8}", run_id)
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
    assert re.fullmatch(r"[0-9a-f]{8}", str(seen["worker_run_id"]))
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
