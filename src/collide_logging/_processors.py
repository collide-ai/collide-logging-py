"""structlog processors implementing the collide/v1 spec.

Internal module. The public surface is the factories `_redact_secrets` and
`_add_service_info`, which `configure()` (issue #3) wires into the structlog
processor chain.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, MutableMapping
from typing import Any

_Processor = Callable[
    [Any, str, MutableMapping[str, Any]],
    MutableMapping[str, Any],
]

_REDACTED = "***REDACTED***"
_REDACTION_ERROR = "***REDACTION_ERROR***"
_SUFFIX_RULES = ("_token", "_api_token", "_signing_secret")


def _json_default(value: object) -> object:
    """Serialize values ``json.dumps`` cannot.

    UUIDs become canonical strings; objects defining ``__structlog__`` use it (as
    structlog's own default does); anything else falls back to ``repr``.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        structlog_hook = getattr(value, "__structlog__", None)
        if callable(structlog_hook):
            return structlog_hook()
        return repr(value)
    except Exception:
        return _REDACTION_ERROR


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
