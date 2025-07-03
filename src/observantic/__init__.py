"""
Observantic: Event monitoring library that bridges external events to 
Eventic Records through customizable hooks.
"""

from .core.base import EventWatcher
from .monitors.file import FileEventBase
from .monitors.sqlite import SQLiteEventBase
from .exceptions import ObservanticException, WatcherException

__version__ = "0.1.0"

__all__ = [
    "EventWatcher",
    "FileEventBase", 
    "SQLiteEventBase",
    "ObservanticException",
    "WatcherException",
]
