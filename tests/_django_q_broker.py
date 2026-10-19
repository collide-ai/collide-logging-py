from __future__ import annotations

from typing import Any


class NullBroker:
    """Broker stand-in: sync tasks never enqueue, and the monitor only needs ``acknowledge``."""

    def __init__(self, list_key: str | None = None) -> None:
        self.list_key = list_key

    def acknowledge(self, ack_id: Any) -> None:
        pass
