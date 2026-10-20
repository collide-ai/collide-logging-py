from __future__ import annotations

import json as json_lib
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

import collide_logging
from collide_logging import _HANDLER_TAG, RESERVED_CONTEXT_FIELDS, StrictModeViolation
from collide_logging.testing import unit_of_work


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    collide_logging.configure(service="reset", json=True)
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]


def _strict(**kwargs: Any) -> None:
    kwargs.setdefault("first_party", ("app",))
    collide_logging.configure(service="t", json=True, strict=True, **kwargs)


def _last_json_line(captured: str) -> dict[str, Any]:
    return json_lib.loads(captured.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_reserved_fields_exported() -> None:
    assert {"request_id", "user_id", "trace_id"} <= RESERVED_CONTEXT_FIELDS
    assert issubclass(StrictModeViolation, AssertionError)


def test_strict_rejects_prose_event() -> None:
    _strict()
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        logging.getLogger("app.mod").info("Shutdown complete")


def test_strict_rejects_positional_args() -> None:
    _strict()
    with pytest.raises(StrictModeViolation, match="positional arguments are not allowed"):
        collide_logging.get_logger("app.mod").info("a.b", 1)  # type: ignore[call-arg]
    with pytest.raises(StrictModeViolation, match="positional arguments are not allowed"):
        logging.getLogger("app.mod").info("a.b %s", "x")


def test_strict_rejects_conflicting_context_value() -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id="7")
    conflict = "user_id conflicts with the bound context value"
    with pytest.raises(StrictModeViolation, match=conflict):
        collide_logging.get_logger("app.mod").info("a.b", user_id="5")
    with pytest.raises(StrictModeViolation, match=conflict):
        logging.getLogger("app.mod").info("a.b", extra={"user_id": "5"})
    with pytest.raises(StrictModeViolation, match=conflict):
        collide_logging.get_logger("app.mod").bind(user_id="5").info("a.b")


def test_strict_allows_same_value(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id="7")
    collide_logging.get_logger("app.mod").info("a.b", user_id="7")
    logging.getLogger("app.mod").info("a.c", extra={"user_id": "7"})
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json_lib.loads(x)["event"] for x in lines] == ["a.b", "a.c"]


def test_strict_allows_unbound_value(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    collide_logging.get_logger("app.mod").info("a.b", account_id="a")
    assert _last_json_line(capsys.readouterr().out)["account_id"] == "a"


def test_strict_rejects_message_and_task_kwargs() -> None:
    _strict()
    log = collide_logging.get_logger("app.mod")
    with pytest.raises(StrictModeViolation, match=r"reserved call-site field\(s\) \['message'\]"):
        log.info("a.b", message="x")
    with pytest.raises(StrictModeViolation, match=r"reserved call-site field\(s\) \['task'\]"):
        log.info("a.b", task="x")
    with pytest.raises(StrictModeViolation, match=r"reserved call-site field\(s\) \['task'\]"):
        logging.getLogger("app.mod").info("a.b", extra={"task": "x"})


def test_strict_allows_bound_context_task(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(task="pkg.mod.fn")
    collide_logging.get_logger("app.mod").info("a.b")
    logging.getLogger("app.mod").info("a.c")
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json_lib.loads(x)["task"] for x in lines] == ["pkg.mod.fn"] * 2


def test_strict_exempt_logger_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    _strict(strict_exempt=("app.legacy",))
    logging.getLogger("app.legacy.x").info("Hello there")
    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == "Hello there"


def test_strict_ignores_third_party_stdlib(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    logging.getLogger("lib").info("hello %s", "x")
    assert _last_json_line(capsys.readouterr().out)["message"] == "hello x"


def test_strict_survives_reconfigure() -> None:
    _strict()
    _strict()
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        logging.getLogger("app.mod").info("Shutdown complete")


def test_strict_off_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, first_party=("app",))
    collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    assert _last_json_line(capsys.readouterr().out)["event"] == "log.message"


def test_strict_allows_bind_matching_context(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id="7")
    collide_logging.get_logger("app.mod").bind(user_id="7").info("a.b")
    assert _last_json_line(capsys.readouterr().out)["user_id"] == "7"


def test_strict_same_rendered_value_passes(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    structlog.contextvars.bind_contextvars(user_id=5)
    collide_logging.get_logger("app.mod").info("a.b", user_id="5")
    logging.getLogger("app.mod").info("a.c", extra={"user_id": "5"})
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json_lib.loads(x)["event"] for x in lines] == ["a.b", "a.c"]


def test_strict_exempt_structlog_logger(capsys: pytest.CaptureFixture[str]) -> None:
    _strict(strict_exempt=("app.legacy",))
    collide_logging.get_logger("app.legacy.x").info("Hello there")  # type: ignore[arg-type]
    out = _last_json_line(capsys.readouterr().out)
    assert out["event"] == "log.message"
    assert out["message"] == "Hello there"


def test_strict_rejects_non_str_event() -> None:
    _strict()
    log: Any = collide_logging.get_logger("app.mod")
    with pytest.raises(StrictModeViolation, match="does not match the event-name rule"):
        log.info(42)


def test_strict_empty_first_party_skips_stdlib(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, strict=True)
    logging.getLogger("app.mod").info("Shutdown %s", "complete")
    assert _last_json_line(capsys.readouterr().out)["message"] == "Shutdown complete"


def test_reconfigure_without_strict_turns_it_off(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    collide_logging.configure(service="t", json=True, first_party=("app",))
    collide_logging.get_logger("app.mod").info("Shutdown complete")  # type: ignore[arg-type]
    logging.getLogger("app.mod").info("a.b %s", "x")
    events = [json_lib.loads(x)["event"] for x in capsys.readouterr().out.strip().splitlines()]
    assert events == ["log.message", "log.message"]


@pytest.mark.parametrize("parameter", ["first_party", "strict_exempt"])
def test_strict_rejects_str_prefix_arguments(parameter: str) -> None:
    with pytest.raises(TypeError, match=parameter):
        collide_logging.configure(service="t", json=True, strict=True, **{parameter: "app"})


@pytest.mark.parametrize(
    ("message", "args"), [("a.b", (1,)), ("a.b %s %s", (1,))], ids=["no-placeholder", "too-few"]
)
def test_strict_rejects_mismatched_stdlib_args(
    monkeypatch: pytest.MonkeyPatch, message: str, args: tuple[object, ...]
) -> None:
    _strict()
    root = logging.getLogger()
    # pytest's own root handler formats first and would raise its own TypeError.
    monkeypatch.setattr(root, "handlers", [h for h in root.handlers if hasattr(h, _HANDLER_TAG)])
    with pytest.raises(StrictModeViolation, match="positional arguments are not allowed"):
        logging.getLogger("app.mod").info(message, *args)


_NO_CONTEXT = "exception logged without a field describing the operation"


class _DeclaredError(Exception):
    def __log_fields__(self) -> dict[str, str]:
        return {"step": "declared"}


def _log_failure(logger_name: str = "app.mod", **fields: Any) -> None:
    log = collide_logging.get_logger(logger_name)
    try:
        raise ValueError("x")
    except ValueError:
        log.exception("x.failed", **fields)


def test_strict_bare_exception_fails() -> None:
    _strict()
    with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
        _log_failure()
    with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
        _log_failure(account_id="a")


def test_strict_exc_info_argument_counts_as_exception() -> None:
    _strict()
    log = collide_logging.get_logger("app.mod")
    try:
        raise ValueError("x")
    except ValueError:
        with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
            log.error("x.failed", exc_info=True)
        log.error("x.failed", exc_info=False)


def test_strict_exception_with_call_site_field_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _strict()
    _log_failure(step="s")
    assert _last_json_line(capsys.readouterr().out)["step"] == "s"


def test_strict_exception_with_log_context_passes(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    with collide_logging.log_context(step="s"):
        _log_failure()
    assert _last_json_line(capsys.readouterr().out)["step"] == "s"


def test_strict_exception_with_declared_fields_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _strict()
    log = collide_logging.get_logger("app.mod")
    try:
        raise _DeclaredError("x")
    except _DeclaredError:
        log.exception("x.failed")
    assert _last_json_line(capsys.readouterr().out)["step"] == "declared"


def test_strict_exception_with_captured_context_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _strict()
    log = collide_logging.get_logger("app.mod")
    try:
        with collide_logging.log_context(step="s"):
            raise ValueError("x")
    except ValueError:
        log.exception("x.failed")
    assert _last_json_line(capsys.readouterr().out)["step"] == "s"


def test_strict_builtin_exception_attributes_do_not_count() -> None:
    _strict()

    class _HttpError(Exception):
        status_code = 500

    log = collide_logging.get_logger("app.mod")
    try:
        raise _HttpError("x")
    except _HttpError:
        with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
            log.exception("x.failed")


def test_strict_bound_identity_is_not_operation_context() -> None:
    _strict()
    structlog.contextvars.bind_contextvars(account_id="a", task="t.f", task_id="1")
    with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
        _log_failure()


def test_strict_exception_in_django_q_task_with_only_bound_ids_fails() -> None:
    _strict()
    structlog.contextvars.bind_contextvars(task="t.f", task_id="1", worker_run_id="w")
    with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
        _log_failure()


def test_strict_exception_not_required_without_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _strict()
    collide_logging.get_logger("app.mod").error("x.failed")
    assert _last_json_line(capsys.readouterr().out)["event"] == "x.failed"


def test_strict_stdlib_exception_needs_context(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    stdlib = logging.getLogger("app.mod")
    try:
        raise ValueError("x")
    except ValueError:
        with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
            stdlib.exception("x.failed")
        stdlib.exception("x.failed", extra={"step": "s"})
    assert _last_json_line(capsys.readouterr().out)["step"] == "s"


def test_strict_exception_rule_ignores_third_party_stdlib(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _strict()
    try:
        raise ValueError("x")
    except ValueError:
        logging.getLogger("other.lib").exception("Prose message")
    line = _last_json_line(capsys.readouterr().out)
    assert (line["logger"], line["event"], line["message"]) == (
        "other.lib",
        "log.message",
        "Prose message",
    )


def test_strict_exception_rule_off_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    _log_failure()
    assert _last_json_line(capsys.readouterr().out)["event"] == "x.failed"


def test_strict_bound_operation_field_without_a_block_is_not_context() -> None:
    _strict()
    structlog.contextvars.bind_contextvars(step="x")
    with pytest.raises(StrictModeViolation, match=_NO_CONTEXT):
        _log_failure()


def test_strict_annotate_outside_unit_is_noop(capsys: pytest.CaptureFixture[str]) -> None:
    _strict()
    collide_logging.annotate(rows=1)
    collide_logging.annotate(**{"Bad-Name": 1})
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "fields",
    [{"Bad-Name": 1}, {"outcome": "x"}, {"rows": object()}, {"ratio": float("nan")}],
    ids=["malformed-name", "library-owned-name", "unsupported-value", "non-finite-float"],
)
def test_strict_annotate_rejects_bad_names_and_values(fields: dict[str, Any]) -> None:
    _strict()
    with unit_of_work() as work:
        with pytest.raises(StrictModeViolation, match="annotate"):
            collide_logging.annotate(**fields)
        assert work.annotations == {}


def test_strict_annotate_drops_identity_and_keywords_silently() -> None:
    _strict()
    with unit_of_work() as work:
        collide_logging.annotate(account_id="a", exc_info=True, rows=1)
    assert work.annotations == {"rows": 1}


def test_annotate_inside_unit_passes_strict() -> None:
    _strict()
    with unit_of_work() as work:
        collide_logging.annotate(rows=1)
    assert work.annotations == {"rows": 1}
