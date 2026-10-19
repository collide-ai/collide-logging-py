from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

import collide_logging


def _last_line(out: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(out.strip().splitlines()[-1])
    return parsed


def test_trace_ids_inside_span(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        ctx = span.get_span_context()
        collide_logging.get_logger("t").info("trace.inside")
    record = _last_line(capsys.readouterr().out)
    assert record["trace_id"] == format(ctx.trace_id, "032x")
    assert record["span_id"] == format(ctx.span_id, "016x")


def test_trace_ids_absent_outside_span(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").info("trace.outside")
    record = _last_line(capsys.readouterr().out)
    assert "trace_id" not in record
    assert "span_id" not in record


def test_explicit_trace_id_is_preserved(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work"):
        collide_logging.get_logger("t").info("trace.explicit", trace_id="caller-id")
    assert _last_line(capsys.readouterr().out)["trace_id"] == "caller-id"


def test_no_opentelemetry_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("opentelemetry", "opentelemetry.trace"):
        monkeypatch.setitem(sys.modules, name, None)
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").info("trace.no_otel")
    record = _last_line(capsys.readouterr().out)
    assert record["event"] == "trace.no_otel"
    assert "trace_id" not in record
    assert "span_id" not in record


def test_fresh_import_works_with_opentelemetry_blocked() -> None:
    script = (
        "import sys\n"
        "sys.modules['opentelemetry'] = None\n"
        "sys.modules['opentelemetry.trace'] = None\n"
        "import collide_logging\n"
        "collide_logging.configure(service='t', json=True)\n"
        "collide_logging.get_logger('t').info('trace.fresh')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")},
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout.strip().splitlines()[-1])
    assert record["event"] == "trace.fresh"
    assert "trace_id" not in record


def test_stdlib_record_inside_span_carries_ids(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        ctx = span.get_span_context()
        logging.getLogger("lib").info("x.y")
    record = _last_line(capsys.readouterr().out)
    assert record["trace_id"] == format(ctx.trace_id, "032x")
    assert record["span_id"] == format(ctx.span_id, "016x")


def test_none_ids_are_filled_from_the_span(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        ctx = span.get_span_context()
        collide_logging.get_logger("t").info("trace.none", trace_id=None, span_id=None)
    record = _last_line(capsys.readouterr().out)
    assert record["trace_id"] == format(ctx.trace_id, "032x")
    assert record["span_id"] == format(ctx.span_id, "016x")


def test_none_ids_are_dropped_without_a_span(capsys: pytest.CaptureFixture[str]) -> None:
    collide_logging.configure(service="svc", json=True)
    collide_logging.get_logger("t").info("trace.none", trace_id=None, span_id=None)
    record = _last_line(capsys.readouterr().out)
    assert "trace_id" not in record
    assert "span_id" not in record
