from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POSITIVE = "tests/typecheck/positive.py"
NEGATIVES = "tests/typecheck/negatives.py"
MARKER = "# expect-error"

_TY_LINE = re.compile(r"^.+?:(\d+):\d+: error\b")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--no-sync", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _expected_lines() -> set[int]:
    text = (ROOT / NEGATIVES).read_text().splitlines()
    return {n for n, line in enumerate(text, start=1) if line.rstrip().endswith(MARKER)}


def _pyright_error_lines(proc: subprocess.CompletedProcess[str]) -> set[int]:
    diagnostics = json.loads(proc.stdout)["generalDiagnostics"]
    return {d["range"]["start"]["line"] + 1 for d in diagnostics if d["severity"] == "error"}


def _ty_error_lines(proc: subprocess.CompletedProcess[str]) -> set[int]:
    return {int(m.group(1)) for line in proc.stdout.splitlines() if (m := _TY_LINE.match(line))}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["pyright", POSITIVE], id="pyright"),
        pytest.param(["ty", "check", POSITIVE], id="ty"),
        pytest.param(["mypy", POSITIVE], id="mypy"),
    ],
)
def test_typing_positive_clean(command: list[str]) -> None:
    proc = _run(*command)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("command", "error_lines"),
    [
        pytest.param(["pyright", "--outputjson", NEGATIVES], _pyright_error_lines, id="pyright"),
        pytest.param(
            ["ty", "check", "--output-format", "concise", NEGATIVES], _ty_error_lines, id="ty"
        ),
    ],
)
def test_typing_negatives_fire(
    command: list[str],
    error_lines: Callable[[subprocess.CompletedProcess[str]], set[int]],
) -> None:
    proc = _run(*command)
    expected = _expected_lines()
    assert expected, "no expect-error markers found"
    assert error_lines(proc) == expected, proc.stdout + proc.stderr
