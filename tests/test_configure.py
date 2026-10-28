from __future__ import annotations

import ipaddress
import json as json_lib
import logging
import re
import uuid
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
import structlog

import collide_logging


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _last_json_line(captured: str) -> dict[str, Any]:
    line = captured.strip().splitlines()[-1]
    return json_lib.loads(line)  # type: ignore[no-any-return]


def test_emits_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    log = collide_logging.get_logger("test.module")
    log.info("test.event")

    record = _last_json_line(capsys.readouterr().out)
    for field in ("timestamp", "level", "service", "logger", "event"):
        assert field in record, f"missing required field: {field}"
    assert record["level"] == "info"
    assert record["service"] == "test-svc"
    assert record["logger"] == "test.module"
    assert record["event"] == "test.event"


def test_timestamp_is_iso_with_z(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("e")

    record = _last_json_line(capsys.readouterr().out)
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$",
        record["timestamp"],
    ), f"timestamp does not match spec format: {record['timestamp']!r}"


def test_service_field_matches_arg(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="alpha", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("e")

    record = _last_json_line(capsys.readouterr().out)
    assert record["service"] == "alpha"


def test_redaction_active(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("auth.event", api_key="hunter2", github_token="ghp_abc")

    record = _last_json_line(capsys.readouterr().out)
    assert record["api_key"] == "***REDACTED***"
    assert record["github_token"] == "***REDACTED***"


def test_extra_redact_keys_honored(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(
        service="t",
        json=True,
        extra_redact_keys=["my_special_key"],
    )
    log = collide_logging.get_logger("t.m")
    log.info("e", my_special_key="x")

    record = _last_json_line(capsys.readouterr().out)
    assert record["my_special_key"] == "***REDACTED***"


def test_console_renderer_when_json_false(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=False)
    log = collide_logging.get_logger("t.m")
    log.info("test.event")

    out = capsys.readouterr().out.strip()
    with pytest.raises(json_lib.JSONDecodeError):
        json_lib.loads(out)
    assert "test.event" in out


def test_user_supplied_service_field_is_preserved(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="default-svc", json=True)
    log = collide_logging.get_logger("t.m")
    log.info("e", service="explicit-override")

    record = _last_json_line(capsys.readouterr().out)
    assert record["service"] == "explicit-override"


def test_idempotent_does_not_pile_up_handlers() -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.configure(service="t", json=True)
    collide_logging.configure(service="t", json=True)

    root = logging.getLogger()
    tagged = [h for h in root.handlers if getattr(h, "_collide_logging_handler", False)]
    assert len(tagged) == 1


def test_exception_logs_traceback_at_error_level(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    log = collide_logging.get_logger("test.module")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("test.failed", attempt=2)

    record = _last_json_line(capsys.readouterr().out)
    assert record["level"] == "error"
    assert record["attempt"] == 2
    assert "builtins.ValueError" in record["exception"]
    assert "boom" not in record["exception"]
    assert 'File "' in record["exception"]


def test_positional_args_do_not_raise_and_render_as_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="test-svc", json=True)
    log: Any = collide_logging.get_logger("t")
    log.info("a %s", "b")

    record = _last_json_line(capsys.readouterr().out)
    assert record["message"] == "a %s"
    assert record["positional_args_count"] == 1
    assert "positional_args" not in record


def test_positional_args_values_never_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = "CANARY-positional-7f3a"
    collide_logging.configure(service="test-svc", json=True)
    log: Any = collide_logging.get_logger("t")
    log.info("x.y", sentinel)
    record = _last_json_line(capsys.readouterr().out)
    assert sentinel not in json_lib.dumps(record)
    assert record["positional_args_count"] == 1

    log.info("x.y", note=sentinel)
    assert sentinel in capsys.readouterr().out


def test_uuid_renders_as_canonical_string(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="test-svc", json=True)
    collide_logging.get_logger("test.module").info("a.b", user_id=uuid.UUID(int=1))

    out = capsys.readouterr().out
    assert _last_json_line(out)["user_id"] == "00000000-0000-0000-0000-000000000001"
    assert "UUID(" not in out


def test_structlog_hook_object_renders_via_hook(capsys: pytest.CaptureFixture[str]) -> None:
    class Hooked:
        def __structlog__(self) -> str:
            return "custom"

    collide_logging.configure(service="test-svc", json=True)
    collide_logging.get_logger("test.module").info("a.b", thing=Hooked())

    assert _last_json_line(capsys.readouterr().out)["thing"] == "custom"


def test_ordinary_object_renders_as_type_name(capsys: pytest.CaptureFixture[str]) -> None:
    class Plain:
        def __repr__(self) -> str:
            return "Plain<1>"

    collide_logging.configure(service="test-svc", json=True)
    collide_logging.get_logger("test.module").info("a.b", thing=Plain())

    out = capsys.readouterr().out
    assert _last_json_line(out)["thing"] == "<Plain>"
    assert "Plain<1>" not in out


def test_object_with_raising_repr_renders_type_name(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    collide_logging.configure(service="test-svc", json=True)
    # pytest's capture handlers str() the event dict, which would hit the broken repr themselves.
    root = logging.getLogger()
    monkeypatch.setattr(
        root,
        "handlers",
        [h for h in root.handlers if getattr(h, "_collide_logging_handler", False)],
    )
    collide_logging.get_logger("test.module").info("a.b", thing=Broken())

    assert _last_json_line(capsys.readouterr().out)["thing"] == "<Broken>"


def _spy_on_processor_chain() -> list[str]:
    """Prepend a processor that records the event of every call the chain runs for."""
    seen: list[str] = []

    def spy(logger: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> Any:
        seen.append(event_dict["event"])
        return event_dict

    structlog.configure(processors=[spy, *structlog.get_config()["processors"]])
    return seen


def test_filtered_line_skips_processor_chain(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, level="info")
    seen = _spy_on_processor_chain()
    log = collide_logging.get_logger("t.m")

    log.debug("x.filtered")
    log.info("x.emitted")

    assert seen == ["x.emitted"]
    assert [r["event"] for r in map(json_lib.loads, capsys.readouterr().out.splitlines())] == [
        "x.emitted"
    ]


@pytest.mark.parametrize(
    ("level", "emitted"),
    [
        ("warning", ["x.warning", "x.error", "x.exception", "x.critical"]),
        ("error", ["x.error", "x.exception", "x.critical"]),
        ("critical", ["x.critical"]),
    ],
)
def test_level_filter_keeps_enabled_lines_carrying_exceptions(
    capsys: pytest.CaptureFixture[str], level: str, emitted: list[str]
) -> None:
    collide_logging.configure(service="t", json=True, level=level)
    seen = _spy_on_processor_chain()
    log = collide_logging.get_logger("t.m")
    exc = ValueError("boom")

    log.info("x.info", exc_info=exc)
    log.warning("x.warning", exc_info=exc)
    log.error("x.error", exc_info=exc)
    try:
        raise exc
    except ValueError:
        log.exception("x.exception")
    log.critical("x.critical", exc_info=exc)

    assert seen == emitted
    records = [json_lib.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["event"] for r in records] == emitted
    assert all(r["exception_type"] == "builtins.ValueError" for r in records)


def test_strict_mode_skips_filtered_lines(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, level="info", strict=True)
    collide_logging.get_logger("t.m").debug("Not An Event Name")  # type: ignore[arg-type]
    assert capsys.readouterr().out == ""


@dataclass
class _Turn:
    user_text: str


def test_dataclass_field_value_never_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.get_logger("t.m").info(
        "turn.done", turn=_Turn("PRIVATE-TEXT"), nested={"items": [_Turn("PRIVATE-TEXT")]}
    )

    out = capsys.readouterr().out
    assert "PRIVATE-TEXT" not in out
    record = _last_json_line(out)
    assert record["turn"] == "<_Turn>"
    assert record["nested"] == {"items": ["<_Turn>"]}


def test_exception_field_value_never_renders_message(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.get_logger("t.m").info("x.done", err=ValueError("SECRET-MESSAGE"))

    out = capsys.readouterr().out
    assert "SECRET-MESSAGE" not in out
    assert _last_json_line(out)["err"] == "<ValueError>"


def test_set_field_value_renders_members(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.get_logger("t.m").info("x.done", tags={"a"}, frozen=frozenset({"b"}))

    record = _last_json_line(capsys.readouterr().out)
    assert record["tags"] == ["a"]
    assert record["frozen"] == ["b"]


def test_console_renderer_never_renders_object_repr(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=False)
    collide_logging.get_logger("t.m").info(
        "turn.done",
        turn=_Turn("PRIVATE-TEXT"),
        err=ValueError("SECRET-MESSAGE"),
        nested={"items": [_Turn("PRIVATE-TEXT")]},
    )

    out = capsys.readouterr().out
    assert "turn.done" in out
    assert "PRIVATE-TEXT" not in out
    assert "SECRET-MESSAGE" not in out
    assert "<_Turn>" in out


def test_non_string_event_never_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.get_logger("t.m").info(ValueError("SECRET-MESSAGE"))  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "SECRET-MESSAGE" not in out
    record = _last_json_line(out)
    assert record["event"] == "log.message"
    assert record["message"] == "<ValueError>"


def test_foreign_record_url_credentials_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    logging.getLogger("azure.core.pipeline").warning(
        "Request URL: 'https://acct.blob.core.windows.net/c/b?sv=2022-11-02&se=2026-10-28"
        "&sig=SECRETSIG%2Fabc%3D'"
    )

    out = capsys.readouterr().out
    assert "SECRETSIG" not in out
    assert _last_json_line(out)["message"] == (
        "Request URL: 'https://acct.blob.core.windows.net/c/b?sv=2022-11-02&se=2026-10-28"
        "&sig=***REDACTED***'"
    )


@pytest.mark.parametrize("text", ["SECRET MESSAGE TEXT", "secret.event"])
def test_foreign_exception_message_never_rendered(
    capsys: pytest.CaptureFixture[str], text: str
) -> None:
    collide_logging.configure(service="t", json=True)
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise ValueError(text) from inner
    except ValueError as exc:
        caught = exc
    logging.getLogger("some.lib").error(caught)

    out = capsys.readouterr().out
    assert text not in out
    record = _last_json_line(out)
    assert (record["event"], record["message"]) == ("log.message", "<ValueError>")
    assert record["exception_type"] == "builtins.ValueError"
    assert re.search(
        r'File ".*test_configure\.py", line \d+, in test_foreign_exception', record["exception"]
    )
    assert "builtins.KeyError" in record["exception"]


def test_foreign_non_string_message_never_rendered(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    logging.getLogger("some.lib").warning(_Turn("PRIVATE-TEXT"))

    out = capsys.readouterr().out
    assert "PRIVATE-TEXT" not in out
    record = _last_json_line(out)
    assert (record["event"], record["message"]) == ("log.message", "<_Turn>")


class _Colour(Enum):
    RED = "red"
    BLUE = 2


class _Shape(Enum):
    SQUARE = _Turn("PRIVATE-TEXT")


def test_readable_values_render_safely(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.get_logger("t.m").info(
        "x.done",
        at=datetime(2026, 10, 27, 12, 30, tzinfo=UTC),
        day=date(2026, 10, 27),
        clock=time(12, 30),
        amount=Decimal("1.50"),
        not_a_number=Decimal("NaN"),
        infinite=Decimal("-Infinity"),
        colour=_Colour.RED,
        other=_Colour.BLUE,
        shape=_Shape.SQUARE,
        raw=b"PRIVATE-BYTES",
        wait=timedelta(minutes=1, milliseconds=500),
        file=Path("/customers/PRIVATE-NAME.csv"),
        ip=ipaddress.ip_address("10.1.2.3"),
    )

    out = capsys.readouterr().out
    assert "PRIVATE" not in out
    record = _last_json_line(out)
    assert record["at"] == "2026-10-27T12:30:00+00:00"
    assert record["day"] == "2026-10-27"
    assert record["clock"] == "12:30:00"
    assert record["amount"] == "1.50"
    assert record["not_a_number"] == "<Decimal>"
    assert record["infinite"] == "<Decimal>"
    assert record["colour"] == "red"
    assert record["other"] == 2
    assert record["shape"] == "<_Shape>"
    assert record["raw"] == "<bytes>"
    assert record["wait"] == 60.5
    assert record["file"] == "<PosixPath>"
    assert record["ip"] == "<IPv4Address>"
    assert "10.1.2.3" not in out


class _HookedColour(Enum):
    RED = "red"

    def __structlog__(self) -> str:
        return "hooked"


def test_structlog_hook_wins_over_readable_rendering(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.get_logger("t.m").info("x.done", colour=_HookedColour.RED)
    assert _last_json_line(capsys.readouterr().out)["colour"] == "hooked"


@pytest.fixture
def _restore_logger_state() -> Iterator[None]:
    child = logging.getLogger("t.child")
    yield
    child.setLevel(logging.NOTSET)
    child.disabled = False
    logging.disable(logging.NOTSET)


@pytest.mark.usefixtures("_restore_logger_state")
def test_child_logger_level_below_root_still_emits(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, level="info")
    logging.getLogger("t.child").setLevel(logging.DEBUG)
    seen = _spy_on_processor_chain()

    collide_logging.get_logger("t.child").debug("x.child_debug")
    collide_logging.get_logger("t.other").debug("x.other_debug")

    assert seen == ["x.child_debug"]
    assert _last_json_line(capsys.readouterr().out)["event"] == "x.child_debug"


@pytest.mark.usefixtures("_restore_logger_state")
def test_logging_disable_skips_processor_chain(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, level="info")
    logging.disable(logging.WARNING)
    seen = _spy_on_processor_chain()
    log = collide_logging.get_logger("t.m")

    log.warning("x.disabled")
    log.error("x.emitted")

    assert seen == ["x.emitted"]
    assert _last_json_line(capsys.readouterr().out)["event"] == "x.emitted"


@pytest.mark.usefixtures("_restore_logger_state")
def test_disabled_logger_emits_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True, level="info")
    logging.getLogger("t.child").disabled = True
    seen = _spy_on_processor_chain()

    collide_logging.get_logger("t.child").error("x.disabled")

    assert seen == []
    assert capsys.readouterr().out == ""


def test_bound_logger_skips_processor_chain_when_filtered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collide_logging.configure(service="t", json=True, level="info")
    seen = _spy_on_processor_chain()
    log = collide_logging.get_logger("t.m").bind(step="load")

    log.debug("x.filtered")
    log.info("x.emitted")

    assert seen == ["x.emitted"]
    assert _last_json_line(capsys.readouterr().out)["step"] == "load"
