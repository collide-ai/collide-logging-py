from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

BASELINE_CODES = {
    "exhaustive-match",
    "deprecated",
    "ignore-without-code",
    "possibly-undefined",
    "redundant-expr",
    "truthy-bool",
    "truthy-iterable",
    "unused-awaitable",
    "mutable-override",
}
TEST_MODULES = ["tests", "tests.*", "*.tests", "*.tests.*"]
IGNORE = re.compile(r"#\s*type:\s*ignore(?P<code>\[[^\]]+\])?(?P<rest>.*)$")


def _pyproject() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _python_sources() -> list[Path]:
    return [
        path
        for directory in ("src", "tests", "typecheck")
        for path in sorted((ROOT / directory).rglob("*.py"))
    ]


def _sources_naming(term: str) -> list[Path]:
    return [
        path
        for path in _python_sources()
        if term in path.read_text() and path.name != "test_type_policy.py"
    ]


def test_mypy_baseline_config() -> None:
    mypy = _pyproject()["tool"]["mypy"]
    assert mypy["strict"] is True and mypy["warn_unreachable"] is True
    assert mypy["strict_equality_for_none"] is True
    assert set(mypy["enable_error_code"]) == BASELINE_CODES
    assert mypy["files"] == ["src", "tests", "typecheck/positive.py"]
    relaxed = [override for override in mypy["overrides"] if override["module"] == TEST_MODULES]
    assert relaxed == [
        {
            "module": TEST_MODULES,
            "disallow_untyped_defs": False,
            "disallow_incomplete_defs": False,
            "disallow_untyped_calls": False,
        }
    ]
    assert "mypy==2.3.1" in _pyproject()["dependency-groups"]["dev"]


def test_mypy_has_no_exclusions_or_skips() -> None:
    mypy = _pyproject()["tool"]["mypy"]
    assert mypy["exclude"] == ["/migrations/"]
    for section in [mypy, *mypy["overrides"]]:
        assert "ignore_errors" not in section and section.get("follow_imports") != "skip"


def test_no_global_ignore_missing_imports() -> None:
    mypy = _pyproject()["tool"]["mypy"]
    assert "ignore_missing_imports" not in mypy
    ignored = [
        override["module"]
        for override in mypy["overrides"]
        if override.get("ignore_missing_imports")
    ]
    assert ignored == ["django_q.*"]


def test_pyright_only_in_typing_test() -> None:
    assert "pyright" not in _pyproject()["tool"]
    assert not (ROOT / "pyrightconfig.json").exists()
    workflows = [
        path
        for pattern in ("*.yml", "*.yaml")
        for path in (ROOT / ".github").rglob(pattern)
        if "pyright" in path.read_text()
    ]
    typing_test = {ROOT / "tests" / "test_typing.py", ROOT / "typecheck" / "negatives.py"}
    sources = [path for path in _sources_naming("pyright") if path not in typing_test]
    assert [*workflows, *sources] == []


def test_no_type_checking_blocks() -> None:
    assert _sources_naming("TYPE_CHECKING") == []


def test_type_ignores_have_code_and_reason() -> None:
    bad = []
    for path in _python_sources():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if (match := IGNORE.search(line)) and not (
                match["code"] and re.match(r"\s{2}#\s*\S", match["rest"])
            ):
                bad.append(f"{path.relative_to(ROOT)}:{number}")
    assert bad == []
