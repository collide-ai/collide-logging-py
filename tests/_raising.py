from __future__ import annotations

from typing import NoReturn


def raise_error(error: BaseException) -> NoReturn:
    """Raise ``error`` so a test's ``try`` body holds only the failing call.

    The API under test needs a live exception inside an ``except`` handler; the helper
    produces it without a ``raise`` in the ``try`` body itself (TRY301).
    """
    raise error


def raise_error_from(error: BaseException, cause: BaseException) -> NoReturn:
    """Raise ``error from cause``, building the chain the way production code builds it."""
    raise error from cause
