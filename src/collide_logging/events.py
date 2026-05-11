"""Public events API for collide-logging.

Adapter authors declare event schemas via :func:`register_event_schema` and
emit records via :meth:`CollideLogger.event`. The library owns validation
and redaction; adapters only declare.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog

__all__ = [
    "EventSchema",
    "EventValidationError",
    "FieldSpec",
    "list_schemas",
    "register_event_schema",
]


class EventValidationError(ValueError):
    """Raised by CollideLogger.event when COLLIDE_LOG_VALIDATE is unset or 'raise'."""


@dataclass
class FieldSpec:
    """Declaration of one field on an event schema.

    Args:
        type: The intended Python type of the value. Currently documentary;
            not enforced at runtime.
        required: If True, the field must be supplied on every event call.
        redact: If True, the value is replaced with a length+sha256 digest
            before emission.
    """

    type: type
    required: bool = False
    redact: bool = False


@dataclass
class EventSchema:
    """One named event type emitted by an adapter.

    Args:
        name: Dotted event name (e.g. ``hermes.skill.invoke``). Becomes the
            ``event`` field on the emitted record.
        fields: Mapping of field name to :class:`FieldSpec`.
        description: Optional human-readable description.
    """

    name: str
    fields: dict[str, FieldSpec]
    description: str = ""


_REGISTRY: dict[str, EventSchema] = {}


def register_event_schema(schema: EventSchema) -> None:
    """Register an event schema in the module-global registry.

    Re-registering the same name with identical fields and description is a
    no-op. Re-registering with a different shape raises ``ValueError``.
    """
    existing = _REGISTRY.get(schema.name)
    if existing is None:
        _REGISTRY[schema.name] = EventSchema(
            name=schema.name,
            fields=dict(schema.fields),
            description=schema.description,
        )
        return
    if existing.fields != schema.fields or existing.description != schema.description:
        raise ValueError(
            f"Event schema {schema.name!r} already registered with a different shape"
        )


def list_schemas() -> list[EventSchema]:
    """Return registered schemas sorted by name."""
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def _reset_registry() -> None:
    """Test-only: clear the schema registry."""
    _REGISTRY.clear()


def _redact_event_field(value: Any) -> dict[str, Any]:
    """Return ``{"len", "sha256"}`` digest for a redact-flagged field."""
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = repr(value).encode("utf-8")
    return {
        "len": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest()[:8],
    }


def _classify(schema: EventSchema, fields: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return ``(missing_required, unknown)`` field-name lists."""
    given = set(fields.keys())
    missing = sorted(
        name for name, spec in schema.fields.items() if spec.required and name not in given
    )
    unknown = sorted(given - set(schema.fields.keys()))
    return missing, unknown


def _emit_event(
    logger: structlog.stdlib.BoundLogger,
    name: str,
    fields: dict[str, Any],
) -> None:
    """Validate, redact, and emit one event through the configured logger."""
    mode = os.environ.get("COLLIDE_LOG_VALIDATE", "raise").lower()
    schema = _REGISTRY.get(name)
    if schema is None:
        _violation(logger, mode, name, violation="unknown_event")
        return

    missing, unknown = _classify(schema, fields)
    if missing or unknown:
        violation = "missing_required" if missing else "unknown_field"
        _violation(
            logger,
            mode,
            name,
            violation=violation,
            missing=missing,
            unknown=unknown,
        )
        return

    payload = {
        key: (_redact_event_field(value) if schema.fields[key].redact else value)
        for key, value in fields.items()
    }
    logger.info(name, **payload)


def _violation(
    logger: structlog.stdlib.BoundLogger,
    mode: str,
    schema_name: str,
    *,
    violation: str,
    missing: list[str] | None = None,
    unknown: list[str] | None = None,
) -> None:
    if mode == "lenient":
        meta: dict[str, Any] = {"violation": violation, "schema": schema_name}
        if missing:
            meta["missing"] = missing
        if unknown:
            meta["unknown"] = unknown
        logger.info("collide_logging.schema_violation", **meta)
        return
    bits = [f"event={schema_name!r}", f"violation={violation}"]
    if missing:
        bits.append(f"missing={missing}")
    if unknown:
        bits.append(f"unknown={unknown}")
    raise EventValidationError("; ".join(bits))
