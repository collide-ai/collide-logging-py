from __future__ import annotations

import importlib.metadata

from collide_logging import __version__


def test_version_is_string() -> None:
    assert isinstance(__version__, str)
    assert __version__


def test_version_matches_installed_metadata() -> None:
    assert __version__ == importlib.metadata.version("collide-logging")
