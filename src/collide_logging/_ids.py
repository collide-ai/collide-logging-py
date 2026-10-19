"""Id generation and inbound-id validation shared by workers and middleware."""

from __future__ import annotations

import re
import secrets

_INBOUND_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def generate_id() -> str:
    return secrets.token_hex(8)


def parse_inbound_id(value: str | None) -> str | None:
    """Return ``value`` if it is a safe id, else ``None``.

    ``fullmatch`` is used because ``$`` would accept a trailing newline.
    """
    if value is not None and _INBOUND_ID.fullmatch(value):
        return value
    return None
