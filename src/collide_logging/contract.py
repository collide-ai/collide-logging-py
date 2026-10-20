"""The typed logging contract every consumer codes against."""

from __future__ import annotations

from typing import LiteralString, Protocol


class Logger(Protocol):
    """Structured logger whose event names are static strings.

    ``event`` is ``LiteralString`` so a type checker rejects prose and interpolated
    event names; variable data goes in keyword ``fields``.
    """

    def debug(self, event: LiteralString, /, **fields: object) -> None: ...

    def info(self, event: LiteralString, /, **fields: object) -> None: ...

    def warning(self, event: LiteralString, /, **fields: object) -> None: ...

    def error(self, event: LiteralString, /, **fields: object) -> None: ...

    def critical(self, event: LiteralString, /, **fields: object) -> None: ...

    def exception(self, event: LiteralString, /, **fields: object) -> None: ...

    def bind(self, **fields: object) -> Logger: ...
