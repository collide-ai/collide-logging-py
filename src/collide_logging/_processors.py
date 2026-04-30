"""structlog processors implementing the collide/v1 spec.

Internal module. The public surface is the factories `_redact_secrets` and
`_add_service_info`, which `configure()` (issue #3) wires into the structlog
processor chain.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any

_Processor = Callable[
    [Any, str, MutableMapping[str, Any]],
    MutableMapping[str, Any],
]

_REDACTED = "***REDACTED***"
_SUFFIX_RULES = ("_token", "_api_token", "_signing_secret")


def _redact_secrets(redact_keys: frozenset[str]) -> _Processor:
    """Return a processor that redacts sensitive values from event dicts.

    Redaction triggers on either:
      - case-insensitive exact match against `redact_keys`, or
      - case-insensitive suffix match against the spec-mandated patterns
        `*_token`, `*_api_token`, `*_signing_secret`.
    """
    keys = frozenset(k.lower() for k in redact_keys)

    def processor(
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        for key in list(event_dict.keys()):
            lk = key.lower()
            if lk in keys or lk.endswith(_SUFFIX_RULES):
                event_dict[key] = _REDACTED
        return event_dict

    return processor


def _add_service_info(service: str) -> _Processor:
    """Return a processor that tags every event with the service slug.

    Uses setdefault so an explicit `service=...` on a log call is preserved.
    """

    def processor(
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        event_dict.setdefault("service", service)
        return event_dict

    return processor
