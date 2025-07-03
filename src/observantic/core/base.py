"""
Base event watcher with hook registration and dispatch system.
"""

from __future__ import annotations

from abc import ABC
from collections import defaultdict
from typing import Any, Callable
from pydantic import BaseModel, Field

from ..exceptions import WatcherException


class EventWatcher(BaseModel, ABC):
    """
    Abstract base mixin providing core event monitoring functionality.
    Subclasses should implement specific monitoring logic and call 
    registered hooks when events occur.
    """
    
    _hooks: dict[str, list[Callable]] = Field(
        default_factory=lambda: defaultdict(list),
        exclude=True
    )
    _watching: bool = Field(default=False, exclude=True)
    
    model_config = {"arbitrary_types_allowed": True}
    
    def register_hook(self, event_name: str, callback: Callable) -> None:
        """Add callback for event type."""
        if not callable(callback):
            raise WatcherException(
                f"Hook must be callable, got {type(callback)}"
            )
        self._hooks[event_name].append(callback)
    
    def unregister_hook(self, event_name: str, callback: Callable) -> None:
        """Remove specific callback."""
        if event_name in self._hooks and callback in self._hooks[event_name]:
            self._hooks[event_name].remove(callback)
    
    def _dispatch_hook(self, event_name: str, *args, **kwargs) -> None:
        """Call all registered hooks for an event."""
        # First check for override method
        method = getattr(self, event_name, None)
        if method and callable(method) and method.__name__ == event_name:
            try:
                method(*args, **kwargs)
            except Exception as e:
                self.on_error(e, event_name)
                raise WatcherException(
                    f"Hook {event_name} failed: {e}"
                ) from e
        
        # Then call registered callbacks
        for callback in self._hooks.get(event_name, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                self.on_error(e, event_name)
                raise WatcherException(
                    f"Callback {callback.__name__} failed: {e}"
                ) from e
    
    def start_watching(self, path: str, **kwargs) -> None:
        """Begin monitoring specified path."""
        if self._watching:
            raise WatcherException("Already watching")
        self._watching = True
        self.on_start()
    
    def stop_watching(self) -> None:
        """Stop monitoring and cleanup resources."""
        if not self._watching:
            return
        self._watching = False
        self.on_stop()
    
    # Hook methods (override in subclasses)
    def on_start(self) -> None:
        """Called when monitoring begins."""
        pass
    
    def on_stop(self) -> None:
        """Called when monitoring ends."""
        pass
    
    def on_error(self, error: Exception, event: Any = None) -> None:
        """Handle errors during processing."""
        pass
