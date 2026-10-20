from __future__ import annotations

import contextlib
import json
import logging
import queue
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from importlib.metadata import entry_points
from multiprocessing import Value
from typing import Any

import pytest
import structlog

pytest.importorskip("django_q")

import django

django.setup()

import django_q.conf  # noqa: E402
import django_q.worker  # noqa: E402
from django_q.conf import ErrorReporter as QErrorReporter  # noqa: E402
from django_q.exceptions import TimeoutException  # noqa: E402
from django_q.monitor import monitor  # noqa: E402
from django_q.tasks import async_task  # noqa: E402
from django_q.worker import worker  # noqa: E402

import collide_logging  # noqa: E402
from collide_logging import _HANDLER_TAG  # noqa: E402
from collide_logging import django_q as cq  # noqa: E402
from collide_logging._unit import close_unit, current_unit, open_unit  # noqa: E402
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
    first_finished = next(x for x in lines if x["event"] == "task.finished")
    assert (first_finished["task_id"], first_finished["account_id"]) == ("id1", "acct-first-task")
    after = lines[lines.index(first_finished) + 1 :]
    assert after
    assert all("account_id" not in x for x in after)
    assert all(x.get("worker_run_id") != run_id for x in after)
    second = next(x for x in lines if x["event"] == "second.done")
    assert second["worker_run_id"] != run_id
    assert "account_id" not in second


def test_task_finished_error_without_reporter_has_no_error_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.explode", "boom", "id3"))
    lines = _lines(capsys)
    finished = [x for x in lines if x["event"] == "task.finished"]
    assert len(finished) == 1
    assert finished[0]["outcome"] == "error"
    assert finished[0]["level"] == "error"
    assert finished[0]["task_name"] == "boom"
    assert "error_type" not in finished[0]
    assert not any(name.startswith("exception") for name in finished[0])
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
    finished = [x for x in lines if x["event"] == "task.finished"]
    assert [(x["task_name"], x["outcome"]) for x in finished] == [
        ("missing", "error"),
        ("good", "ok"),
    ]


def test_connect_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.explode", "boom", "id6"))
    lines = _lines(capsys)
    finished = [x for x in lines if x["event"] == "task.finished"]
    assert len(finished) == 1
    assert finished[0]["task_name"] == "boom"
    assert finished[0]["task_id"] == "id6"
    assert "canary" not in json.dumps(lines)


def log_task_id() -> None:
    log.info("inside.task")
    raise ValueError("canary")


def test_finished_line_joins_task_lines_by_task_id(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.log_task_id", "joined", "id7"))
    lines = _lines(capsys)
    inside = next(x for x in lines if x["event"] == "inside.task")
    finished = next(x for x in lines if x["event"] == "task.finished")
    assert inside["task_id"] == finished["task_id"] == "id7"
    assert inside["worker_run_id"] == finished["worker_run_id"]


def test_post_execute_in_worker_clears_context_and_closes_unit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)
    task = _task("x.y", "dead", "id8")
    cq._on_pre_execute(task=task)
    assert current_unit() is not None
    structlog.contextvars.bind_contextvars(account_id="a1")
    cq._on_post_execute_in_worker(task={**task, "success": False})
    assert structlog.contextvars.get_contextvars() == {}
    assert current_unit() is None
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert len(finished) == 1
    assert finished[0]["task_name"] == "dead"
    assert finished[0]["account_id"] == "a1"


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
    finished = next(x for x in lines if x["event"] == "task.finished")
    assert inside["task"] == path
    assert finished["task"] == path
    assert finished["task_name"] == "humanhash-name"


def test_string_func_binds_function_path(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.log_only", "n", "id10"))
    line = next(x for x in _lines(capsys) if x["event"] == "second.done")
    assert line["task"] == f"{__name__}.log_only"


def test_task_finished_survives_strict_mode(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("collide_logging",))
    task = _task("x.y", "dead", "id9")
    cq._on_pre_execute(task=task)
    collide_logging.annotate(rows=2)
    try:
        with collide_logging.log_context(step="s"):
            raise ValueError("canary")
    except ValueError:
        cq.ErrorReporter().report()
    cq._on_post_execute_in_worker(task={**task, "success": False})
    lines = _lines(capsys)
    assert [x["event"] for x in lines] == ["task.finished"]
    line = lines[0]
    assert (line["task"], line["task_id"], line["task_name"]) == ("x.y", "id9", "dead")
    assert (line["outcome"], line["error_type"], line["step"], line["rows"]) == (
        "error",
        "ValueError",
        "s",
        2,
    )


@pytest.fixture
def reporter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(django_q.worker, "error_reporter", QErrorReporter([cq.ErrorReporter()]))


def annotate_and_return() -> str:
    collide_logging.annotate(rows=3, kind="import")
    return "SENTINEL-result"


def fail_in_context() -> None:
    collide_logging.annotate(rows=1)
    with collide_logging.log_context(step="s"):
        raise ValueError("canary")


def fail_with_sentinel() -> None:
    raise ValueError("SENTINEL-error")


def test_task_finished_ok(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.annotate_and_return", "fine", "id20"))
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert len(finished) == 1
    line = finished[0]
    assert line["level"] == "info"
    assert line["outcome"] == "ok"
    assert (line["rows"], line["kind"]) == (3, "import")
    assert isinstance(line["duration_ms"], int)
    assert line["duration_ms"] >= 0
    assert (line["task"], line["task_id"]) == (f"{__name__}.annotate_and_return", "id20")
    assert "worker_run_id" in line
    assert "error_type" not in line


def test_task_finished_error_carries_context(
    capsys: pytest.CaptureFixture[str], reporter: None
) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.fail_in_context", "boom", "id21"))
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert len(finished) == 1
    line = finished[0]
    assert line["level"] == "error"
    assert line["outcome"] == "error"
    assert line["error_type"] == "ValueError"
    assert line["exception_type"] == "builtins.ValueError"
    assert line["step"] == "s"
    assert line["rows"] == 1


def test_task_result_never_rendered(capsys: pytest.CaptureFixture[str], reporter: None) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(
        _task(f"{__name__}.annotate_and_return", "fine", "id22"),
        _task(f"{__name__}.fail_with_sentinel", "bad", "id23"),
    )
    out = capsys.readouterr()
    raw = out.out + out.err
    finished = [
        x
        for x in (json.loads(r) for r in raw.splitlines() if r.startswith("{"))
        if x["event"] == "task.finished"
    ]
    assert [x["outcome"] for x in finished] == ["ok", "error"]
    assert "SENTINEL" not in raw


def test_sync_task_skipped(capsys: pytest.CaptureFixture[str], reporter: None) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    unit, token = open_unit()
    try:
        async_task(f"{__name__}.annotate_and_return", sync=True)
        with pytest.raises(ValueError, match="canary"):
            async_task(f"{__name__}.explode", sync=True)
        assert unit.exc is None
    finally:
        close_unit(unit, token)
    assert not [x for x in _lines(capsys) if x["event"] == "task.finished"]


def test_reporter_never_raises(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    cq.ErrorReporter().report()
    unit, token = open_unit()
    try:
        try:
            raise ValueError("canary")
        except ValueError:
            cq.ErrorReporter(dsn="unused").report()
    finally:
        close_unit(unit, token)
    assert unit.exc is None
    _run(_task(f"{__name__}.log_only", "after", "id24"))
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert [x["outcome"] for x in finished] == ["ok"]


def test_reporter_swallows_a_failing_record(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_record(_exc: object) -> None:
        raise RuntimeError("record failed")

    monkeypatch.setattr("collide_logging._django_q_reporter.record_exception", failing_record)
    collide_logging.configure(service="t", json=True)
    task = _task("x.y", "dead", "id27")
    cq._on_pre_execute(task=task)
    try:
        raise ValueError("canary")
    except ValueError:
        cq.ErrorReporter().report()
    cq._on_post_execute_in_worker(task={**task, "success": False})
    (line,) = _lines(capsys)
    assert (line["event"], line["outcome"]) == ("task.finished", "error")
    assert "error_type" not in line


def test_annotations_do_not_leak_between_tasks(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(
        _task(f"{__name__}.annotate_and_return", "one", "id25"),
        _task(f"{__name__}.log_only", "two", "id26"),
    )
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert [x["task_id"] for x in finished] == ["id25", "id26"]
    assert "rows" not in finished[1]
    assert current_unit() is None
    assert structlog.contextvars.get_contextvars() == {}


def test_error_reporter_entry_point_resolves() -> None:
    (entry,) = entry_points(group="djangoq.errorreporters", name="collide_logging")
    assert entry.load() is cq.ErrorReporter


_SETUP_WITH_REPORTER = textwrap.dedent(
    """
    import django
    from django.conf import settings

    settings.configure(
        INSTALLED_APPS=["django_q"],
        DATABASES={},
        SECRET_KEY="test-only",
        Q_CLUSTER={"name": "t", "error_reporter": {"collide_logging": {}}},
    )
    django.setup()

    import django_q.conf
    from collide_logging._django_q_reporter import ErrorReporter

    targets = django_q.conf.error_reporter.targets
    assert [type(target) for target in targets] == [ErrorReporter], targets
    print("setup-ok")
    """
)


def test_django_setup_loads_the_error_reporter() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SETUP_WITH_REPORTER],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "setup-ok"


def catch_sync_failure() -> str:
    with contextlib.suppress(ValueError):
        async_task(f"{__name__}.explode", sync=True)
    return "handled"


def time_out() -> None:
    raise TimeoutException


def test_caught_sync_failure_leaves_outer_task_ok(
    capsys: pytest.CaptureFixture[str], reporter: None
) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.catch_sync_failure", "outer", "id30"))
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert len(finished) == 1
    line = finished[0]
    assert line["outcome"] == "ok"
    assert line["level"] == "info"
    assert "error_type" not in line
    assert not any(name.startswith("exception") for name in line)


def test_timed_out_task_finishes_with_error(
    capsys: pytest.CaptureFixture[str], reporter: None
) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(_task(f"{__name__}.time_out", "slow", "id31"))
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert len(finished) == 1
    assert finished[0]["outcome"] == "error"
    assert finished[0]["error_type"] == "TimeoutException"


def test_task_finished_ok_survives_strict_mode(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("collide_logging",))
    task = _task("x.y", "fine", "id32")
    cq._on_pre_execute(task=task)
    collide_logging.annotate(rows=2)
    cq._on_post_execute_in_worker(task={**task, "success": True})
    lines = _lines(capsys)
    assert [x["event"] for x in lines] == ["task.finished"]
    assert (lines[0]["outcome"], lines[0]["rows"], lines[0]["task_name"]) == ("ok", 2, "fine")


def fail_without_context() -> None:
    raise ValueError("canary")


def test_library_outcome_lines_pass_strict(
    capsys: pytest.CaptureFixture[str], reporter: None
) -> None:
    collide_logging.configure(service="t", json=True, strict=True, first_party=("app",))
    cq.connect()
    _run(_task(f"{__name__}.fail_without_context", "bare", "id40"))
    finished = [x for x in _lines(capsys) if x["event"] == "task.finished"]
    assert len(finished) == 1
    assert finished[0]["outcome"] == "error"
    assert finished[0]["task_name"] == "bare"


_suspended: list[Iterator[None]] = []


def suspend_inside_a_block() -> None:
    def held() -> Iterator[None]:
        with collide_logging.log_context(held="first"):
            yield

    generator = held()
    next(generator)
    _suspended.append(generator)


def close_inside_a_block() -> None:
    with collide_logging.log_context(step="second"):
        _suspended.pop().close()
        log.info("second.after_close")


def test_block_left_open_by_a_previous_task_cannot_unwind_the_next(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True)
    cq.connect()
    _run(
        _task(f"{__name__}.suspend_inside_a_block", "one", "id50"),
        _task(f"{__name__}.close_inside_a_block", "two", "id51"),
    )
    line = next(x for x in _lines(capsys) if x["event"] == "second.after_close")
    assert line["step"] == "second"
    assert "held" not in line


_NOT_CONFIGURED = "collide_logging.django_q.reporter_not_configured"


def test_connect_warns_when_the_reporter_is_not_configured(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(django_q.conf, "error_reporter", None)
    collide_logging.configure(service="t", json=True)
    cq.connect()
    lines = _lines(capsys)
    assert [(x["event"], x["level"]) for x in lines] == [(_NOT_CONFIGURED, "warning")]


def test_connect_is_quiet_when_the_reporter_is_configured(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = object()
    monkeypatch.setattr(
        django_q.conf, "error_reporter", QErrorReporter([other, cq.ErrorReporter()])
    )
    collide_logging.configure(service="t", json=True)
    cq.connect()
    assert _lines(capsys) == []


def test_connect_warns_when_only_other_reporters_are_configured(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(django_q.conf, "error_reporter", QErrorReporter([object()]))
    collide_logging.configure(service="t", json=True)
    cq.connect()
    assert [x["event"] for x in _lines(capsys)] == [_NOT_CONFIGURED]


class _BrokenReporter:
    @property
    def targets(self) -> list[object]:
        raise RuntimeError("broken")


def test_connect_never_raises_checking_the_reporter(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(django_q.conf, "error_reporter", _BrokenReporter())
    collide_logging.configure(service="t", json=True)
    cq.connect()
    assert [x["event"] for x in _lines(capsys)] == [_NOT_CONFIGURED]
