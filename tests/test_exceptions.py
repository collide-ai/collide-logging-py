from __future__ import annotations

import json as json_lib
import logging
import re
import traceback
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import collide_logging
from collide_logging._processors import (
    _MAX_EXCEPTION_CHAIN,
    _MAX_EXCEPTION_CHARS,
    _elided,
    _exception_chain,
    _type_name,
)


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _record(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    return json_lib.loads(out.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_exception_messages_never_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log = collide_logging.get_logger("t")
    try:
        try:
            raise KeyError("canary-2")
        except KeyError as inner:
            err = ValueError("canary-1")
            err.add_note("canary-3")
            raise err from inner
    except ValueError:
        log.exception("x.failed")

    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception_type"] == "builtins.ValueError"
    assert "exc_info" not in record
    for needle in ("builtins.ValueError", "builtins.KeyError", 'File "'):
        assert needle in record["exception"]


def test_exception_chain_types_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log = collide_logging.get_logger("t")
    try:
        raise ExceptionGroup("canary-g", [TypeError("canary-t"), OSError("canary-o")])
    except ExceptionGroup:
        log.exception("x.failed")

    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception_type"] == "builtins.ExceptionGroup"
    for needle in ("builtins.TypeError", "builtins.OSError"):
        assert needle in record["exception"]


def test_safe_attributes_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    log = collide_logging.get_logger("t")

    http_err = RuntimeError("canary-h")
    http_err.response = SimpleNamespace(status_code=503)  # type: ignore[attr-defined]
    try:
        raise http_err
    except RuntimeError:
        log.exception("http.failed")
    record = _record(capsys)
    assert record["status_code"] == 503

    db_err = RuntimeError("canary-d")
    db_err.diag = SimpleNamespace(sqlstate="23505", constraint_name="uniq_x")  # type: ignore[attr-defined]
    try:
        raise db_err
    except RuntimeError:
        log.exception("db.failed")
    record = _record(capsys)
    assert record["sqlstate"] == "23505"
    assert record["constraint"] == "uniq_x"


def test_safe_attributes_do_not_overwrite_caller_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="svc", json=True)
    err = RuntimeError("x")
    err.response = SimpleNamespace(status_code=503)  # type: ignore[attr-defined]
    try:
        raise err
    except RuntimeError:
        collide_logging.get_logger("t").exception("up.failed", status_code=502)
    assert _record(capsys)["status_code"] == 502


def test_non_int_status_code_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    err = RuntimeError("x")
    err.response = SimpleNamespace(status_code="canary-503")  # type: ignore[attr-defined]
    try:
        raise err
    except RuntimeError:
        collide_logging.get_logger("t").exception("http.failed")
    out = capsys.readouterr().out
    assert "canary-" not in out
    assert "status_code" not in json_lib.loads(out.strip().splitlines()[-1])


def test_cyclic_chain_terminates(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    a, b = ValueError("canary-a"), KeyError("canary-b")
    a.__context__ = b
    b.__context__ = a
    try:
        raise a
    except ValueError:
        collide_logging.get_logger("t").exception("loop.failed")
    record = _record(capsys)
    assert record["exception"].count("builtins.KeyError") == 1


def test_foreign_record_exception_rendered_by_type(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    try:
        raise ValueError("canary-boom")
    except ValueError:
        logging.getLogger("lib").exception("boom")

    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception_type"] == "builtins.ValueError"
    assert 'File "' in record["exception"]
    assert "exc_info" not in record


def test_non_str_sqlstate_falls_back_to_diag(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    err = RuntimeError("x")
    err.sqlstate = 12345  # type: ignore[attr-defined]
    err.diag = SimpleNamespace(sqlstate="23505")  # type: ignore[attr-defined]
    try:
        raise err
    except RuntimeError:
        collide_logging.get_logger("t").exception("db.failed")
    assert _record(capsys)["sqlstate"] == "23505"


def test_long_chain_is_capped(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    exc: BaseException = ValueError("canary-0")
    for i in range(_MAX_EXCEPTION_CHAIN * 2):
        nxt = ValueError(f"canary-{i + 1}")
        nxt.__cause__ = exc
        exc = nxt
    try:
        raise exc
    except ValueError:
        collide_logging.get_logger("t").exception("deep.failed")
    out = capsys.readouterr().out
    assert "canary-" not in out
    record = json_lib.loads(out.strip().splitlines()[-1])
    assert record["exception"].count("builtins.ValueError") == _MAX_EXCEPTION_CHAIN


class _InnerError(Exception):
    pass


class _OuterError(Exception):
    pass


def _raise_inner() -> None:
    raise _InnerError("inner-canary")


def _raise_chained() -> None:
    try:
        _raise_inner()
    except _InnerError as inner:
        raise _OuterError("outer-canary") from inner


def _caught_chained() -> _OuterError:
    try:
        _raise_chained()
    except _OuterError as exc:
        return exc
    raise AssertionError("unreachable")


def _assert_full_traceback(record: dict[str, Any], level: str) -> None:
    assert record["level"] == level
    assert record["exception_type"] == f"{__name__}._OuterError"
    rendered = record["exception"]
    assert re.search(r'File ".*test_exceptions\.py", line \d+, in _raise_inner', rendered)
    assert re.search(r'File ".*test_exceptions\.py", line \d+, in _raise_chained', rendered)
    assert rendered.index(f"{__name__}._InnerError") < rendered.index(f"{__name__}._OuterError")
    assert "canary" not in json_lib.dumps(record)


def test_warning_with_exception_object_keeps_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression guard: a warning line given an exception object keeps its full traceback."""
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").warning("x.retrying", exc_info=_caught_chained())
    _assert_full_traceback(_record(capsys), "warning")


def test_warning_with_exc_info_true_keeps_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression guard: a warning line with ``exc_info=True`` keeps its full traceback."""
    collide_logging.configure(service="svc", json=True)
    try:
        _raise_chained()
    except _OuterError:
        collide_logging.get_logger("t").warning("x.retrying", exc_info=True)
    _assert_full_traceback(_record(capsys), "warning")


def test_error_with_chained_exception_keeps_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression guard: an error line keeps the frames and cause chain of ``raise B from A``."""
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").error("x.failed", exc_info=_caught_chained())
    _assert_full_traceback(_record(capsys), "error")


def test_info_with_exc_info_keeps_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression guard: an info line carrying an exception keeps its full traceback."""
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").info("x.skipped", exc_info=_caught_chained())
    _assert_full_traceback(_record(capsys), "info")


_LINE_LIMIT = 16 * 1024
_FRAME_LINE = re.compile(r'^  File ".*", line \d+, in (\w+)$')
_ELIDED = re.compile(r"^  \.\.\. (\d+) frames elided$")


def _recurse(depth: int) -> None:
    if depth == 0:
        raise _InnerError("recursion-canary")
    _recurse(depth - 1)


def _raised(func: Any, *args: Any) -> BaseException:
    try:
        func(*args)
    except BaseException as exc:
        return exc
    raise AssertionError("did not raise")


def _deep_chain(links: int, depth: int) -> BaseException:
    """``links`` exceptions, each raised from the previous one at the bottom of ``depth`` frames."""
    error = _raised(_recurse, depth)
    for _ in range(links - 1):
        error = _raised(_reraise_deep, depth, error)
    return error


def _reraise_deep(depth: int, cause: BaseException) -> None:
    if depth == 0:
        raise _OuterError("wrapper-canary") from cause
    _reraise_deep(depth - 1, cause)


def _log_and_read(
    capsys: pytest.CaptureFixture[str], error: BaseException
) -> tuple[str, dict[str, Any]]:
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").warning("x.failed", exc_info=error, operation="probe")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    return line, json_lib.loads(line)


def _blocks(rendered: str) -> list[list[str]]:
    return [block.splitlines() for block in rendered.split("\n\n")]


def _pre_cap_rendering(exc: BaseException) -> str:
    """The ``exception`` field as rendered before the cap existed."""
    parts: list[str] = []
    for item in reversed(_exception_chain(exc)):
        frames = traceback.StackSummary.extract(
            traceback.walk_tb(item.__traceback__), lookup_lines=False
        )
        lines = [
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n' for frame in frames
        ]
        lines.append(f"{_type_name(item)}\n")
        if isinstance(item, BaseExceptionGroup):
            lines.extend(f"  {_type_name(member)}\n" for member in item.exceptions)
        parts.append("".join(lines))
    return "\n".join(parts).rstrip("\n")


def test_short_traceback_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    error = _deep_chain(links=5, depth=3)
    _, record = _log_and_read(capsys, error)
    assert record["exception"] == _pre_cap_rendering(error)
    blocks = _blocks(record["exception"])
    assert len(blocks) == 5
    assert all(_FRAME_LINE.match(block[0]) for block in blocks)


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (0, ["  ... 5 frames elided\n"]),
        (1, ["  ... 4 frames elided\n", "e"]),
        (2, ["a", "  ... 3 frames elided\n", "e"]),
        (4, ["a", "b", "  ... 1 frames elided\n", "d", "e"]),
        (5, ["a", "b", "c", "d", "e"]),
    ],
)
def test_elision_keeps_the_raise_site_first(limit: int, expected: list[str]) -> None:
    assert _elided(["a", "b", "c", "d", "e"], limit) == expected


def test_deep_recursion_keeps_first_and_last_frames(capsys: pytest.CaptureFixture[str]) -> None:
    line, record = _log_and_read(capsys, _raised(_recurse, 500))
    (block,) = _blocks(record["exception"])
    assert block[-1] == f"{__name__}._InnerError"
    assert record["exception_type"] == f"{__name__}._InnerError"
    frames = [_FRAME_LINE.match(text) for text in block[:-1]]
    elided = [_ELIDED.match(text) for text in block[:-1]]
    marker_at = next(index for index, match in enumerate(elided) if match)
    assert all(frames[index] for index in range(len(frames)) if index != marker_at)
    assert frames[0] is not None and frames[0].group(1) == "_raised"
    assert frames[-1] is not None and frames[-1].group(1) == "_recurse"
    kept = len(block) - 2
    assert kept + int(elided[marker_at].group(1)) == 502  # _raised plus 501 _recurse frames
    assert len(line.encode()) < _LINE_LIMIT


def test_long_cause_chain_keeps_frames_for_the_raised_exception_and_root_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = _deep_chain(links=12, depth=200)
    line, record = _log_and_read(capsys, error)
    assert len(line.encode()) < _LINE_LIMIT
    assert record["exception_type"] == f"{__name__}._OuterError"
    blocks = _blocks(record["exception"])
    assert [block[-1] for block in blocks] == [f"{__name__}._InnerError"] + [
        f"{__name__}._OuterError"
    ] * 11
    # 202 frames each: _raised plus 201 recursive frames. The ends keep 16, the middle links none.
    for block in (blocks[0], blocks[-1]):
        assert len(block) == 18
        assert _FRAME_LINE.match(block[0])
        assert _FRAME_LINE.match(block[-2])
        assert [text for text in block if _ELIDED.match(text)] == ["  ... 186 frames elided"]
    for block in blocks[1:-1]:
        assert block == ["  ... 202 frames elided", f"{__name__}._OuterError"]


def test_three_deep_chain_of_deep_stacks_keeps_frames_for_each(
    capsys: pytest.CaptureFixture[str],
) -> None:
    line, record = _log_and_read(capsys, _deep_chain(links=3, depth=300))
    assert len(line.encode()) < _LINE_LIMIT
    for block in _blocks(record["exception"]):
        assert _FRAME_LINE.match(block[0])
        assert _FRAME_LINE.match(block[-2])
        assert sum(1 for text in block if _ELIDED.match(text)) == 1


def test_long_paths_shrink_frames_to_stay_under_the_cap(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _deep_chain(links=4, depth=300)
    long_name = "/opt/" + "very-long-site-packages-directory/" * 20 + "module.py"
    original = traceback.FrameSummary.__init__

    def long_filename(self: Any, filename: str, *args: Any, **kwargs: Any) -> None:
        original(self, long_name, *args, **kwargs)

    monkeypatch.setattr(traceback.FrameSummary, "__init__", long_filename)
    line, record = _log_and_read(capsys, error)
    assert len(line.encode()) < _LINE_LIMIT
    blocks = _blocks(record["exception"])
    assert [block[-1] for block in blocks] == [f"{__name__}._InnerError"] + [
        f"{__name__}._OuterError"
    ] * 3
    assert record["exception_type"] == f"{__name__}._OuterError"
    assert long_name in record["exception"]
    # The middle links give up their frames before the raised exception and root cause do.
    for block in (blocks[0], blocks[-1]):
        assert _FRAME_LINE.match(block[0])
        assert _FRAME_LINE.match(block[-2])
    for block in blocks[1:-1]:
        assert block == ["  ... 302 frames elided", f"{__name__}._OuterError"]


def test_huge_exception_group_keeps_its_type_line(capsys: pytest.CaptureFixture[str]) -> None:
    group = ExceptionGroup("canary-g", [ValueError(f"canary-{i}") for i in range(2000)])
    line, record = _log_and_read(capsys, _raised(_raise_group, group))
    assert len(line.encode()) < _LINE_LIMIT
    assert record["exception"].endswith("\nbuiltins.ExceptionGroup\n  ... 2000 members elided")
    assert record["exception_type"] == "builtins.ExceptionGroup"


_LongNamedError = type("E" * 200, (Exception,), {})


def test_a_long_chain_of_long_type_names_holds_the_bound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _LongNamedError()
    error: BaseException = root
    for _ in range(_MAX_EXCEPTION_CHAIN * 2):
        outer = _LongNamedError()
        outer.__cause__ = error
        error = outer
    line, record = _log_and_read(capsys, _raised(_raise_group, error))
    rendered = record["exception"]
    assert len(rendered) <= _MAX_EXCEPTION_CHARS
    assert len(line.encode()) < _LINE_LIMIT
    long_name = f"{__name__}.{'E' * 200}"
    assert record["exception_type"] == long_name
    blocks = _blocks(rendered)
    assert blocks[0] == [long_name]
    (marker,) = blocks[1]
    match = re.fullmatch(r"  \.\.\. (\d+) earlier exceptions elided", marker)
    assert match is not None
    assert len(blocks) - 1 + int(match.group(1)) == _MAX_EXCEPTION_CHAIN  # root, marker, kept
    assert blocks[-1] == ["  ... 2 frames elided", long_name]


def _raise_group(group: BaseException) -> None:
    raise group
