"""
Observantic: Event monitoring library that bridges external events to
Eventic Records through customizable hooks.

Public API:
* Watchers — ``FileEventBase``, ``SQLiteEventBase``, ``WebhookEventBase``
* Core — ``EventWatcher``
* Configuration — ``settings`` / ``ObservanticSettings``
* Eventic integration — ``init`` / ``is_eventic_ready`` (see observantic._eventic)
"""

from __future__ import annotations

from typing import Any

from ._eventic import init_eventic, is_ready, reset_eventic
from .config import ObservanticSettings, settings
from .core import EventWatcher
from .monitors import FileEventBase, SQLiteEventBase, WebhookEventBase

__version__ = "0.2.0"

__all__ = [
    # Core classes
    "EventWatcher",
    # Watcher implementations
    "FileEventBase",
    "SQLiteEventBase",
    "WebhookEventBase",
    # Configuration
    "ObservanticSettings",
    "settings",
    # Eventic integration
    "init",
    "reset",
    "is_eventic_ready",
]


def init(name: str, database_url: str | None = None, **kwargs: Any) -> Any:
    """Initialize the Eventic runtime.

    ``database_url`` defaults to ``settings.DB_URL`` (from
    ``OBSERVANTIC_DB_URL`` / ``DB_URL``) when omitted. Eventic may only be
    initialized once per process; repeated calls return the existing
    singleton. Call :func:`reset` to tear it down and re-initialize.
    """
    kw: dict[str, Any] = dict(kwargs)
    if database_url is not None:
        kw["database_url"] = database_url
    return init_eventic(name=name, **kw)


def reset() -> None:
    """Tear down the Eventic singleton (``Eventic.reset()``).

    Useful for tests and multi-app processes: after ``reset()`` a fresh
    ``init(...)`` starts clean.
    """
    reset_eventic()


def is_eventic_ready() -> bool:
    """True once ``init`` has been called successfully."""
    return is_ready()
