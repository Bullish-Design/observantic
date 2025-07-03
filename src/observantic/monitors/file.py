"""
File system monitoring mixin using watchdog.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    PatternMatchingEventHandler
)

from ..core.base import EventWatcher
from ..exceptions import WatcherException


class FileEventBase(EventWatcher):
    """File system monitoring mixin using watchdog."""
    
    watch_patterns: list[str] = ["*"]
    ignore_patterns: list[str] = []
    event_throttle_seconds: float = 0.1
    
    _observer: Observer | None = None
    _watch_path: str | None = None
    _last_event_times: dict[str, float] = {}
    
    def start_watching(self, path: str, recursive: bool = True) -> None:
        """Begin monitoring directory."""
        super().start_watching(path)
        
        if not Path(path).exists():
            raise WatcherException(f"Path does not exist: {path}")
        
        self._watch_path = path
        self._observer = Observer()
        
        # Create handler with pattern matching
        handler = _FileEventHandler(
            watcher=self,
            patterns=self.watch_patterns,
            ignore_patterns=self.ignore_patterns,
            ignore_directories=False,
            case_sensitive=True
        )
        
        try:
            self._observer.schedule(handler, path, recursive=recursive)
            self._observer.start()
        except Exception as e:
            self._watching = False
            raise WatcherException(f"Failed to start observer: {e}") from e
    
    def stop_watching(self) -> None:
        """Stop monitoring."""
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join()
            self._observer = None
        
        super().stop_watching()
    
    def _should_throttle(self, path: str) -> bool:
        """Check if event should be throttled."""
        if self.event_throttle_seconds <= 0:
            return False
        
        now = time.time()
        last_time = self._last_event_times.get(path, 0)
        
        if now - last_time < self.event_throttle_seconds:
            return True
        
        self._last_event_times[path] = now
        return False
    
    # Override these in subclasses
    def on_file_created(self, event: FileSystemEvent) -> None:
        """Called when file is created."""
        pass
    
    def on_file_modified(self, event: FileSystemEvent) -> None:
        """Called when file is modified."""
        pass
    
    def on_file_deleted(self, event: FileSystemEvent) -> None:
        """Called when file is deleted."""
        pass
    
    def on_file_moved(self, event: FileMovedEvent) -> None:
        """Called when file is moved/renamed."""
        pass


class _FileEventHandler(PatternMatchingEventHandler):
    """Internal handler that routes watchdog events to FileEventBase hooks."""
    
    def __init__(self, watcher: FileEventBase, **kwargs):
        super().__init__(**kwargs)
        self.watcher = watcher
    
    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory and not self.watcher._should_throttle(event.src_path):
            self.watcher._dispatch_hook("on_file_created", event)
    
    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory and not self.watcher._should_throttle(event.src_path):
            self.watcher._dispatch_hook("on_file_modified", event)
    
    def on_deleted(self, event: FileDeletedEvent) -> None:
        if not event.is_directory:
            self.watcher._dispatch_hook("on_file_deleted", event)
    
    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self.watcher._dispatch_hook("on_file_moved", event)
