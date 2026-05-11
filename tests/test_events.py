from __future__ import annotations

import hashlib
import json as json_lib
import logging
from collections.abc import Iterator
from typing import Any

import pytest

import collide_logging
from collide_logging.events import _reset_registry
from collide_logging.testing import assert_collide_v1


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("COLLIDE_LOG_VALIDATE", raising=False)
    _reset_registry()
    yield
    _reset_registry()
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _last_json(captured: str) -> dict[str, Any]:
    line = captured.strip().splitlines()[-1]
    return json_lib.loads(line)  # type: ignore[no-any-return]


def _all_json(captured: str) -> list[dict[str, Any]]:
    return [json_lib.loads(line) for line in captured.strip().splitlines()]


def _simple_schema() -> collide_logging.EventSchema:
    return collide_logging.EventSchema(
        name="demo.thing",
        fields={
            "user_id": collide_logging.FieldSpec(type=str, required=True),
            "extra": collide_logging.FieldSpec(type=str),
        },
        description="demo event",
    )


def test_register_and_emit_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(_simple_schema())

    log = collide_logging.get_logger("t.m")
    log.event("demo.thing", user_id="u_123", extra="ok")

    record = _last_json(capsys.readouterr().out)
    assert_collide_v1(record)
    assert record["event"] == "demo.thing"
    assert record["user_id"] == "u_123"
    assert record["extra"] == "ok"


def test_wrapper_class_is_collide_logger_after_bind() -> None:
    """get_logger returns a lazy proxy; after .bind() it materializes into
    the configured wrapper_class. Confirms configure() wired CollideLogger."""
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m").bind()
    assert isinstance(log, collide_logging.CollideLogger)
    assert callable(log.event)


def test_idempotent_registration() -> None:
    collide_logging.register_event_schema(_simple_schema())
    collide_logging.register_event_schema(_simple_schema())
    schemas = collide_logging.list_schemas()
    assert len(schemas) == 1
    assert schemas[0].name == "demo.thing"


def test_conflicting_registration_raises() -> None:
    collide_logging.register_event_schema(_simple_schema())
    with pytest.raises(ValueError, match=r"demo\.thing"):
        collide_logging.register_event_schema(
            collide_logging.EventSchema(
                name="demo.thing",
                fields={"different": collide_logging.FieldSpec(type=str)},
            )
        )


def test_unknown_event_raises_in_dev_mode() -> None:
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    with pytest.raises(collide_logging.EventValidationError, match="unknown_event"):
        log.event("never.registered")


def test_missing_required_raises_in_dev_mode() -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(_simple_schema())
    log = collide_logging.get_logger("t.m")
    with pytest.raises(collide_logging.EventValidationError, match="missing_required"):
        log.event("demo.thing", extra="ok")


def test_unknown_field_raises_in_dev_mode() -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(_simple_schema())
    log = collide_logging.get_logger("t.m")
    with pytest.raises(collide_logging.EventValidationError, match="unknown_field"):
        log.event("demo.thing", user_id="u", bogus="x")


def test_lenient_mode_unknown_event_emits_meta_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLLIDE_LOG_VALIDATE", "lenient")
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    log.event("never.registered", x=1)

    record = _last_json(capsys.readouterr().out)
    assert record["event"] == "collide_logging.schema_violation"
    assert record["violation"] == "unknown_event"
    assert record["schema"] == "never.registered"


def test_lenient_mode_missing_required_emits_meta_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLLIDE_LOG_VALIDATE", "lenient")
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(_simple_schema())
    log = collide_logging.get_logger("t.m")
    log.event("demo.thing", extra="ok")

    record = _last_json(capsys.readouterr().out)
    assert record["event"] == "collide_logging.schema_violation"
    assert record["violation"] == "missing_required"
    assert record["schema"] == "demo.thing"
    assert record["missing"] == ["user_id"]


def test_lenient_mode_unknown_field_emits_meta_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLLIDE_LOG_VALIDATE", "lenient")
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(_simple_schema())
    log = collide_logging.get_logger("t.m")
    log.event("demo.thing", user_id="u", bogus="x", other="y")

    record = _last_json(capsys.readouterr().out)
    assert record["event"] == "collide_logging.schema_violation"
    assert record["violation"] == "unknown_field"
    assert record["schema"] == "demo.thing"
    assert record["unknown"] == ["bogus", "other"]


def test_redact_flagged_string_field(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(
        collide_logging.EventSchema(
            name="demo.secret",
            fields={
                "user_id": collide_logging.FieldSpec(type=str, required=True),
                "payload": collide_logging.FieldSpec(type=str, redact=True),
            },
        )
    )
    log = collide_logging.get_logger("t.m")
    log.event("demo.secret", user_id="u", payload="hunter2")

    record = _last_json(capsys.readouterr().out)
    expected_hash = hashlib.sha256(b"hunter2").hexdigest()[:8]
    assert record["payload"] == {"len": 7, "sha256": expected_hash}
    assert record["user_id"] == "u"


def test_redact_handles_non_string_value(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(
        collide_logging.EventSchema(
            name="demo.number",
            fields={"value": collide_logging.FieldSpec(type=int, redact=True)},
        )
    )
    log = collide_logging.get_logger("t.m")
    log.event("demo.number", value=12345)

    record = _last_json(capsys.readouterr().out)
    expected_hash = hashlib.sha256(b"12345").hexdigest()[:8]
    assert record["value"] == {"len": 5, "sha256": expected_hash}


def test_redact_handles_bytes_value(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(
        collide_logging.EventSchema(
            name="demo.bytes",
            fields={"blob": collide_logging.FieldSpec(type=bytes, redact=True)},
        )
    )
    log = collide_logging.get_logger("t.m")
    log.event("demo.bytes", blob=b"\x00\x01\x02\x03")

    record = _last_json(capsys.readouterr().out)
    expected_hash = hashlib.sha256(b"\x00\x01\x02\x03").hexdigest()[:8]
    assert record["blob"] == {"len": 4, "sha256": expected_hash}


def test_list_schemas_sorted_by_name() -> None:
    collide_logging.register_event_schema(
        collide_logging.EventSchema(name="zeta", fields={})
    )
    collide_logging.register_event_schema(
        collide_logging.EventSchema(name="alpha", fields={})
    )
    collide_logging.register_event_schema(
        collide_logging.EventSchema(name="mu", fields={})
    )
    names = [s.name for s in collide_logging.list_schemas()]
    assert names == ["alpha", "mu", "zeta"]


def test_explicit_raise_mode_matches_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLLIDE_LOG_VALIDATE", "raise")
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    with pytest.raises(collide_logging.EventValidationError):
        log.event("never.registered")
    assert capsys.readouterr().out == ""


def test_event_record_picks_up_bound_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Events ride the standard processor chain, so bound context vars apply."""
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(_simple_schema())
    log = collide_logging.get_logger("t.m")
    with collide_logging.bind_worker_run_id("run_xyz"):
        log.event("demo.thing", user_id="u")

    record = _last_json(capsys.readouterr().out)
    assert record["worker_run_id"] == "run_xyz"
    assert record["event"] == "demo.thing"


def test_global_suffix_redaction_still_applies_on_top(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-redact-flagged field whose name matches *_token still gets the
    global string redaction."""
    collide_logging.configure(service="t", json=True)
    collide_logging.register_event_schema(
        collide_logging.EventSchema(
            name="demo.mixed",
            fields={
                "user_id": collide_logging.FieldSpec(type=str, required=True),
                "api_token": collide_logging.FieldSpec(type=str),
            },
        )
    )
    log = collide_logging.get_logger("t.m")
    log.event("demo.mixed", user_id="u", api_token="ghp_abc")

    record = _last_json(capsys.readouterr().out)
    assert record["api_token"] == "***REDACTED***"


def test_lenient_mode_no_emission_on_unknown_event_records_only_meta(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The offending record itself is dropped; only the meta-event is emitted."""
    monkeypatch.setenv("COLLIDE_LOG_VALIDATE", "lenient")
    collide_logging.configure(service="t", json=True)
    log = collide_logging.get_logger("t.m")
    log.event("never.registered", user_id="u")

    records = _all_json(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["event"] == "collide_logging.schema_violation"
