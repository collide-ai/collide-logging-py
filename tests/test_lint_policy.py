"""Pins the programme's ruff baseline so the config cannot drift or be suppressed silently."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.test_type_policy import ROOT, _pyproject, _python_sources

BASELINE_RULES: Final = frozenset(
    {
        "E", "W", "F", "I", "N", "B", "UP", "C4", "SIM", "RUF", "PERF", "FLY", "INT", "PGH",
        "S", "BLE", "TRY", "ASYNC", "G", "LOG", "T20", "DTZ", "PTH", "FBT", "ARG", "ERA",
        "TD002", "TD003", "SLF001", "ANN401", "TID251",
        "PLC0415", "PLR2004", "PLR1736", "PLR0124", "PLW0602", "PLW0603", "PT",
    }
)  # fmt: skip
REQUIRED_IGNORES: Final = frozenset(
    {"E501", "TRY003", "TRY400", "G201", "SIM105", "B008", "S603", "S607"}
)
NEVER_SELECTED: Final = frozenset(
    {"TC", "TCH", "TC001", "TC002", "TC003", "C", "C90", "C901", "ANN"}
)
# Rules selected but still ignored while their findings are fixed; this set only shrinks and
# is empty when the baseline is complete.
PENDING_RULES: Final[frozenset[str]] = frozenset(
    {
        "TRY301", "TRY004", "ARG001", "ANN401", "BLE001", "PT011", "PT017", "PLC0415",
        "PLR2004", "T201", "FBT001", "FBT003", "PLW0603", "G004", "DTZ005", "SLF001", "S110",
    }
)  # fmt: skip
TEST_GLOBS: Final = frozenset({"tests/**", "typecheck/**"})
TEST_IGNORES: Final = frozenset(
    {"S101", "S105", "S106", "S107", "S608", "PLR2004", "SLF001", "ARG", "FBT", "ANN401"}
)
BANS: Final = {
    "typing.TYPE_CHECKING": "from typing import TYPE_CHECKING\n",
    "os.environ": "import os\nX = os.environ\n",
    "os.getenv": "import os\nX = os.getenv('A')\n",
    "logging.FileHandler": "import logging\nX = logging.FileHandler\n",
    "logging.handlers.RotatingFileHandler": (
        "import logging.handlers\nX = logging.handlers.RotatingFileHandler\n"
    ),
    "logging.handlers.TimedRotatingFileHandler": (
        "import logging.handlers\nX = logging.handlers.TimedRotatingFileHandler\n"
    ),
}
NOQA: Final = re.compile(
    r"#\s*noqa(?P<codes>:\s*[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)?(?P<rest>.*)$", re.IGNORECASE
)
FILE_NOQA: Final = re.compile(r"#\s*ruff\s*:\s*noqa", re.IGNORECASE)


def _lint() -> dict[str, Any]:
    lint: dict[str, Any] = _pyproject()["tool"]["ruff"]["lint"]
    return lint


def _selected() -> set[str]:
    return set(_lint()["select"]) | set(_lint().get("extend-select", []))


def _per_file_ignores() -> dict[str, list[str]]:
    ignores: dict[str, list[str]] = _lint()["per-file-ignores"]
    return ignores


def _policy_free_sources() -> list[Path]:
    return [path for path in _python_sources() if path.name != "test_lint_policy.py"]


def test_baseline_rules_are_selected() -> None:
    assert _selected() >= BASELINE_RULES
    assert not _selected() & NEVER_SELECTED
    assert not _lint().get("preview", False)


def test_ignores_are_the_required_set_plus_the_pending_rules() -> None:
    assert set(_lint()["ignore"]) == REQUIRED_IGNORES | PENDING_RULES
    assert not REQUIRED_IGNORES & PENDING_RULES


def test_ruff_is_pinned_exactly() -> None:
    assert _pyproject()["dependency-groups"]["dev"].count("ruff==0.17.4") == 1


def test_convention_conflicting_rules_are_inactive() -> None:
    assert {"TRY400", "G201", "SIM105"} <= set(_lint()["ignore"])
    assert not _selected() & {"TC", "TCH", "TC001", "TC002", "TC003"}


def test_tid251_table_is_exact() -> None:
    assert set(_lint()["flake8-tidy-imports"]["banned-api"]) == set(BANS)


def test_only_test_paths_have_per_file_ignores() -> None:
    assert set(_per_file_ignores()) == TEST_GLOBS
    for pattern, rules in _per_file_ignores().items():
        assert set(rules) == TEST_IGNORES, pattern


def test_migrations_are_the_only_extend_exclude() -> None:
    ruff = _pyproject()["tool"]["ruff"]
    assert ruff["extend-exclude"] == ["**/migrations/**"]
    assert "exclude" not in ruff
    assert "exclude" not in ruff.get("format", {})


def test_noqa_has_code_and_reason() -> None:
    missing_reason = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _policy_free_sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if (match := NOQA.search(line))
        and (
            not match["codes"]
            or "TID251" in match["codes"]
            or not re.match(r"\s{2}#\s*\S", match["rest"])
        )
    ]
    file_level = [
        str(path.relative_to(ROOT))
        for path in _policy_free_sources()
        if FILE_NOQA.search(path.read_text())
    ]
    assert missing_reason == []
    assert file_level == []


@pytest.mark.parametrize("api", sorted(BANS))
def test_each_ban_fires_in_the_library(api: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "TID251",
            "--stdin-filename",
            "src/collide_logging/planted.py",
            "-",
        ],
        input=BANS[api],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert "TID251" in result.stdout, result.stdout + result.stderr


def test_lint_job_runs_both_ruff_commands() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    runs = [step.get("run", "") for step in ci["jobs"]["lint"]["steps"]]
    assert "uv run ruff check ." in runs
    assert "uv run ruff format --check ." in runs
