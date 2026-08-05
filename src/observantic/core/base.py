"""observantic.core.base
========================
Minimal, fully-functional foundation shared by every Observantic watcher.

Guiding principles:
* Fail fast at ``start_watching()`` — the state machine validates *before*
  flipping state and rolls back on failure (H-10).
* The observer thread must never die: all hook/lifecycle errors funnel to
  ``on_error`` and are swallowed (C-04).
* Dispatch bypasses Eventic's metaclass wrappers by default
  (``call_unwrapped`` from the ``observantic._eventic`` seam), so
  Record-based watchers work without ``launch()`` and never double-execute
  (C-03).
* Persistence is opt-in (``auto_persist`` + ``record_model``) and honest (C-08).
"""

from __future__ import annotations

import logging
from abc import ABC
from collections import defaultdict
from collections.abc import Callable
from threading import Lock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from .._eventic import EventicNotReadyError, call_unwrapped, is_record_class, persist
from ..exceptions import ConfigurationException, WatcherException

logger = logging.getLogger("observantic")

HookFn = Callable[..., Any]


class EventWatcher(BaseModel, ABC):
    """
    Abstract base providing the watcher state machine, hook dispatch, and
    optional Eventic persistence.

    Subclasses implement the extension points ``_validate_start`` /
    ``_start_impl`` / ``_stop_impl``, emit records with ``_emit``, and fire
    hooks with ``_dispatch_hook``.
    """

    # ---- dispatch / persistence knobs ---------------------------------- #
    record_model: type[Any] | None = Field(
        default=None,
        description="Model emitted per event; defaults to the monitor's internal record model",
    )
    auto_persist: bool = Field(
        default=False,
        description="Append emitted records to Eventic's store (requires observantic.init; Eventic 0.1.5 also persists a durable v0 row at construction when a store is wired)",
    )
    persist_strict: bool = Field(
        default=False,
        description="Raise ConfigurationException when auto_persist is requested but Eventic is not ready",
    )
    dispatch_direct: bool = Field(
        default=True,
        description="Bypass Eventic metaclass wrappers when dispatching hooks (recommended)",
    )
    raise_on_hook_error: bool = Field(
        default=False,
        description="Collect hook errors instead of swallowing them",
    )

    _hooks: dict[str, list[HookFn]] = PrivateAttr(
        default_factory=lambda: defaultdict(list)
    )
    _watching: bool = PrivateAttr(default=False)
    _lock: Lock = PrivateAttr(default_factory=Lock)
    _last_hook_error: Exception | None = PrivateAttr(default=None)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ---- hook registry -------------------------------------------------- #

    def register_hook(self, event_name: str, callback: HookFn) -> None:
        """Add a callback for an event name. Must be callable."""
        if not callable(callback):
            raise ConfigurationException(f"Hook must be callable, got {type(callback)}")
        with self._lock:
            self._hooks[event_name].append(callback)

    def unregister_hook(self, event_name: str, callback: HookFn) -> None:
        """Remove a previously registered callback (no-op if absent)."""
        with self._lock:
            if event_name in self._hooks and callback in self._hooks[event_name]:
                self._hooks[event_name].remove(callback)

    # ---- lifecycle state machine ---------------------------------------- #

    def start_watching(self, path: str, **kwargs: Any) -> None:
        """Begin monitoring; validates *before* flipping state (H-10)."""
        with self._lock:
            if self._watching:
                raise WatcherException("Already watching")
        self._validate_start(path)  # subclass hook; raises ConfigurationException
        with self._lock:
            self._watching = True
        try:
            self._start_impl(path, **kwargs)  # subclass hook
        except Exception:
            with self._lock:
                self._watching = False  # rollback
            raise
        self._safe_call("on_start")

    def stop_watching(self) -> None:
        """Stop monitoring; idempotent. Never raises into the caller."""
        with self._lock:
            if not self._watching:
                return
            self._watching = False  # flip first so stop is idempotent
        try:
            self._stop_impl()  # subclass hook
        except Exception as e:
            self._safe_call("on_error", e, None)
        finally:
            self._safe_call("on_stop")

    # ---- subclass extension points -------------------------------------- #

    def _validate_start(self, path: str) -> None:
        """Validate before state flips. Raise ConfigurationException on error."""

    def _start_impl(self, path: str, **kwargs: Any) -> None:
        """Actually begin monitoring (observer threads, servers)."""

    def _stop_impl(self) -> None:
        """Tear down resources. Must be bounded."""

    # ---- hook dispatch -------------------------------------------------- #

    def _dispatch_hook(
        self, event_name: str, *args: Any, **kwargs: Any
    ) -> Exception | None:
        """Invoke the override method + registered callbacks. NEVER raises.

        Errors are reported via ``on_error(error, event)`` and swallowed; if
        ``raise_on_hook_error`` is set they are collected in
        ``_last_hook_error`` (cleared at the start of each dispatch) and the
        last error is returned to the caller — e.g. the webhook 500 decision.
        """
        if self.raise_on_hook_error:
            self._last_hook_error = None
        event = args[0] if args else None
        for hook in self._hook_callables(event_name):
            try:
                hook(*args, **kwargs)
            except Exception as e:
                self._safe_call("on_error", e, event)
                if self.raise_on_hook_error:
                    self._last_hook_error = e
        return self._last_hook_error if self.raise_on_hook_error else None

    def _hook_callables(self, event_name: str) -> list[HookFn]:
        """Override method (wrapper-stripped, bound) first, then callbacks."""
        fn: HookFn | None
        if self.dispatch_direct:
            raw = call_unwrapped(type(self), event_name)
            fn = raw.__get__(self) if raw is not None else None
        else:
            candidate = getattr(self, event_name, None)
            fn = candidate if callable(candidate) else None
        with self._lock:
            callbacks = list(self._hooks.get(event_name, ()))
        return ([fn] if fn is not None else []) + callbacks

    def _safe_call(self, name: str, *args: Any) -> None:
        """Invoke a lifecycle hook; failures are logged, never raised."""
        try:
            fn = call_unwrapped(type(self), name)
            if fn is not None:
                fn(self, *args)
        except Exception as e:
            logger.error("lifecycle hook %s failed: %s", name, e, exc_info=True)

    # ---- emission / persistence ----------------------------------------- #

    def _emit(self, **fields: Any) -> Any:
        """Create an event record.

        The emitted model is, in order of preference: the user-set
        ``record_model``; the watcher's own class when it is a Record
        subclass (so ``_emit()`` creates *your* record — C-08); or the
        monitor's internal record model.

        With ``auto_persist=True`` the record is also appended to Eventic's
        store (an idempotent re-append for Record-based models — Eventic
        0.1.5 already persists the durable v0 row at construction and fires
        ``@on.create``); a missing Eventic degrades to a warning unless
        ``persist_strict=True`` (which raises ConfigurationException).
        """
        model = self.record_model
        if model is None:
            if is_record_class(type(self)):
                model = type(self)
            else:
                model = self._default_record_model()
        record = model(**fields)
        if self.auto_persist:
            try:
                persist(record)
            except EventicNotReadyError as e:
                if self.persist_strict:
                    raise ConfigurationException(str(e)) from e
                logger.warning(
                    "auto_persist=True but Eventic not ready; record not persisted"
                )
        return record

    def _default_record_model(self) -> type[Any]:
        """Return the monitor's internal record model (subclass contract)."""
        raise NotImplementedError

    # ---- future async placeholder --------------------------------------- #

    async def run_async(self) -> None:
        raise NotImplementedError("Async watchers planned for v1.1")
