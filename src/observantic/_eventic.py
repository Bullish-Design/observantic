"""observantic._eventic
======================
The **only** module that imports Eventic internals. If Eventic differs from
the contract below, only this file changes.

Contract with Eventic 0.1.5+ (the fixed, DBOS-2.29-based release):

* ``Eventic.init(*, name, database_url)`` — keyword-only, **once per process**
  (a second call raises ``RuntimeError``); ``Eventic.reset()`` tears the
  singleton down so the next ``init`` starts fresh. ``observantic.init``
  therefore returns the existing instance on repeated calls (idempotent UX).
* ``Eventic.instance()`` — returns the singleton, raises if not initialized.
* ``RecordMeta`` wraps **only** methods explicitly marked ``@evented`` (opt-in;
  everything else is left untouched), so ``inspect.unwrap`` is a passthrough
  for undecorated hooks and still strips the ``functools.wraps``-based queue
  wrapper on decorated ones. Dispatch never touches DBOS queues by default.
* ``Record`` construction is safe without a store; with a wired store it
  persists a durable **v0** row and fires ``@on.create`` handlers
  (``@on.update`` on later mutations). ``Record._store.append(record)`` is the
  persistence API and works **standalone** (no ``launch()`` required).
* Record subclass names must be unique per process — Eventic queues are keyed
  by class name and duplicate declarations raise at class-definition time.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from .config import DB_URL as _DEFAULT_DB_URL

logger = logging.getLogger("observantic.eventic")

__all__ = [
    "EventicNotReadyError",
    "init_eventic",
    "reset_eventic",
    "is_ready",
    "is_launched",
    "call_unwrapped",
    "is_record_class",
    "persist",
    "can_persist",
]


class EventicNotReadyError(RuntimeError):
    """Raised when persistence is requested before Eventic is ready."""


_initialized = False


def init_eventic(
    *, name: str, database_url: str = _DEFAULT_DB_URL, **kwargs: Any
) -> Any:
    """Thin wrapper over ``Eventic.init`` — keeps observantic decoupled.

    ``Eventic.init`` is keyword-only and may only be called once per process;
    repeated calls return the existing singleton (Eventic 0.1.5 raises on a
    second ``init``, so we short-circuit on our own readiness flag).
    """
    from eventic import Eventic

    global _initialized
    if _initialized:
        return Eventic.instance()
    result = Eventic.init(name=name, database_url=database_url, **kwargs)
    _initialized = True
    return result


def reset_eventic() -> None:
    """Tear down the Eventic singleton so the next ``init`` starts fresh.

    Useful for tests and multi-app processes.
    """
    try:
        from eventic import Eventic

        Eventic.reset()
    except ImportError:  # pragma: no cover - eventic always installed
        pass
    global _initialized
    _initialized = False


def is_ready() -> bool:
    """True once ``init_eventic`` has been called successfully."""
    return _initialized


def is_launched() -> bool:
    """True when the Eventic runtime has been launched (queues usable).

    Not required for persistence (Eventic 0.1.5 appends standalone), but
    needed for ``@evented`` queue processing.
    """
    if not _initialized:
        return False
    try:
        from eventic import Eventic

        instance = Eventic.instance()
        return bool(getattr(instance, "_launched", False))
    except Exception:
        return False


def call_unwrapped(cls: type, name: str) -> Callable[..., Any] | None:
    """Return the raw function ``name`` from ``cls``'s MRO, wrapper-stripped.

    Eventic 0.1.5 only wraps ``@evented``-marked methods (via
    ``functools.wraps``), so ``inspect.unwrap`` strips those and is a no-op
    passthrough for everything else. Returns ``None`` when the class has no
    override for ``name``.
    """
    if not hasattr(cls, name):
        return None
    fn = getattr(cls, name)
    return inspect.unwrap(fn) if callable(fn) else None


def is_record_class(cls: type) -> bool:
    """True when ``cls`` is a subclass of Eventic's Record base."""
    try:
        from eventic.core.record import Record
    except ImportError:
        return False
    return issubclass(cls, Record)


def persist(record: Any) -> None:
    """Append ``record`` to its Eventic store.

    Raises :class:`EventicNotReadyError` when Eventic is not initialized or
    the record's class has no store wired. The append itself works standalone
    (Eventic 0.1.5 falls back to its own session when no DBOS transaction is
    active) and is idempotent for freshly-constructed Records (durable v0 is
    already persisted; ``ON CONFLICT DO NOTHING`` makes the re-append a no-op).
    """
    if not _initialized:
        raise EventicNotReadyError(
            "Persistence requested but Eventic is not initialized. "
            "Call observantic.init(name=..., database_url=...) first, "
            "or set auto_persist=False."
        )
    store = getattr(type(record), "_store", None)
    if store is None:
        raise EventicNotReadyError(
            "Record store is not wired (Eventic not initialized)."
        )
    store.append(record)


def can_persist() -> bool:
    """True when the persistence path (init + wired store) is usable."""
    if not _initialized:
        return False
    try:
        from eventic.core.record import Record as _Record  # noqa: F401
    except ImportError:
        return False
    return True
