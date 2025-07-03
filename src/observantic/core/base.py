from __future__ import annotations

"""observantic.core.base
====================
Minimal, *fully‑functional* foundation shared by every Observantic watcher.

Guiding principles (mirrors project‑wide conventions):
* Pydantic‑powered data structures.
* Copy‑on‑write Record persistence is *delegated* to Eventic.
* Synchronous; all async stubs raise NotImplementedError for future extension.
* ≤80‑char lines / Smalltalk‑style OOP (many small helpers).
"""

from abc import ABC
from collections import defaultdict
from threading import Lock
from typing import Any, Callable, Dict, Optional

from eventic import Eventic, Record
from pydantic import BaseModel, Field, PrivateAttr

# ---------------------------------------------------------------------------
# Eventic shim – zero‑cost façade so users import from `observantic` only.
# ---------------------------------------------------------------------------


class EventicShim:
    """Re‑export Eventic while ensuring singleton init."""

    _instance: Optional[Eventic] = None

    @classmethod
    def init(cls, *args: Any, **kw: Any) -> Eventic:
        cls._instance = Eventic.init(*args, **kw)
        return cls._instance

    @classmethod
    def instance(cls) -> Eventic:
        if cls._instance is None:
            cls._instance = Eventic.instance()
        return cls._instance


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------


class RecordMixin:
    """Inject `_emit()` helper so hooks create immutable versions in one line."""

    @staticmethod
    def _emit(record_cls: type[Record], **fields: Any) -> Record:
        return record_cls(**fields)


# ---------------------------------------------------------------------------
# EventWatcher – life‑cycle & hook dispatch (agnostic of event source).
# ---------------------------------------------------------------------------


HookFn = Callable[[Any], None]


class EventWatcher(BaseModel, ABC):
    """
    Abstract base mixin providing core event monitoring functionality.
    Subclasses should implement specific monitoring logic and call
    registered hooks when events occur.
    """

    _hooks: Dict[str, list[HookFn]] = PrivateAttr(
        default_factory=lambda: defaultdict(list), exclude=True
    )
    _watching: bool = PrivateAttr(default=False, exclude=True)
    _lock: Lock = PrivateAttr(default_factory=Lock, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def register_hook(self, event_name: str, callback: HookFn) -> None:
        """Add callback for event type."""
        if not callable(callback):
            raise ValueError(f"Hook must be callable, got {type(callback)}")
        with self._lock:
            self._hooks[event_name].append(callback)

    def unregister_hook(self, event_name: str, callback: HookFn) -> None:
        """Remove specific callback."""
        with self._lock:
            if event_name in self._hooks and callback in self._hooks[event_name]:
                self._hooks[event_name].remove(callback)

    def _dispatch_hook(self, event_name: str, *args: Any, **kwargs: Any) -> None:
        """Call all registered hooks for an event."""
        # First check for override method
        method = getattr(self, event_name, None)
        if method and callable(method) and method.__name__ == event_name:
            try:
                method(*args, **kwargs)
            except Exception as e:
                self.on_error(e, event_name)
                raise

        # Then call registered callbacks
        for callback in list(self._hooks.get(event_name, [])):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                self.on_error(e, event_name)
                raise

    def start_watching(self, path: str, **kwargs: Any) -> None:
        """Begin monitoring specified path."""
        if self._watching:
            raise RuntimeError("Already watching")
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

    # ------------------------------------------------------------------ #
    # future async support placeholder
    # ------------------------------------------------------------------ #
    async def run_async(self) -> None:
        raise NotImplementedError("Async watchers planned for v1.1")
