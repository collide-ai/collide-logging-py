from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POSITIVE = "typecheck/positive.py"
NEGATIVES = "typecheck/negatives.py"
CHECKERS = frozenset({"mypy", "pyright", "ty"})
_MARKER = re.compile(r"# expect-error(?:\[(?P<checkers>[^\]]*)\])?$")

_TY_LINE = re.compile(r"^.+?:(\d+):\d+: error\b")
_MYPY_LINE = re.compile(r"^typecheck/negatives\.py:(\d+): error:")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--no-sync", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _markers() -> dict[int, frozenset[str] | None]:
    """Each marked line of ``NEGATIVES`` and the checkers it names, ``None`` meaning every one."""
    markers: dict[int, frozenset[str] | None] = {}
    for number, line in enumerate((ROOT / NEGATIVES).read_text().splitlines(), start=1):
        match = _MARKER.search(line.rstrip())
        if match:
            named = match["checkers"]
            markers[number] = None if named is None else frozenset(named.split(","))
    return markers


def _expected_lines(checker: str) -> set[int]:
    return {
        number for number, checkers in _markers().items() if checkers is None or checker in checkers
    }


def _pyright_error_lines(proc: subprocess.CompletedProcess[str]) -> set[int]:
    diagnostics = json.loads(proc.stdout)["generalDiagnostics"]
    return {d["range"]["start"]["line"] + 1 for d in diagnostics if d["severity"] == "error"}


def _ty_error_lines(proc: subprocess.CompletedProcess[str]) -> set[int]:
    return {int(m.group(1)) for line in proc.stdout.splitlines() if (m := _TY_LINE.match(line))}


def _mypy_error_lines(proc: subprocess.CompletedProcess[str]) -> set[int]:
    return {int(m.group(1)) for line in proc.stdout.splitlines() if (m := _MYPY_LINE.match(line))}


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
    ("command", "checker", "error_lines"),
    [
        pytest.param(
            ["pyright", "--outputjson", NEGATIVES], "pyright", _pyright_error_lines, id="pyright"
        ),
        pytest.param(
            ["ty", "check", "--output-format", "concise", NEGATIVES],
            "ty",
            _ty_error_lines,
            id="ty",
        ),
        pytest.param(["mypy", NEGATIVES], "mypy", _mypy_error_lines, id="mypy"),
    ],
)
def test_typing_negatives_fire(
    command: list[str],
    checker: str,
    error_lines: Callable[[subprocess.CompletedProcess[str]], set[int]],
) -> None:
    proc = _run(*command)
    expected = _expected_lines(checker)
    assert expected, f"no expect-error markers for {checker}"
    assert error_lines(proc) == expected, proc.stdout + proc.stderr


def test_expect_error_markers_name_known_checkers() -> None:
    markers = _markers()
    assert markers, "no expect-error markers found"
    lines = (ROOT / NEGATIVES).read_text().splitlines()
    mentioned = {number for number, line in enumerate(lines, start=1) if "expect-error" in line}
    assert mentioned <= markers.keys(), sorted(mentioned - markers.keys())
    named = [checkers for checkers in markers.values() if checkers is not None]
    assert all(checkers <= CHECKERS for checkers in named), named
