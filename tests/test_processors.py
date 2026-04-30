from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from collide_logging._processors import _add_service_info, _redact_secrets

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
