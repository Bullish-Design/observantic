"""
File system monitoring mixin using watchdog.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    PatternMatchingEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from ..core import EventWatcher
from ..exceptions import ConfigurationException, WatcherException


class FileRecord(BaseModel):
    """File system event record."""

    path: str = Field(..., description="Absolute file path")
    event_type: str = Field(..., description="created/modified/deleted/moved")
    is_directory: bool = False
    dest_path: str | None = Field(
        default=None, description="Destination path for 'moved' events"
    )
    timestamp: float = Field(default_factory=time.time)

    model_config = {"frozen": True, "extra": "forbid"}


class FileEventBase(EventWatcher):
    """File system monitoring mixin using watchdog."""

    watch_patterns: list[str] = Field(
        default=["*"], description="File patterns to monitor (e.g., ['*.pdf', '*.txt'])"
    )
    ignore_patterns: list[str] = Field(default=[], description="Patterns to ignore")
    case_sensitive: bool = Field(
        default=True, description="Pattern matching is case-sensitive"
    )
    event_throttle_seconds: float = Field(
        default=0.1, description="Minimum seconds between events per file"
    )

    _observer: BaseObserver | None = PrivateAttr(default=None)
    _watch_path: str | None = PrivateAttr(default=None)
    _last_event_times: dict[str, float] = PrivateAttr(default_factory=dict)

    # ---- state machine extension points ---------------------------------- #

    def _validate_start(self, path: str) -> None:
        if not Path(path).exists():
            raise ConfigurationException(f"Path does not exist: {path}")
        if not Path(path).is_dir():
            raise ConfigurationException(f"Not a directory: {path}")

    def _start_impl(self, path: str, recursive: bool = True, **kwargs: Any) -> None:
        self._watch_path = str(Path(path).resolve())
        self._last_event_times = {}
        self._observer = Observer()
        try:
            self._observer.schedule(
                self._create_handler(), self._watch_path, recursive=recursive
            )
            self._observer.start()
        except Exception as e:
            self._observer = None
            raise WatcherException(f"Failed to start observer: {e}") from e

    def _stop_impl(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is not None and observer.is_alive():
            observer.stop()
            observer.join(timeout=5)  # bounded join (H-19)

    def _default_record_model(self) -> type[Any]:
        return FileRecord

    # ---- throttling ------------------------------------------------------ #

    def _should_throttle(self, path: str) -> bool:
        """True when `path` fired too recently; prunes stale entries (H-19)."""
        if self.event_throttle_seconds <= 0:
            return False

        now = time.time()
        cutoff = now - max(60.0, self.event_throttle_seconds * 100)
        for stale in [p for p, t in self._last_event_times.items() if t < cutoff]:
            del self._last_event_times[stale]

        last_time = self._last_event_times.get(path, 0)
        if now - last_time < self.event_throttle_seconds:
            return True
        self._last_event_times[path] = now
        return False

    # ---- watchdog handler ------------------------------------------------ #

    def _create_handler(self) -> PatternMatchingEventHandler:
        parent = self

        class FileHandler(PatternMatchingEventHandler):
            def __init__(self):
                super().__init__(
                    patterns=parent.watch_patterns,
                    ignore_patterns=parent.ignore_patterns,
                    ignore_directories=False,
                    case_sensitive=parent.case_sensitive,
                )

            def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
                if not event.is_directory and not parent._should_throttle(
                    str(event.src_path)
                ):
                    parent._emit(
                        path=str(Path(str(event.src_path)).resolve()),
                        event_type="created",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_created", event)

            def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
                if not event.is_directory and not parent._should_throttle(
                    str(event.src_path)
                ):
                    parent._emit(
                        path=str(Path(str(event.src_path)).resolve()),
                        event_type="modified",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_modified", event)

            def on_deleted(self, event: FileDeletedEvent) -> None:  # type: ignore[override]
                if not event.is_directory:
                    parent._emit(
                        path=str(Path(str(event.src_path)).resolve()),
                        event_type="deleted",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_deleted", event)

            def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
                if not event.is_directory and not parent._should_throttle(
                    str(event.src_path)
                ):
                    parent._emit(
                        path=str(Path(str(event.src_path)).resolve()),
                        event_type="moved",
                        is_directory=event.is_directory,
                        dest_path=str(Path(str(event.dest_path)).resolve()),
                    )
                    parent._dispatch_hook("on_file_moved", event)

        return FileHandler()
