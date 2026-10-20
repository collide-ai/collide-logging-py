from __future__ import annotations

import importlib
import importlib.util

import pytest

import collide_logging

REMOVED_NAMES = [
    "EventSchema",
    "EventValidationError",
    "FieldSpec",
    "digest_value",
    "list_schemas",
    "register_event_schema",
]


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_removed_public_names_are_gone(name: str) -> None:
    assert not hasattr(collide_logging, name)
    assert name not in collide_logging.__all__


def test_collide_logger_has_no_event_method() -> None:
    assert not hasattr(collide_logging.CollideLogger, "event")


def test_flask_adapter_is_gone() -> None:
    assert importlib.util.find_spec("collide_logging.flask") is None
