from __future__ import annotations

import re

import pytest

from collide_logging._ids import generate_id, parse_inbound_id


def test_generated_id_is_16_hex() -> None:
    ids = [generate_id() for _ in range(1000)]
    assert all(re.fullmatch(r"[0-9a-f]{16}", i) for i in ids)
    assert len(set(ids)) == 1000


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc-123", "abc-123"),
        ("a" * 64, "a" * 64),
        ("a" * 65, None),
        ("has space", None),
        ("x\ny", None),
        ("日本", None),
        ("", None),
        (None, None),
    ],
)
def test_inbound_id_rules(value: str | None, expected: str | None) -> None:
    assert parse_inbound_id(value) == expected
