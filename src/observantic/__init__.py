from __future__ import annotations

"""
Observantic: Event monitoring library that bridges external events to 
Eventic Records through customizable hooks.
"""

from .config import ObservanticSettings, settings
from .core import EventWatcher, EventicShim, RecordMixin
from .monitors import FileEventBase, SQLiteEventBase, WebhookEventBase

__version__ = "0.2.0"

__all__ = [
    # Core classes
    "EventWatcher",
    "EventicShim", 
    "RecordMixin",
    
    # Watcher implementations
    "FileEventBase",
    "SQLiteEventBase",
    "WebhookEventBase",
    
    # Configuration
    "ObservanticSettings",
    "settings",
]

# Re-export Eventic init for convenience
init = EventicShim.init
