from __future__ import annotations

import json
import logging
import queue
from collections.abc import Iterator
from multiprocessing import Value
from typing import Any

import pytest
import structlog

pytest.importorskip("django_q")

import django

django.setup()

from django_q.monitor import monitor  # noqa: E402
from django_q.tasks import async_task  # noqa: E402
from django_q.worker import worker  # noqa: E402

import collide_logging  # noqa: E402
from collide_logging import _HANDLER_TAG  # noqa: E402
from collide_logging import django_q as cq  # noqa: E402
from tests._django_q_broker import NullBroker  # noqa: E402

log = collide_logging.get_logger("test.tasks")


def bind_and_log() -> None:
    structlog.contextvars.bind_contextvars(account_id="acct-first-task")
    log.info("first.done")


def log_only() -> None:
    log.info("second.done")


def explode() -> None:
    raise ValueError("canary")


def _task(func: str, name: str, task_id: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": name,
        "func": func,
        "args": (),
        "kwargs": {},
        "started": None,
        "sync": False,
    }


def _run(*tasks: dict[str, Any]) -> None:
    task_queue: queue.Queue[Any] = queue.Queue()
    result_queue: queue.Queue[Any] = queue.Queue()
    for task in tasks:
        task_queue.put(task)
    task_queue.put("STOP")
    worker(task_queue, result_queue, Value("f", -1))  # type: ignore[arg-type]
    result_queue.put("STOP")
    monitor(result_queue, broker=NullBroker())  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    django_q_logger = logging.getLogger("django-q")
    django_q_logger.setLevel(logging.NOTSET)
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    structlog.reset_defaults()


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr()
    raw = out.out + out.err
    assert "canary" not in raw
    return [json.loads(x) for x in raw.splitlines() if x.startswith("{")]


def test_second_task_carries_none_of_first(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(
        _task(f"{__name__}.bind_and_log", "one", "id1"),
        _task(f"{__name__}.log_only", "two", "id2"),
    )
    lines = _lines(capsys)
    first = next(x for x in lines if x["event"] == "first.done")
    assert first["account_id"] == "acct-first-task"
    run_id = first["worker_run_id"]
    after = lines[lines.index(first) + 1 :]
    assert after
    assert all("account_id" not in x for x in after)
    assert all(x.get("worker_run_id") != run_id for x in after)
    second = next(x for x in lines if x["event"] == "second.done")
    assert second["worker_run_id"] != run_id
    assert "account_id" not in second


def test_failed_task_logs_task_failed_without_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.explode", "boom", "id3"))
    lines = _lines(capsys)
    failed = [x for x in lines if x["event"] == "task.failed"]
    assert len(failed) == 1
    assert failed[0]["task_name"] == "boom"
    assert "canary" not in json.dumps(lines)


def test_unresolvable_task_does_not_kill_worker(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(
        _task("no.such.module.func", "missing", "id4"),
        _task(f"{__name__}.log_only", "good", "id5"),
    )
    lines = _lines(capsys)
    assert any(x["event"] == "second.done" for x in lines)
    failed = [x for x in lines if x["event"] == "task.failed"]
    assert len(failed) == 1
    assert failed[0]["task_name"] == "missing"


def test_connect_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.explode", "boom", "id6"))
    lines = _lines(capsys)
    failed = [x for x in lines if x["event"] == "task.failed"]
    assert len(failed) == 1
    assert failed[0]["task_name"] == "boom"
    assert failed[0]["task_id"] == "id6"
    assert "canary" not in json.dumps(lines)


def log_task_id() -> None:
    log.info("inside.task")
    raise ValueError("canary")


def test_failure_line_joins_task_lines_by_task_id(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.log_task_id", "joined", "id7"))
    lines = _lines(capsys)
    inside = next(x for x in lines if x["event"] == "inside.task")
    failed = next(x for x in lines if x["event"] == "task.failed")
    assert inside["task_id"] == failed["task_id"] == "id7"


def test_post_execute_clears_context_and_logs_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)
    structlog.contextvars.bind_contextvars(account_id="a1", worker_run_id="w1")
    cq._on_post_execute(task={"id": "id8", "name": "dead", "func": "x.y", "success": False})
    assert structlog.contextvars.get_contextvars() == {}
    failed = [x for x in _lines(capsys) if x["event"] == "task.failed"]
    assert len(failed) == 1
    assert failed[0]["task_name"] == "dead"


def test_sync_success_keeps_callers_context(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    structlog.contextvars.bind_contextvars(request_id="r1")
    async_task(f"{__name__}.log_only", sync=True)
    bound = structlog.contextvars.get_contextvars()
    assert bound == {"request_id": "r1"}
    line = next(x for x in _lines(capsys) if x["event"] == "second.done")
    assert line["request_id"] == "r1"
    assert "worker_run_id" not in line


def test_sync_failure_raises_and_keeps_callers_context() -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    structlog.contextvars.bind_contextvars(request_id="r1")
    with pytest.raises(ValueError, match="canary"):
        async_task(f"{__name__}.explode", sync=True)
    assert structlog.contextvars.get_contextvars() == {"request_id": "r1"}


def test_task_without_task_name_binds_function_path(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    task = _task(f"{__name__}.log_task_id", "humanhash-name", "id9")
    task["func"] = log_task_id
    _run(task)
    lines = _lines(capsys)
    path = f"{__name__}.log_task_id"
    inside = next(x for x in lines if x["event"] == "inside.task")
    failed = next(x for x in lines if x["event"] == "task.failed")
    assert inside["task"] == path
    assert failed["task"] == path
    assert failed["task_name"] == "humanhash-name"


def test_string_func_binds_function_path(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.log_only", "n", "id10"))
    line = next(x for x in _lines(capsys) if x["event"] == "second.done")
    assert line["task"] == f"{__name__}.log_only"


def test_task_failed_survives_strict_mode(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("collide_logging",))
    task = _task("x.y", "dead", "id9")
    cq._on_pre_execute(task=task)
    cq._on_post_execute(task={**task, "success": False})
    lines = _lines(capsys)
    assert [x["event"] for x in lines] == ["task.failed"]
    assert (lines[0]["task"], lines[0]["task_id"], lines[0]["task_name"]) == ("x.y", "id9", "dead")
