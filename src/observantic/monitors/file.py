from __future__ import annotations

"""
File system monitoring mixin using watchdog.
"""

import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, PrivateAttr
from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    PatternMatchingEventHandler,
)
from watchdog.observers import Observer

from ..core import EventWatcher, RecordMixin


class FileRecord(BaseModel):
    """File system event record."""

    path: str = Field(..., description="Absolute file path")
    event_type: str = Field(..., description="created/modified/deleted/moved")
    is_directory: bool = False
    timestamp: float = Field(default_factory=time.time)

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }


class FileEventBase(EventWatcher, RecordMixin):
    """File system monitoring mixin using watchdog."""

    watch_patterns: list[str] = Field(
        default=["*"], description="File patterns to monitor (e.g., ['*.pdf', '*.txt'])"
    )
    ignore_patterns: list[str] = Field(default=[], description="Patterns to ignore")
    event_throttle_seconds: float = Field(
        default=0.1, description="Minimum seconds between events per file"
    )

    _observer: Optional[Observer] = PrivateAttr(default=None)
    _watch_path: Optional[str] = PrivateAttr(default=None)
    _last_event_times: dict[str, float] = PrivateAttr(default_factory=dict)

    def start_watching(self, path: str, recursive: bool = True) -> None:
        """Begin monitoring directory."""
        super().start_watching(path)

        if not Path(path).exists():
            raise ValueError(f"Path does not exist: {path}")

        self._watch_path = path
        self._observer = Observer()

        # Create handler with pattern matching
        handler = self._create_handler()

        try:
            self._observer.schedule(handler, path, recursive=recursive)
            self._observer.start()
        except Exception as e:
            self._watching = False
            raise RuntimeError(f"Failed to start observer: {e}") from e

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

    def _create_handler(self) -> PatternMatchingEventHandler:
        """Create the watchdog event handler."""
        parent = self

        class FileHandler(PatternMatchingEventHandler):
            def __init__(self):
                super().__init__(
                    patterns=parent.watch_patterns,
                    ignore_patterns=parent.ignore_patterns,
                    ignore_directories=False,
                    case_sensitive=True,
                )

            def on_created(self, event: FileCreatedEvent) -> None:
                if not event.is_directory and not parent._should_throttle(
                    event.src_path
                ):
                    parent._emit(
                        FileRecord,
                        path=str(Path(event.src_path).resolve()),
                        event_type="created",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_created", event)

            def on_modified(self, event: FileModifiedEvent) -> None:
                if not event.is_directory and not parent._should_throttle(
                    event.src_path
                ):
                    parent._emit(
                        FileRecord,
                        path=str(Path(event.src_path).resolve()),
                        event_type="modified",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_modified", event)

            def on_deleted(self, event: FileDeletedEvent) -> None:
                if not event.is_directory:
                    parent._emit(
                        FileRecord,
                        path=str(Path(event.src_path).resolve()),
                        event_type="deleted",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_deleted", event)

            def on_moved(self, event: FileMovedEvent) -> None:
                if not event.is_directory:
                    parent._emit(
                        FileRecord,
                        path=str(Path(event.src_path).resolve()),
                        event_type="moved",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_moved", event)

        return FileHandler()

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
