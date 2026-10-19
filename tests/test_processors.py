from __future__ import annotations

import copy
import time
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any

import pytest

from collide_logging._processors import (
    DEFAULT_REDACT_KEYS,
    _add_service_info,
    _redact_secrets,
)

# Spec-mandated minimum exact-match redact list. Mirrors what configure() will
# pass in by default once #3 lands; declared inline here to keep the test
# coupled to the spec rather than to the (yet-unwritten) defaults constant.
SPEC_DEFAULTS = frozenset(
    {
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "secret",
        "secret_key",
    }
)


def _redact(
    event_dict: dict[str, Any],
    extra: frozenset[str] = frozenset(),
) -> MutableMapping[str, Any]:
    processor = _redact_secrets(SPEC_DEFAULTS | extra)
    return processor(None, "info", dict(event_dict))


def test_redacts_exact_match() -> None:
    assert _redact({"api_key": "abc"})["api_key"] == "***REDACTED***"


def test_redacts_case_insensitive() -> None:
    out = _redact({"API_KEY": "abc"})
    assert out["API_KEY"] == "***REDACTED***"


def test_redacts_suffix_token() -> None:
    assert _redact({"github_token": "x"})["github_token"] == "***REDACTED***"
    assert _redact({"slack_bot_token": "x"})["slack_bot_token"] == "***REDACTED***"


def test_redacts_suffix_signing_secret() -> None:
    out = _redact({"slack_signing_secret": "x"})
    assert out["slack_signing_secret"] == "***REDACTED***"


def test_redacts_suffix_api_token() -> None:
    out = _redact({"jira_api_token": "x"})
    assert out["jira_api_token"] == "***REDACTED***"


def test_does_not_redact_unrelated() -> None:
    out = _redact({"my_creds": "abc", "token_name": "public"})
    assert out["my_creds"] == "abc"
    assert out["token_name"] == "public"


def test_extra_redact_keys() -> None:
    out = _redact({"weirdo_secret_field": "x"}, extra=frozenset({"weirdo_secret_field"}))
    assert out["weirdo_secret_field"] == "***REDACTED***"


def test_redact_preserves_unrelated_fields() -> None:
    out = _redact({"api_key": "x", "user_id": 42, "ok": True})
    assert out["api_key"] == "***REDACTED***"
    assert out["user_id"] == 42
    assert out["ok"] is True


def test_service_info_sets_when_missing() -> None:
    processor = _add_service_info("my-svc")
    out = processor(None, "info", {})
    assert out["service"] == "my-svc"


def test_service_info_does_not_clobber() -> None:
    processor = _add_service_info("default-svc")
    out = processor(None, "info", {"service": "explicit"})
    assert out["service"] == "explicit"


_NEW_CONTENT_KEYS = [
    "prompt",
    "user_prompt",
    "user_query",
    "query",
    "question",
    "completion",
    "ai_response",
    "messages",
    "text_delta",
    "tool_input",
    "tool_output",
    "request_payload",
    "response_body",
    "email",
    "user_email",
    "recipient",
    "request",
]


def _default_redact(event_dict: dict[str, Any]) -> MutableMapping[str, Any]:
    return _redact_secrets(DEFAULT_REDACT_KEYS)(None, "info", event_dict)


def test_nested_keys_redacted() -> None:
    out = _redact({"outer": [{"password": "p"}], "t": ({"api_key": "k"},)})
    assert out["outer"] == [{"password": "***REDACTED***"}]
    assert out["t"] == ({"api_key": "***REDACTED***"},)


def test_caller_object_not_mutated() -> None:
    nested = {"outer": [{"password": "p"}], "s": {"x"}, "t": ({"api_key": "k"},)}
    snapshot = copy.deepcopy(nested)
    out = _redact(nested)
    assert nested == snapshot
    assert out["outer"] is not nested["outer"]


def test_bearer_value_scrubbed() -> None:
    out = _redact({"msg": "auth was Bearer abc.def", "n": {"h": ["bearer   x/y="]}})
    assert out["msg"] == "auth was Bearer ***REDACTED***"
    assert out["n"] == {"h": ["Bearer ***REDACTED***"]}


class _BrokenMapping(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def items(self) -> Any:
        raise RuntimeError("boom")


def test_unredactable_value_becomes_error_sentinel() -> None:
    out = _redact({"bad": _BrokenMapping(), "ok": "fine"})
    assert out["bad"] == "***REDACTION_ERROR***"
    assert out["ok"] == "fine"


def test_redaction_depth_bounded() -> None:
    deep: dict[str, Any] = {"leaf": "x"}
    for _ in range(40):
        deep = {"n": deep}
    out = _redact({"deep": deep})
    node: Any = out["deep"]
    for _ in range(32):
        node = node["n"]
    assert node == "***REDACTION_ERROR***"


def test_self_referencing_list_terminates_at_reentry() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    assert _redact({"cyclic": cyclic})["cyclic"] == ["***REDACTION_ERROR***"]


def test_branching_cycle_returns_promptly() -> None:
    n: dict[str, Any] = {"children": []}
    n["children"] += [n, n]
    start = time.monotonic()
    out = _redact({"tree": n})
    assert time.monotonic() - start < 1
    assert out["tree"] == {"children": ["***REDACTION_ERROR***"] * 2}


def test_shared_node_dag_hits_visit_budget() -> None:
    node: list[Any] = ["leaf"]
    for _ in range(32):
        node = [node, node]
    start = time.monotonic()
    out = _redact({"dag": node})
    assert time.monotonic() - start < 1
    assert "***REDACTION_ERROR***" in repr(out)


def test_flat_dict_unaffected_by_budget() -> None:
    event = {f"k{i}": i for i in range(50)}
    assert _redact(event) == event


@pytest.mark.parametrize("key", _NEW_CONTENT_KEYS)
def test_default_keys_include_content_keys(key: str) -> None:
    assert _default_redact({key: "v"})[key] == "***REDACTED***"


def test_non_str_keys_compared_via_str() -> None:
    out = _redact({"d": {1: "a", b"password": "b"}}, extra=frozenset({"1"}))
    assert out["d"][1] == "***REDACTED***"
    assert out["d"]["password"] == "***REDACTED***"


def test_sibling_shared_container_redacted_everywhere() -> None:
    shared = {"password": 1}
    out = _redact({"data": {"a": shared, "b": [shared, shared]}})["data"]
    assert out["a"] == {"password": "***REDACTED***"}
    assert out["b"] == [{"password": "***REDACTED***"}] * 2
