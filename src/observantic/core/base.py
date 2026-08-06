"""observantic.core.base
========================
Foundation shared by every Observantic watcher.

Guiding principles:
* Fail fast at ``start_watching()`` — the state machine validates *before*
  flipping state and rolls back on failure (H-10).
* The observer thread must never die: all hook/lifecycle errors funnel to
  ``on_error`` and are swallowed (C-04).
* Persistence is explicit and store-bound (eventic 1.1, invariant I5): a
  watcher declares a ``stream`` and ``bind()``s a ``Runtime``; ``_emit()``
  builds a plain pydantic state and, with ``auto_persist=True``, commits it
  through the bound ``Collection`` (compare-and-swap, loud conflicts).
"""

from __future__ import annotations

import logging
from abc import ABC
from collections import defaultdict
from collections.abc import Callable
from threading import Lock
from typing import Any

from eventic import Stream
from eventic.errors import UsageError
from eventic.runtime import Collection, Runtime
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from ..exceptions import ConfigurationException, WatcherException

logger = logging.getLogger("observantic")

# eventic's SQLite(":memory:") store shares one connection across threads
# (StaticPool + check_same_thread=False) and is not safe under concurrent
# create() calls from observer threads. Serialize all observantic persists
# process-wide; the lock is a no-op for file-based SQLite (QueuePool + WAL)
# and Postgres (pooled connections).
_persist_lock = Lock()

HookFn = Callable[..., Any]


class EventWatcher(BaseModel, ABC):
    """
    Abstract base providing the watcher state machine, hook dispatch, and
    optional eventic persistence.

    Subclasses implement the extension points ``_validate_start`` /
    ``_start_impl`` / ``_stop_impl``, declare a ``stream``, and emit states
    with ``_emit`` / fire hooks with ``_dispatch_hook``.
    """

    # ---- eventic integration -------------------------------------------- #
    stream: Stream | None = Field(
        default=None,
        description=(
            "Eventic Stream this watcher emits into. Its model is the "
            "persisted state contract; defaults per monitor (FILE_STREAM, "
            "SQLITE_STREAM, WEBHOOK_STREAM)."
        ),
    )
    auto_persist: bool = Field(
        default=False,
        description="Commit each emitted state to the bound Collection (requires bind())",
    )
    persist_strict: bool = Field(
        default=False,
        description="Raise ConfigurationException when auto_persist is requested but no Collection is bound",
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
    _runtime: Runtime | None = PrivateAttr(default=None)
    _collection: Collection[Any] | None = PrivateAttr(default=None)

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

    # ---- eventic binding ------------------------------------------------- #

    def bind(self, runtime: Runtime) -> None:
        """Bind a Runtime so ``auto_persist`` emits commit to ``self.stream``.

        Idempotent; ``unbind()`` reverses it. Raises ConfigurationException
        when the watcher has no stream, or the stream is not installed in the
        runtime's app.
        """
        if self.stream is None:
            raise ConfigurationException(
                "cannot bind: watcher has no stream (set stream=...)"
            )
        try:
            collection = runtime[self.stream]
        except UsageError as exc:
            raise ConfigurationException(
                f"stream {self.stream.name!r} is not installed in the bound app"
            ) from exc
        self._runtime = runtime
        self._collection = collection

    def unbind(self) -> None:
        """Drop the bound Runtime/Collection."""
        self._runtime = None
        self._collection = None

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
        """Override method (bound) first, then registered callbacks.

        eventic 1.1 has no metaclass wrappers — a plain ``getattr`` is the
        raw hook; no unwrapping is needed.
        """
        candidate = getattr(self, event_name, None)
        fn = candidate if callable(candidate) else None
        with self._lock:
            callbacks = list(self._hooks.get(event_name, ()))
        return ([fn] if fn is not None else []) + callbacks

    def _safe_call(self, name: str, *args: Any) -> None:
        """Invoke a lifecycle hook; failures are logged, never raised."""
        try:
            fn = getattr(self, name, None)
            if fn is not None:
                fn(*args)
        except Exception as e:
            logger.error("lifecycle hook %s failed: %s", name, e, exc_info=True)

    # ---- emission / persistence ----------------------------------------- #

    def _emit(self, **fields: Any) -> Any:
        """Build the state model instance for one external event.

        The model is ``self.stream.model`` when a stream is declared, else
        the monitor's internal record model (``_default_record_model``). With
        ``auto_persist=True`` the state is committed to the bound Collection
        as a new aggregate (revision 0).

        A custom stream model must accept the monitor's emit fields (see
        IMPLEMENTATION_GUIDE.md Appendix A) — a mismatch raises loudly rather
        than silently dropping data.
        """
        model = (
            self.stream.model
            if self.stream is not None
            else self._default_record_model()
        )
        state = model(**fields)
        if self.auto_persist:
            self._persist(state)
        return state

    def _persist(self, state: Any) -> None:
        """Commit one emitted state to the bound Collection."""
        if self._collection is None:
            if self.persist_strict:
                raise ConfigurationException(
                    "auto_persist=True but watcher is not bound; "
                    "call watcher.bind(runtime) first or set auto_persist=False"
                )
            logger.warning(
                "auto_persist=True but watcher is not bound; state not persisted"
            )
            return
        self._commit(state)

    def _commit(
        self,
        state: Any,
        op: Callable[[Collection[Any]], Any] | None = None,
    ) -> None:
        """Run one collection mutation under the persist lock.

        Every observantic write goes through here: it is serialized
        process-wide (see ``_persist_lock``) and failures are reported via
        ``on_error`` and swallowed unless ``persist_strict`` is set —
        persistence is best-effort and the observer thread must never die
        (C-04). Subclass persistence (e.g. keyed aggregates) calls this with
        an ``op`` instead of bypassing the guards.
        """
        collection = self._collection
        if collection is None:
            return  # defensive: callers guard the unbound case first
        with _persist_lock:
            try:
                if op is None:
                    collection.create(state)
                else:
                    op(collection)
            except Exception as e:
                if self.persist_strict:
                    raise
                logger.warning("persist failed (state not committed): %s", e)
                self._safe_call("on_error", e, state)

    def _emit_safe(self, event: Any, **fields: Any) -> Any | None:
        """Emit from an observer thread; NEVER raises (C-04).

        Model or persistence errors route to ``on_error(error, event)`` and
        monitoring continues. Direct ``_emit`` calls still raise, so tests
        and caller code keep loud semantics.
        """
        try:
            return self._emit(**fields)
        except Exception as e:
            self._safe_call("on_error", e, event)
            return None

    def _default_record_model(self) -> type[Any]:
        """Return the monitor's internal state model (subclass contract)."""
        raise NotImplementedError

    # ---- future async placeholder --------------------------------------- #

    async def run_async(self) -> None:
        raise NotImplementedError("Async watchers planned for a later release")
