from __future__ import annotations

from collide_logging import __version__


def test_version_is_string() -> None:
    assert isinstance(__version__, str)
    assert __version__
