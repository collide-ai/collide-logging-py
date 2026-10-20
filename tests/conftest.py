from __future__ import annotations

import importlib.util
from typing import Any

collect_ignore = ["typecheck"]


def _configure_django() -> None:
    if importlib.util.find_spec("django") is None:
        return
    from django.conf import settings

    if settings.configured:
        return
    options: dict[str, Any] = {
        "DEBUG": True,
        "ALLOWED_HOSTS": ["*"],
        "DATABASES": {},
        "INSTALLED_APPS": [],
        "USE_TZ": True,
        "DEFAULT_CHARSET": "utf-8",
    }
    if importlib.util.find_spec("django_q") is not None:
        options["INSTALLED_APPS"] = ["django_q"]
        options["SECRET_KEY"] = "test-only"
        options["Q_CLUSTER"] = {
            "name": "collide-test",
            "sync": True,
            "timeout": 30,
            "retry": 60,
            "broker_class": "tests._django_q_broker.NullBroker",
        }
    settings.configure(**options)


_configure_django()
