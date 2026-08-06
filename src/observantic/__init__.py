"""
Observantic: Event monitoring library that bridges external events to
eventic streams through customizable hooks.

Public API:
* Watchers — ``FileEventBase``, ``SQLiteEventBase``, ``WebhookEventBase``
* Core — ``EventWatcher``
* Configuration — ``settings`` / ``ObservanticSettings``
* Eventic integration — ``make_store`` / ``build_app`` (see observantic._eventic)
* Default streams — ``FILE_STREAM``, ``SQLITE_STREAM``, ``WEBHOOK_STREAM``
"""

from __future__ import annotations

from ._eventic import build_app, make_store
from .config import ObservanticSettings, settings
from .core import EventWatcher
from .monitors import (
    FILE_STREAM,
    SQLITE_STREAM,
    WEBHOOK_STREAM,
    FileEventBase,
    SQLiteEventBase,
    WebhookEventBase,
)

__version__ = "0.3.0"

__all__ = [
    # Core classes
    "EventWatcher",
    # Watcher implementations
    "FileEventBase",
    "SQLiteEventBase",
    "WebhookEventBase",
    # Default streams
    "FILE_STREAM",
    "SQLITE_STREAM",
    "WEBHOOK_STREAM",
    # Configuration
    "ObservanticSettings",
    "settings",
    # Eventic integration
    "build_app",
    "make_store",
]
