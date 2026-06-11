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
    "digest_value",
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
    """Test-only: clear the schema registry and process-once state."""
    _REGISTRY.clear()
    _SEEN_INVALID_MODES.clear()


_VALID_MODES = frozenset({"raise", "lenient"})

# Bad COLLIDE_LOG_VALIDATE values already warned about this process, so the
# warning fires once per distinct value rather than on every event. In a normal
# service the env var is set once at boot, so this holds at most one entry;
# it only accumulates if a process mutates the env var at runtime.
_SEEN_INVALID_MODES: set[str] = set()


def _resolve_mode(raw: str | None) -> tuple[str, str | None]:
    """Map ``COLLIDE_LOG_VALIDATE`` to ``(mode, invalid_value)``.

    Unset or ``"raise"`` -> raise. ``"lenient"`` -> lenient. Any other set
    value -> lenient, returned as ``invalid_value`` (the offending raw string):
    a misconfigured prod var (a typo like ``leniant``, or stray whitespace)
    must fail safe rather than silently start raising ``EventValidationError``
    into the host. Whitespace is stripped so ``"lenient "`` resolves to
    lenient, not invalid. ``invalid_value`` is ``None`` whenever the mode was
    recognized.
    """
    if raw is None:
        return "raise", None
    normalized = raw.strip().lower()
    if normalized in _VALID_MODES:
        return normalized, None
    return "lenient", raw


def _note_invalid_mode(logger: structlog.stdlib.BoundLogger, raw: str) -> None:
    """Emit a one-time warning that an unrecognized validate mode fell back to lenient."""
    if raw in _SEEN_INVALID_MODES:
        return
    _SEEN_INVALID_MODES.add(raw)
    logger.warning(
        "collide_logging.invalid_validate_mode",
        value=raw,
        resolved="lenient",
    )


def digest_value(value: Any) -> dict[str, Any]:
    """Return a ``{"len", "sha256"}`` digest of ``value`` for redaction.

    This is the exact transform behind ``FieldSpec(redact=True)``, exposed so
    consumers on the plain ``log.info(...)`` path can hand-redact sensitive
    free-text values (search queries, transcripts) that name-based redaction
    never inspects, and stay byte-for-byte consistent with the library's own
    field-level redaction. ``sha256`` is the first 8 hex characters of the
    SHA-256 of the value's UTF-8 encoding (``repr`` for non-str/bytes values).
    """
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


# "exception" is deliberately excluded: structlog's .exception() auto-captures
# the current traceback even with exc_info unset, so it would not honor the
# "defaulted call is unchanged" contract. Use level="error", exc_info=True.
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical"})


def _emit(
    logger: structlog.stdlib.BoundLogger,
    level: str,
    name: str,
    payload: dict[str, Any],
    exc_info: Any,
) -> None:
    """Emit ``name`` at ``level``, threading ``exc_info`` only when truthy.

    Omitting a falsy ``exc_info`` keeps the record identical to a bare
    ``logger.<level>(name, **payload)`` call, so defaulted callers see no drift.
    """
    method = getattr(logger, level)
    if exc_info:
        method(name, exc_info=exc_info, **payload)
    else:
        method(name, **payload)


def _redact_known(schema: EventSchema, fields: dict[str, Any]) -> dict[str, Any]:
    """Redact flagged fields, keeping only keys declared on ``schema``.

    Filtering to declared keys is a no-op on the happy path (validation already
    proved every key is declared) and drops unknown keys on the best-effort path.
    """
    return {
        key: (digest_value(value) if schema.fields[key].redact else value)
        for key, value in fields.items()
        if key in schema.fields
    }


def _emit_event(
    logger: structlog.stdlib.BoundLogger,
    name: str,
    fields: dict[str, Any],
    *,
    level: str = "info",
    exc_info: Any = False,
) -> None:
    """Validate, redact, and emit one event through the configured logger."""
    if level not in _LOG_METHODS:
        raise ValueError(
            f"Unknown log level {level!r}; expected one of {sorted(_LOG_METHODS)}"
        )
    mode, invalid_value = _resolve_mode(os.environ.get("COLLIDE_LOG_VALIDATE"))
    if invalid_value is not None:
        _note_invalid_mode(logger, invalid_value)
    schema = _REGISTRY.get(name)
    if schema is None:
        # _violation raises in raise mode; only returns (True) in lenient mode,
        # which is what gates best-effort emission below.
        if _violation(logger, mode, name, violation="unknown_event"):
            _emit_best_effort(
                logger,
                name,
                fields,
                schema=None,
                violation="unknown_event",
                level=level,
                exc_info=exc_info,
            )
        return

    missing, unknown = _classify(schema, fields)
    if missing or unknown:
        violation = "missing_required" if missing else "unknown_field"
        if _violation(
            logger,
            mode,
            name,
            violation=violation,
            missing=missing,
            unknown=unknown,
        ):
            _emit_best_effort(
                logger,
                name,
                fields,
                schema=schema,
                violation=violation,
                missing=missing,
                unknown=unknown,
                level=level,
                exc_info=exc_info,
            )
        return

    _emit(logger, level, name, _redact_known(schema, fields), exc_info)


def _emit_best_effort(
    logger: structlog.stdlib.BoundLogger,
    name: str,
    fields: dict[str, Any],
    *,
    schema: EventSchema | None,
    violation: str,
    missing: list[str] | None = None,
    unknown: list[str] | None = None,
    level: str,
    exc_info: Any,
) -> None:
    """Emit a violating event under its real name so the payload survives.

    Lenient mode only (gated on ``_violation`` returning rather than raising).
    Unknown fields are dropped — there is no schema entry to validate or
    field-redact them — but their names are preserved in the
    ``_schema_violation`` marker on the record. Known fields keep their
    field-level redaction. When no schema is registered at all, every supplied
    field is emitted as-is and the global suffix-based redaction processor is
    the only redaction that applies.
    """
    marker: dict[str, Any] = {"violation": violation}
    if missing:
        marker["missing"] = missing
    if unknown:
        marker["unknown"] = unknown

    payload = dict(fields) if schema is None else _redact_known(schema, fields)
    # marker last so a caller field named _schema_violation cannot clobber it.
    _emit(logger, level, name, {**payload, "_schema_violation": marker}, exc_info)


def _violation(
    logger: structlog.stdlib.BoundLogger,
    mode: str,
    schema_name: str,
    *,
    violation: str,
    missing: list[str] | None = None,
    unknown: list[str] | None = None,
) -> bool:
    """Signal a schema violation. Returns True when suppressed (lenient mode).

    In lenient mode, emits the ``collide_logging.schema_violation`` meta-event
    and returns True, telling the caller it is safe to emit the event
    best-effort. In raise mode, raises ``EventValidationError`` and never
    returns — so best-effort emission is structurally unreachable in dev.
    """
    if mode == "lenient":
        meta: dict[str, Any] = {"violation": violation, "schema": schema_name}
        if missing:
            meta["missing"] = missing
        if unknown:
            meta["unknown"] = unknown
        logger.info("collide_logging.schema_violation", **meta)
        return True
    bits = [f"event={schema_name!r}", f"violation={violation}"]
    if missing:
        bits.append(f"missing={missing}")
    if unknown:
        bits.append(f"unknown={unknown}")
    raise EventValidationError("; ".join(bits))
