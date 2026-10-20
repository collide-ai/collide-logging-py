from __future__ import annotations

import enum
import json as json_lib
import logging
import math
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import collide_logging
from collide_logging._fields import LIBRARY_OWNED_NAMES, clean_fields


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    yield
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_collide_logging_handler", False)]


def _log_exception(capsys: pytest.CaptureFixture[str], error: BaseException) -> dict[str, Any]:
    collide_logging.configure(service="svc", json=True)
    try:
        raise error
    except BaseException:
        collide_logging.get_logger("t").exception("op.failed")
    out = capsys.readouterr().out
    assert "SENTINEL" not in out
    return json_lib.loads(out.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_http_attributes(capsys: pytest.CaptureFixture[str]) -> None:
    request = httpx.Request("GET", "https://api.example.com/v1/u/SENTINEL_PATH?t=SENTINEL_Q")
    response = httpx.Response(404, request=request)
    error = httpx.HTTPStatusError("SENTINEL", request=request, response=response)

    record = _log_exception(capsys, error)

    assert record["status_code"] == 404
    assert record["http_method"] == "GET"
    assert record["http_host"] == "api.example.com"


def test_db_attributes(capsys: pytest.CaptureFixture[str]) -> None:
    class StandInDbError(Exception):
        diag = SimpleNamespace(
            sqlstate="23505",
            constraint_name="uniq_email",
            schema_name="public",
            table_name="t" * 500,
            column_name="email",
            message_detail="SENTINEL",
            message_hint="SENTINEL",
            internal_query="SENTINEL",
        )

    record = _log_exception(capsys, StandInDbError("SENTINEL"))

    assert record["sqlstate"] == "23505"
    assert record["constraint"] == "uniq_email"
    assert record["db_schema"] == "public"
    assert record["db_table"] == "t" * 128
    assert record["db_column"] == "email"


def test_raising_request_keeps_diag_fields(capsys: pytest.CaptureFixture[str]) -> None:
    class RaisingRequestError(Exception):
        diag = SimpleNamespace(sqlstate="23505", table_name="accounts")

        @property
        def request(self) -> object:
            raise RuntimeError("SENTINEL")

    record = _log_exception(capsys, RaisingRequestError("SENTINEL"))

    assert (record["sqlstate"], record["db_table"]) == ("23505", "accounts")
    assert "http_method" not in record


def test_raising_diag_property_omits_only_that_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Diag:
        sqlstate = "23505"
        constraint_name = "uniq_email"

        @property
        def table_name(self) -> str:
            raise RuntimeError("SENTINEL")

    class RaisingDiagError(Exception):
        diag = Diag()

    record = _log_exception(capsys, RaisingDiagError("SENTINEL"))

    assert (record["sqlstate"], record["constraint"]) == ("23505", "uniq_email")
    assert "db_table" not in record


def test_raising_attribute_omits_only_that_field(capsys: pytest.CaptureFixture[str]) -> None:
    record = _log_exception(capsys, httpx.RequestError("SENTINEL"))

    assert record["event"] == "op.failed"
    assert record["exception_type"] == "httpx.RequestError"
    assert "http_method" not in record


def test_bool_status_code_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    error = RuntimeError("SENTINEL")
    error.response = SimpleNamespace(status_code=True)  # type: ignore[attr-defined]

    assert "status_code" not in _log_exception(capsys, error)


def test_clean_fields_applies_the_field_rule() -> None:
    identifier = uuid.UUID("12345678-1234-5678-1234-567812345678")
    raw = {
        "tenant": "acme",
        "attempt": 3,
        "ratio": 0.5,
        "ok": True,
        "nothing": None,
        "uuid_field": identifier,
        "long": "x" * 300,
        "Bad": 1,
        "1bad": 1,
        "bad-key": 1,
        5: "int key",
        "nested": {"a": 1},
        "items": [1],
        "blob": b"bytes",
        "exception_note": "x",
        "request_id": "owned",
    }

    cleaned = clean_fields(raw, limit=50)

    assert cleaned == {
        "tenant": "acme",
        "attempt": 3,
        "ratio": 0.5,
        "ok": True,
        "nothing": None,
        "uuid_field": "12345678-1234-5678-1234-567812345678",
        "long": "x" * 128,
    }


def test_clean_fields_caps_the_key_count() -> None:
    raw = {f"field_{index}": index for index in range(10)}

    assert list(clean_fields(raw, limit=3)) == ["field_0", "field_1", "field_2"]


class _DeclaringError(Exception):
    error_code = "drive.revoke_failed"

    def __init__(self, fields: object) -> None:
        super().__init__("SENTINEL")
        self._fields = fields

    def __log_fields__(self) -> object:
        if isinstance(self._fields, BaseException):
            raise self._fields
        return self._fields


@pytest.mark.parametrize("name", sorted(LIBRARY_OWNED_NAMES))
def test_owned_name_dropped_from_declared_fields(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    error = _DeclaringError({name: "SENTINEL_OWNED", "kept": "v"})

    record = _log_exception(capsys, error)

    assert record["kept"] == "v"
    assert "SENTINEL_OWNED" not in json_lib.dumps(record)


def test_declared_task_names_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    record = _log_exception(capsys, _DeclaringError({"task": "t", "task_id": "i", "kept": "v"}))

    assert record["kept"] == "v"
    assert "task" not in record
    assert "task_id" not in record


def test_huge_declared_int_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    record = _log_exception(capsys, _DeclaringError({"huge": 10**5000, "kept": "v"}))

    assert record["event"] == "op.failed"
    assert record["kept"] == "v"
    assert "huge" not in record


def test_declared_fields_capped_at_twenty(capsys: pytest.CaptureFixture[str]) -> None:
    record = _log_exception(capsys, _DeclaringError({f"k{index}": index for index in range(25)}))

    assert [name for name in record if name.startswith("k")] == [f"k{index}" for index in range(20)]


class _CountingMapping(dict):  # type: ignore[type-arg]
    def __init__(self, entries: dict[object, object]) -> None:
        super().__init__(entries)
        self.examined = 0

    def items(self) -> Iterator[tuple[object, object]]:  # type: ignore[override]
        for entry in super().items():
            self.examined += 1
            yield entry


def test_clean_fields_examines_a_bounded_number_of_entries() -> None:
    raw = _CountingMapping({**{f"Bad{index}": index for index in range(1000)}, "valid": 1})

    assert clean_fields(raw, limit=20) == {}
    assert raw.examined <= 80


def test_declared_fields_after_many_invalid_keys_render_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = {**{f"Bad{index}": index for index in range(1000)}, "valid": 1}

    assert "valid" not in _log_exception(capsys, _DeclaringError(raw))


def test_declared_fields_render(capsys: pytest.CaptureFixture[str]) -> None:
    request = httpx.Request("GET", "https://api.example.com/v1")
    cause = httpx.HTTPStatusError(
        "SENTINEL", request=request, response=httpx.Response(502, request=request)
    )
    error = _DeclaringError({"connector_id": "c1", "attempt": 2})
    error.__cause__ = cause

    record = _log_exception(capsys, error)

    assert record["error_code"] == "drive.revoke_failed"
    assert record["connector_id"] == "c1"
    assert record["attempt"] == 2
    assert record["status_code"] == 502


def test_declared_fields_rules(capsys: pytest.CaptureFixture[str]) -> None:
    error = _DeclaringError(
        {"Bad-Key": 1, "nested": {"a": 1}, "request_id": "x", "event": "y", "ok": "v"}
    )

    record = _log_exception(capsys, error)

    assert record["ok"] == "v"
    assert record["event"] == "op.failed"
    assert "request_id" not in record
    assert "Bad-Key" not in record
    assert "nested" not in record
    assert "SENTINEL" not in json_lib.dumps(record)


def test_raising_log_fields_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    record = _log_exception(capsys, _DeclaringError(RuntimeError("SENTINEL")))

    assert record["exception_type"].endswith("_DeclaringError")
    assert "connector_id" not in record
    assert record["error_code"] == "drive.revoke_failed"
    assert record["event"] == "op.failed"


def test_non_mapping_log_fields_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    baseline = _log_exception(capsys, _DeclaringError({}))
    record = _log_exception(capsys, _DeclaringError([("a", 1)]))

    assert record.keys() == baseline.keys()


def test_declared_redact_key_masked(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    try:
        raise _DeclaringError({"password": "SENTINEL"})
    except _DeclaringError:
        collide_logging.get_logger("t").exception("op.failed")
    record = json_lib.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert record["password"] == "***REDACTED***"


def test_outermost_declared_field_wins(capsys: pytest.CaptureFixture[str]) -> None:
    inner = _DeclaringError({"tenant": "inner", "inner_only": 1})
    inner.error_code = "inner.code"
    outer = _DeclaringError({"tenant": "outer"})
    outer.__cause__ = inner

    record = _log_exception(capsys, outer)

    assert record["tenant"] == "outer"
    assert record["inner_only"] == 1
    assert record["error_code"] == "drive.revoke_failed"


def test_error_code_must_be_str_and_is_capped(capsys: pytest.CaptureFixture[str]) -> None:
    long_code = _DeclaringError({})
    long_code.error_code = "c" * 300
    assert _log_exception(capsys, long_code)["error_code"] == "c" * 128

    numeric_code = _DeclaringError({})
    numeric_code.error_code = 500  # type: ignore[assignment]
    assert "error_code" not in _log_exception(capsys, numeric_code)


def test_call_site_field_beats_declared(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    try:
        raise _DeclaringError({"tenant": "declared"})
    except _DeclaringError:
        collide_logging.get_logger("t").exception("op.failed", tenant="call_site")
    record = json_lib.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert record["tenant"] == "call_site"


class _HostileMapping(dict):  # type: ignore[type-arg]
    def items(self) -> object:
        raise RuntimeError("SENTINEL")


class _HostileStr(str):
    __slots__ = ()

    def __getitem__(self, index: object) -> str:
        raise RuntimeError("SENTINEL")


def test_hostile_declared_fields_keep_the_event(capsys: pytest.CaptureFixture[str]) -> None:
    record = _log_exception(capsys, _DeclaringError(_HostileMapping(a=1)))

    assert record["event"] == "op.failed"
    assert record["exception_type"].endswith("_DeclaringError")
    assert "a" not in record


def test_str_subclass_value_capped_without_running_its_getitem(
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = _log_exception(capsys, _DeclaringError({"ok": _HostileStr("x" * 300)}))

    assert record["event"] == "op.failed"
    assert record["ok"] == "x" * 128


def test_str_enum_error_code(capsys: pytest.CaptureFixture[str]) -> None:
    class Code(enum.StrEnum):
        REVOKE = "drive.revoke_failed"

    error = _DeclaringError({})
    error.error_code = Code.REVOKE

    assert _log_exception(capsys, error)["error_code"] == "drive.revoke_failed"


def test_non_finite_floats_dropped() -> None:
    raw = {"nan": math.nan, "inf": math.inf, "neg": -math.inf, "fine": 1.5}

    assert clean_fields(raw, limit=10) == {"fine": 1.5}


def test_declared_beats_builtin(capsys: pytest.CaptureFixture[str]) -> None:
    request = httpx.Request("GET", "https://api.example.com/v1")
    error = _DeclaringError({"status_code": 999})
    error.response = httpx.Response(502, request=request)  # type: ignore[attr-defined]

    assert _log_exception(capsys, error)["status_code"] == 999


def test_bearer_in_rendered_exception_text_scrubbed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error_class = type("Bearer abcdefghijkl12345", (Exception,), {})
    collide_logging.configure(service="svc", json=True)
    try:
        raise error_class("SENTINEL")
    except Exception:
        collide_logging.get_logger("t").exception("op.failed")
    out = capsys.readouterr().out

    assert "abcdefghijkl12345" not in out
    assert "Bearer ***REDACTED***" in out
