"""Fields read from exceptions: declared by the exception, built in, and captured by log_context.

Exception messages can carry arbitrary data, so only allow-listed attributes are read, each one
type-checked and capped. A read that raises omits that one field.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping

from collide_logging._fields import MAX_FIELDS, FieldValue, cap_string, clean_fields

_CAPTURED_ATTRIBUTE = "_collide_log_context"


def _path(obj: object, *names: str) -> object:
    """Follow ``obj.name1.name2...``; ``None`` when any step is missing or raises."""
    try:
        for name in names:
            obj = getattr(obj, name)
    except Exception:
        return None
    return obj


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: object) -> str | None:
    return cap_string(value) if isinstance(value, str) else None


def _candidates(exc: BaseException) -> dict[str, FieldValue]:
    return {
        "status_code": _as_int(_path(exc, "response", "status_code")),
        "http_method": _as_str(_path(exc, "request", "method")),
        "http_host": _as_str(_path(exc, "request", "url", "host")),
        "sqlstate": _as_str(_path(exc, "sqlstate")) or _as_str(_path(exc, "diag", "sqlstate")),
        "constraint": _as_str(_path(exc, "diag", "constraint_name")),
        "db_schema": _as_str(_path(exc, "diag", "schema_name")),
        "db_table": _as_str(_path(exc, "diag", "table_name")),
        "db_column": _as_str(_path(exc, "diag", "column_name")),
    }


def _builtin_attributes(chain: list[BaseException]) -> dict[str, FieldValue]:
    found: dict[str, FieldValue] = {}
    for exc in chain:
        for name, value in _candidates(exc).items():
            if value is not None and name not in found:
                found[name] = value
    return found


def _log_fields_of(exc: BaseException) -> object:
    method = _path(exc, "__log_fields__")
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _declared_by(exc: BaseException) -> dict[str, FieldValue]:
    declared: dict[str, FieldValue] = {}
    code = _as_str(_path(exc, "error_code"))
    if code is not None:
        declared["error_code"] = code
    raw = _log_fields_of(exc)
    if isinstance(raw, Mapping):
        with contextlib.suppress(Exception):
            declared.update(clean_fields(raw, limit=MAX_FIELDS))
    return declared


def _declared_fields(chain: list[BaseException]) -> dict[str, FieldValue]:
    found: dict[str, FieldValue] = {}
    for exc in chain:
        for name, value in _declared_by(exc).items():
            found.setdefault(name, value)
    return found


def record_context(exc: BaseException, fields: Mapping[str, FieldValue]) -> None:
    """Record ``fields`` on ``exc`` beneath any already recorded by an inner block."""
    # An exception that refuses the attribute simply carries no context.
    try:
        captured = getattr(exc, _CAPTURED_ATTRIBUTE, None)
        if not isinstance(captured, dict):
            captured = {}
            setattr(exc, _CAPTURED_ATTRIBUTE, captured)
        for name, value in fields.items():
            captured.setdefault(name, value)
    except Exception:
        return


def _captured_context(chain: list[BaseException]) -> dict[str, FieldValue]:
    for exc in chain:
        raw = _path(exc, _CAPTURED_ATTRIBUTE)
        if isinstance(raw, dict):
            return dict(raw)
    return {}


def exception_fields(chain: list[BaseException]) -> dict[str, FieldValue]:
    """Merge the chain's fields: declared, then built-in, then captured; the first source wins.

    Declared fields are ``error_code`` and ``__log_fields__()`` (outermost exception first, through
    the field rule). Built-in attributes come from httpx and psycopg-style errors. Captured fields
    are those ``log_context`` recorded on the outermost exception that carries them.
    """
    merged = _declared_fields(chain)
    for source in (_builtin_attributes(chain), _captured_context(chain)):
        for name, value in source.items():
            merged.setdefault(name, value)
    return merged
