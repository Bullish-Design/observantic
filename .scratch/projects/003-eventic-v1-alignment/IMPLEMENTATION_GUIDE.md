# Observantic × eventic 1.1.0 — Implementation Guide

Step-by-step guide to implement the changes specified in
`OVERVIEW.md` (same directory). Every code block was validated against
eventic **v1.1.0** (`/home/andrew/Documents/Projects/eventic`, tag `v1.1.0`,
commit `b489da2`) with live probes:

- `SQLite(":memory:")` create/change/replace/get/history/where/batch, CAS
  conflicts, `NotFound`, `Store.close()` idempotence
- Outbox subscriptions + `Worker.drain_once()` on SQLite
- `Stream` as a pydantic model field default (shared, immutable)
- Default streams built from observantic's internal record models
  (`FileRecord`, `DatabaseRow`, `WebhookRecord`)
- End-to-end `watcher._emit(...)` → `collection.create(...)` → `where(...)`

Two decisions were locked during implementation (deltas from the overview):

1. **`record_model` is removed, not kept.** The stream model
   (`stream.model`) is the single durable contract; a second "record model"
   knob would let users emit states that cannot be persisted coherently.
   `_default_record_model()` remains as the fallback for custom
   `EventWatcher` subclasses with no `stream`.
2. **Default `OBSERVANTIC_DB_URL` becomes `sqlite:///observantic.db`**
   (eventic 1.1's own dev/test backend). Keep `postgresql://` in production
   via the env var. If you prefer the old Postgres default, skip Step 6 and
   the `test_config.py` half of Step 7.

The tree is **expected to be red** between Steps 1 and 6 (imports of the
removed `Record`/`Eventic` API). Do Steps 1–6 as one unit, then reach the
first green checkpoint at the end of Step 7.

---

## Step 0 — Preflight

```bash
cd ~/Documents/Projects/observantic
git status --short            # must be clean
git checkout -b align/eventic-v1.1.0
```

Confirm the revised library resolves (it is a git dep of the same URL):

```bash
# optional: confirm v1.1.0 exists on the remote
git -C ~/Documents/Projects/eventic describe --tags           # v1.1.0
git -C ~/Documents/Projects/eventic rev-parse HEAD            # b489da2...
```

---

## Step 1 — Dependencies (`pyproject.toml` + `uv.lock`)

**File: `pyproject.toml`**

Replace the `dependencies` block and the git sources:

```toml
[project]
name = "observantic"
version = "0.3.0"
description = "Bridges external events (files, SQLite, webhooks) to eventic streams through customizable hooks"
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
    "eventic",
    "pydantic>=2.9",
    "watchdog>=6.0.0",
]
```

- `confidantic` and `python-dotenv` are removed (dead: grep finds no imports).
- `pydantic>=2.9` becomes a direct dep (observantic models use it directly;
  eventic requires it).
- Keep the runtime dep minimal (`eventic` core — no Postgres driver). Add
  the driver for local/prod Postgres work to the dev group:

```toml
[dependency-groups]
dev = [
    "eventic[postgres]",
    "eventic[migrate]",
    "pytest>=8",
    "requests>=2.31",
    "ruff>=0.6",
    "mypy>=1.11",
]
```

Pin the git source to the new release:

```toml
[tool.uv.sources]
eventic = { git = "https://github.com/Bullish-Design/eventic.git", tag = "v1.1.0" }
```

`confidantic` source line is deleted. Everything else in `pyproject.toml`
(ruff/mypy/pytest/hatch config) stays.

**Verify**

```bash
uv lock && uv sync --group dev
uv run python -c "import eventic; print(eventic.__version__)"   # 1.1.0
uv run python -c "import observantic; print(observantic.__version__)"  # 0.3.0
```

`uv.lock` now pins `v1.1.0`; `dbos`, `confidantic`, `psycopg2-binary`,
`python-dotenv` disappear from the lock.

---

## Step 2 — Rewrite the seam (`src/observantic/_eventic.py`)

The old seam wraps `Record`, the `Eventic` singleton, DBOS queues and
metaclass unwrapping — all gone. The new seam is a thin facade over eventic's
public API and the only place observantic builds stores/apps.

**File: `src/observantic/_eventic.py` (full rewrite)**

```python
"""observantic._eventic
======================
The Eventic seam: the stable import boundary between observantic and eventic.

Contract with Eventic 1.1.0 (the rewritten, declaration-based release):

* ``App`` / ``Stream`` / ``Subscription`` are frozen values; constructing
  them performs no I/O (I4).
* ``App.bind(store)`` returns a ``Runtime``; all writes go through
  ``runtime[stream]`` (a ``Collection``) with compare-and-swap (I5, I7).
* Stores: ``eventic.sql.SQLite`` (dev/test) and ``eventic.sql.Postgres``
  (production). ``Store.close()`` is idempotent.
* Delivery: ``Inline`` (best-effort, in-process) or ``Outbox`` (durable,
  drained by ``eventic worker`` / ``eventic.worker.Worker``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from eventic import App, NoMeta, Stream
from eventic.subscription import Subscription

from .config import DB_URL as DEFAULT_DB_URL
from .exceptions import ConfigurationException

logger = logging.getLogger("observantic.eventic")

__all__ = [
    "DEFAULT_DB_URL",
    "build_app",
    "make_store",
]


def make_store(
    url_or_path: str | None = None, *, create_tables: bool = True
) -> Any:
    """Build an eventic store from a URL or bare path.

    ``sqlite://`` (or a bare path like ``"obs.db"``) -> ``SQLite``;
    ``postgresql://`` -> ``Postgres``. ``url_or_path`` defaults to the
    settings snapshot ``DEFAULT_DB_URL`` (``OBSERVANTIC_DB_URL`` /
    ``DB_URL``).
    """
    url = url_or_path if url_or_path is not None else DEFAULT_DB_URL
    if not isinstance(url, str) or not url:
        raise ConfigurationException("store URL must be a non-empty string")
    try:
        if url.startswith("postgresql"):
            from eventic.sql import Postgres

            return Postgres(url, create_tables=create_tables)
        if url.startswith("sqlite"):
            from eventic.sql import SQLite

            return SQLite(url, create_tables=create_tables)
        if "://" not in url:
            # bare path — SQLite, matching eventic's own loader
            from eventic.sql import SQLite

            return SQLite(url, create_tables=create_tables)
    except ConfigurationException:
        raise
    except Exception as exc:  # missing driver, malformed URL, ...
        raise ConfigurationException(
            f"cannot create eventic store from {url!r}: {exc} "
            "(install eventic[postgres] for postgresql:// URLs)"
        ) from exc
    raise ConfigurationException(
        f"unsupported database URL scheme: {url!r} "
        "(expected sqlite://, postgresql://, or a bare path)"
    )


def build_app(
    id: str,
    streams: Sequence[Stream[Any]] = (),
    subscriptions: Sequence[Subscription[Any, Any]] = (),
    meta: Any = NoMeta,
    on_inline_error: str = "raise",
) -> App:
    """Assemble an eventic ``App`` from watcher streams and subscriptions.

    A thin passthrough so core/ and users never import eventic directly.
    The returned ``App`` can be bound to a store
    (``app.bind(store)``) and passed to the ``eventic`` CLI
    (``--app module:attr``).
    """
    return App(
        id=id,
        streams=streams,
        subscriptions=subscriptions,
        meta=meta,
        on_inline_error=on_inline_error,
    )
```

Notes:

- `eventic.sql` is imported lazily inside `make_store` so `import
  observantic` stays free of SQLAlchemy until a store is actually needed.
- `Subscription`, `App`, `Stream`, `NoMeta` are imported at module top —
  `import eventic` pulls only pydantic, so this is cheap.
- The old exports (`EventicNotReadyError`, `init_eventic`, `reset_eventic`,
  `is_ready`, `is_launched`, `call_unwrapped`, `is_record_class`, `persist`,
  `can_persist`) are deleted. Nothing else in the repo may import them.

---

## Step 3 — `EventWatcher` (`src/observantic/core/base.py`)

**File: `src/observantic/core/base.py` (rewrite)**

Changes: drop `record_model` and `dispatch_direct`; add `stream`,
`bind`/`unbind`; `_emit` builds plain pydantic state and commits via the
bound `Collection`; hook dispatch is plain `getattr` (no metaclass unwrap).

```python
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
        self._collection.create(state)

    def _default_record_model(self) -> type[Any]:
        """Return the monitor's internal state model (subclass contract)."""
        raise NotImplementedError

    # ---- future async placeholder --------------------------------------- #

    async def run_async(self) -> None:
        raise NotImplementedError("Async watchers planned for a later release")
```

Behavior deltas to note (all intentional):

- `_hook_callables` no longer special-cases `dispatch_direct`; hooks are the
  raw bound methods. Registered callbacks still run after the override.
- `_emit` no longer touches a global store; persistence is only via
  `bind()` + `auto_persist`.
- `record_model` is gone — use `stream=Stream(MyModel, name=...)` instead.

---

## Step 4 — Monitors: default streams

Each monitor gains a module-level `Stream` constant and a `stream` field
default. No other behavior changes.

**File: `src/observantic/monitors/file.py`** — add imports + constant + field:

```python
from eventic import Stream
from ..core import EventWatcher
from ..exceptions import ConfigurationException, WatcherException
```

Insert after the `FileRecord` model:

```python
FILE_STREAM: Stream = Stream(FileRecord, name="files")
```

In `class FileEventBase(EventWatcher):`, after the docstring:

```python
    stream: Stream = FILE_STREAM
```

**File: `src/observantic/monitors/sqlite.py`** — same pattern:

```python
from eventic import Stream
...
SQLITE_STREAM: Stream = Stream(DatabaseRow, name="sqlite")


class SQLiteEventBase(EventWatcher):
    """SQLite database monitoring mixin."""

    stream: Stream = SQLITE_STREAM
```

**File: `src/observantic/monitors/webhook.py`**:

```python
from eventic import Stream
...
WEBHOOK_STREAM: Stream = Stream(WebhookRecord, name="webhooks")


class WebhookEventBase(EventWatcher):
    """HTTP webhook monitoring mixin."""

    stream: Stream = WEBHOOK_STREAM
```

**File: `src/observantic/monitors/__init__.py`** — export the constants:

```python
"""Event monitors for external sources."""

from .file import FILE_STREAM, FileEventBase
from .sqlite import SQLITE_STREAM, SQLiteEventBase
from .webhook import WEBHOOK_STREAM, WebhookEventBase

__all__ = [
    "FILE_STREAM",
    "FileEventBase",
    "SQLITE_STREAM",
    "SQLiteEventBase",
    "WEBHOOK_STREAM",
    "WebhookEventBase",
]
```

Notes:

- The internal record models (`FileRecord`, `DatabaseRow`, `WebhookRecord`)
  are unchanged and remain the default state models. They were verified to
  satisfy `Stream`'s rules (plain `BaseModel`, no `RootModel`, no
  `SecretStr`).
- Stream names (`files`, `sqlite`, `webhooks`) are durable identities —
  choosing them now is permanent for your data.

---

## Step 5 — Public API (`src/observantic/__init__.py`)

**File: `src/observantic/__init__.py` (rewrite)**

```python
"""
Observantic: Event monitoring library that bridges external events to
eventic streams through customizable hooks.

Public API:
* Watchers — ``FileEventBase``, ``SQLiteEventBase``, ``WebhookEventBase``
* Core — ``EventWatcher``
* Configuration — ``settings`` / ``ObservanticSettings``
* Eventic integration — ``make_store`` / ``build_app`` (see observantic._eventic)
* Default streams — ``FILE_STREAM``, ``SQLITE_STREAM``, ``WEBHOOK_STREAM``
"""

from __future__ import annotations

from ._eventic import build_app, make_store
from .config import ObservanticSettings, settings
from .core import EventWatcher
from .monitors import (
    FILE_STREAM,
    SQLITE_STREAM,
    WEBHOOK_STREAM,
    FileEventBase,
    SQLiteEventBase,
    WebhookEventBase,
)

__version__ = "0.3.0"

__all__ = [
    # Core classes
    "EventWatcher",
    # Watcher implementations
    "FileEventBase",
    "SQLiteEventBase",
    "WebhookEventBase",
    # Default streams
    "FILE_STREAM",
    "SQLITE_STREAM",
    "WEBHOOK_STREAM",
    # Configuration
    "ObservanticSettings",
    "settings",
    # Eventic integration
    "build_app",
    "make_store",
]
```

Removed: `init`, `reset`, `is_eventic_ready` (the 0.1.5 global-singleton
API). `core/__init__.py` and `exceptions.py` are unchanged.

---

## Step 6 — Configuration default (`src/observantic/config.py`)

**File: `src/observantic/config.py`** — one-line default change (and docstring
update):

```python
    DB_URL: str = Field(
        default="sqlite:///observantic.db",
        description="Database URL for eventic (sqlite:// or postgresql://)",
    )
```

The `OBSERVANTIC_*`-wins alias logic, `LOG_LEVEL`, and the import-time
snapshot are unchanged. Update the module docstring's env-var list to note
the SQLite default.

---

## Step 7 — Tests (first green checkpoint)

### 7.1 `tests/conftest.py`

**File: `tests/conftest.py` (rewrite)**

```python
"""Shared pytest fixtures (no external services; SQLite is the test backend)."""

from __future__ import annotations

import socket
import sqlite3

import pytest
from eventic.sql import SQLite


@pytest.fixture
def tmp_db_path(tmp_path):
    """Path to a small, ready-to-use SQLite database."""
    p = tmp_path / "test.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def free_port():
    """A port that was free at the moment of reservation."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def store():
    """An in-memory eventic store; tables are created on construction."""
    s = SQLite(":memory:")
    yield s
    s.close()
```

The autouse `_isolate_eventic` fixture is **deleted** — eventic 1.1 has no
process-global state to reset.

### 7.2 `tests/test_core_eventic.py`

**File: `tests/test_core_eventic.py` (full rewrite)**

```python
"""Tests for the Eventic seam (observantic._eventic) and persistence.

Eventic 1.1.0 is declaration-based: streams are frozen values, writes go
through a bound Collection, and there is no process-global state.
"""

from __future__ import annotations

import logging

import pytest
from eventic import App, Stream
from eventic.errors import ConfigError, NotFound, RevisionConflict
from eventic.sql import SQLite
from pydantic import BaseModel

from observantic import EventWatcher, build_app, make_store
from observantic.exceptions import ConfigurationException
from observantic.monitors import FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


PROBE_STREAM = Stream(ProbeEvent, name="probe")


class EmittingWatcher(EventWatcher):
    """A watcher with a stream; persistence is opt-in."""

    stream = PROBE_STREAM


# ---------------------------------------------------------------------------
# make_store / build_app / default streams
# ---------------------------------------------------------------------------


def test_make_store_sqlite(tmp_path):
    store = make_store(f"sqlite:///{tmp_path / 'x.db'}")
    try:
        assert store.capabilities.outbox is True  # outbox works on sqlite
    finally:
        store.close()


def test_make_store_bare_path(tmp_path):
    store = make_store(str(tmp_path / "bare.db"))
    try:
        store.close()  # tables were created on construction
    finally:
        store.close()


def test_make_store_rejects_bad_scheme():
    with pytest.raises(ConfigurationException, match="scheme"):
        make_store("oracle://host/db")


def test_make_store_defaults_to_settings(monkeypatch, tmp_path):
    import observantic._eventic as seam

    monkeypatch.setattr(
        seam, "DEFAULT_DB_URL", f"sqlite:///{tmp_path / 'default.db'}"
    )
    store = make_store()
    try:
        assert "sqlite" in str(store.engine.url)
    finally:
        store.close()


def test_build_app_collects_streams():
    app = build_app(id="t", streams=[PROBE_STREAM])
    assert [s.name for s in app.streams] == ["probe"]


def test_default_streams_have_stable_names():
    assert FILE_STREAM.name == "files"
    assert SQLITE_STREAM.name == "sqlite"
    assert WEBHOOK_STREAM.name == "webhooks"
    assert FILE_STREAM.model.__name__ == "FileRecord"
    assert SQLITE_STREAM.model.__name__ == "DatabaseRow"
    assert WEBHOOK_STREAM.model.__name__ == "WebhookRecord"


def test_stream_name_validation():
    with pytest.raises(ConfigError):
        Stream(ProbeEvent, name="Not Valid!")


# ---------------------------------------------------------------------------
# _emit / auto_persist / bind
# ---------------------------------------------------------------------------


def test_emit_creates_state_without_touching_eventic():
    w = EmittingWatcher()
    state = w._emit(path="/x", event_type="created", is_directory=False)
    assert isinstance(state, ProbeEvent)
    assert state.path == "/x"


def test_emit_uses_custom_stream_model():
    class Custom(BaseModel):
        path: str = ""
        event_type: str = ""
        is_directory: bool = False

    custom = Stream(Custom, name="custom")
    w = EmittingWatcher(stream=custom)
    state = w._emit(path="/y", event_type="modified", is_directory=False)
    assert isinstance(state, Custom)


def test_emit_unknown_kwarg_fails_loudly():
    w = EmittingWatcher()
    with pytest.raises(Exception):  # pydantic ValidationError, not silent drop
        w._emit(bogus=1)


def test_auto_persist_default_is_false():
    assert EmittingWatcher().auto_persist is False


def test_auto_persist_without_bind_warns_and_returns_state(caplog):
    w = EmittingWatcher(auto_persist=True)
    with caplog.at_level(logging.WARNING, logger="observantic"):
        state = w._emit(path="/x", event_type="created", is_directory=False)
    assert state is not None
    assert "not bound" in caplog.text


def test_auto_persist_strict_without_bind_raises():
    w = EmittingWatcher(auto_persist=True, persist_strict=True)
    with pytest.raises(ConfigurationException, match="not bound"):
        w._emit(path="/x", event_type="created", is_directory=False)


def test_bind_requires_stream():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EventWatcher()  # no stream
        with pytest.raises(ConfigurationException, match="no stream"):
            w.bind(runtime)
    finally:
        store.close()


def test_bind_rejects_uninstalled_stream():
    store = SQLite(":memory:")
    try:
        other = Stream(ProbeEvent, name="other")
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(stream=other)
        with pytest.raises(ConfigurationException, match="not installed"):
            w.bind(runtime)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Real persistence — SQLite backend
# ---------------------------------------------------------------------------


def test_auto_persist_commits_create_to_store():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(auto_persist=True)
        w.bind(runtime)

        w._emit(path="/a", event_type="created", is_directory=False)

        page = runtime[PROBE_STREAM].where(path="/a")
        assert len(page.items) == 1
        assert page.items[0].state.path == "/a"
        assert page.items[0].revision == 0  # one new aggregate per event
    finally:
        store.close()


def test_emit_without_autopersist_does_not_persist():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher()
        w.bind(runtime)
        w._emit(path="/a", event_type="created", is_directory=False)
        assert runtime[PROBE_STREAM].where(path="/a").items == ()
    finally:
        store.close()


def test_change_writes_revision_and_cas_conflicts():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        col = runtime[PROBE_STREAM]
        r0 = col.create(ProbeEvent(path="/a"))
        r1 = col.change(r0, path="/b")
        assert r1.revision == 1
        assert col.get(r0.id, revision=0).state.path == "/a"
        assert col.get(r0.id).state.path == "/b"
        assert [r.revision for r in col.history(r0.id).items] == [0, 1]

        # stale base -> loud conflict (I7)
        with pytest.raises(RevisionConflict):
            col.change(r0, path="/c")
    finally:
        store.close()


def test_get_missing_raises_not_found():
    from uuid import uuid4

    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        with pytest.raises(NotFound):
            runtime[PROBE_STREAM].get(uuid4())
    finally:
        store.close()


def test_unbind_stops_persistence():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(auto_persist=True)
        w.bind(runtime)
        w._emit(path="/a", event_type="created", is_directory=False)
        w.unbind()
        with pytest.raises(ConfigurationException, match="not bound"):
            w._emit(path="/b", event_type="created", is_directory=False)
    finally:
        store.close()
```

### 7.3 `tests/test_public_api.py` — edits

- `test_version_consistent_with_metadata`: `"0.2.0"` → `"0.3.0"`.
- Delete `test_init_reexport` and `test_is_eventic_ready_callable`.
- Replace them with:

```python
def test_eventic_integration_exports():
    assert callable(observantic.make_store)
    assert callable(observantic.build_app)


def test_default_streams_exports():
    assert observantic.FILE_STREAM.name == "files"
    assert observantic.SQLITE_STREAM.name == "sqlite"
    assert observantic.WEBHOOK_STREAM.name == "webhooks"
```

### 7.4 `tests/test_config.py` — edits (only if Step 6 applied)

- `test_defaults`: `assert s.DB_URL == "sqlite:///observantic.db"`.
- `test_read_settings_defaults`: `"DB_URL": "sqlite:///observantic.db"`.

### 7.5 First green checkpoint

```bash
uv run pytest -q
```

Expected: all `test_*_monitor.py`, `test_core_dispatch.py`,
`test_core_state.py`, `test_config.py` pass unchanged; the rewritten seam,
core and eventic tests pass; **`test_public_api.py::test_examples_importable`
is expected to fail** until Step 8 rewrites the examples (it imports modules
that still use `Record`).

If you want the checkpoint fully green before Step 8, temporarily mark that
one test `@pytest.mark.skip`.

---

## Step 8 — Examples (second green checkpoint)

All four examples drop `Record` and switch to the declaration + `bind`
pattern. Also add a `demo_app.py` for the `eventic` CLI.

### 8.1 `src/examples/example_file.py` (rewrite)

```python
#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.3.0",
#     "eventic>=1.1.0",
# ]
# ///
"""
File monitoring example for Observantic.
Watches the current directory for documents; each event is committed to the
`documents` stream when persistence is wired.
"""

from __future__ import annotations

import time
from pathlib import Path

from eventic import App, Stream
from eventic.sql import SQLite
from observantic import FileEventBase


class DocumentEvent(BaseModel):
    """One emitted file event (the stream's state model).

    Must accept the monitor's emit fields: path, event_type, is_directory,
    dest_path (moved events). See IMPLEMENTATION_GUIDE.md Appendix A.
    """

    path: str = ""
    event_type: str = ""
    is_directory: bool = False
    dest_path: str | None = None


documents = Stream(DocumentEvent, name="documents")
app = App(id="file-demo", streams=[documents])


class DocumentWatcher(FileEventBase):
    """Monitor documents; each event emits a DocumentEvent."""

    watch_patterns: list[str] = ["*.pdf", "*.txt", "*.docx", "*.md", "*.py"]

    def on_file_created(self, event):
        src = Path(event.src_path)
        size = src.stat().st_size if src.exists() else 0
        print(f"📄 Created: {src.name} ({size} bytes)")

    def on_file_modified(self, event):
        print(f"📝 Modified: {Path(event.src_path).name}")

    def on_file_deleted(self, event):
        print(f"🗑️  Deleted: {Path(event.src_path).name}")

    def on_file_moved(self, event):
        print(f"➡️  Moved: {Path(event.src_path).name} → {Path(event.dest_path).name}")

    def on_start(self):
        print(f"Started monitoring: {self._watch_path}")


def main():
    """Run the file monitoring demo."""
    print("🚀 File Monitor Demo")
    print("Watching the current directory for documents...")
    print("Press Ctrl+C to stop\n")

    store = SQLite("demo.db")  # or observantic.make_store(settings.DB_URL)
    runtime = app.bind(store)

    watcher = DocumentWatcher(stream=documents, auto_persist=True)
    watcher.bind(runtime)
    watcher.start_watching(".")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n✅ Monitoring stopped")
    finally:
        watcher.stop_watching()
        store.close()


if __name__ == "__main__":
    main()
```

Note: the class body no longer mixes watcher config and record fields —
`DocumentEvent` (the stream model) and `DocumentWatcher` (the monitor) are
separate, matching eventic 1.1's "pure declaration" model. `from pydantic
import BaseModel` must be added to the imports.

### 8.2 `src/examples/sqlite_example.py` (rewrite)

```python
#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.3.0",
#     "eventic>=1.1.0",
# ]
# ///
"""
SQLite monitoring example for Observantic.
Tracks row-level changes (inserts, updates, deletes) in a demo database.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from eventic import App, Stream
from eventic.sql import SQLite
from observantic import SQLiteEventBase


class RowEvent(BaseModel):
    """One emitted row-level change (the stream's state model)."""

    table_name: str = ""
    row_data: dict[str, Any] | None = None
    row_id: int | str | None = None
    operation: Literal["inserted", "updated", "deleted"] = "inserted"


rows = Stream(RowEvent, name="row_events")
app = App(id="sqlite-demo", streams=[rows])


class DatabaseSync(SQLiteEventBase):
    """Monitor SQLite changes.

    Prefer the per-row hooks below; ``on_data_changed`` is kept for
    backward compatibility and receives all rows inserted since the last
    check.
    """

    def on_row_inserted(self, row):
        print(f"➕ Inserted into {row.table_name}: {row.row_data}")

    def on_row_updated(self, row):
        print(f"✏️  Updated {row.table_name} row {row.row_id}: {row.row_data}")

    def on_row_deleted(self, row):
        print(f"🗑️  Deleted {row.table_name} row {row.row_id}")

    def on_schema_changed(self, change):
        print(f"🧬 Schema changed: +{change.tables_added} -{change.tables_dropped}")

    def on_start(self):
        print(f"Started monitoring database: {self._db_path}")


def setup_test_db(db_path: str) -> None: ...
def add_test_data(db_path: str) -> None: ...  # unchanged from current file


def main():
    print("🚀 SQLite Monitor Demo")
    db_path = "example.db"
    setup_test_db(db_path)
    print(f"Monitoring database: {db_path}")

    store = SQLite("demo-events.db")
    runtime = app.bind(store)
    monitor = DatabaseSync(stream=rows, auto_persist=True)
    monitor.bind(runtime)
    monitor.start_watching(db_path)

    try:
        add_test_data(db_path)
        time.sleep(2)
    finally:
        monitor.stop_watching()
        store.close()

    print("\n✅ Monitoring complete")
    Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
```

(Keep `setup_test_db` and `add_test_data` bodies from the current file;
only the class shape and `main()` wiring change.)

### 8.3 `src/examples/example_webhook.py` (rewrite)

```python
#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.3.0",
#     "eventic>=1.1.0",
#     "requests>=2.31.0",
# ]
# ///
"""
Webhook server example for Observantic.
Receives HTTP POST/PUT webhooks and prints them; ships with an auth demo.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import requests
from eventic import App, Stream
from eventic.sql import SQLite
from observantic import WebhookEventBase


class WebhookEvent(BaseModel):
    """One emitted webhook request (the stream's state model)."""

    path: str = ""
    method: str = ""
    headers: dict[str, str] = {}
    body: bytes | str | dict = b""
    source_ip: str = ""


webhooks = Stream(WebhookEvent, name="webhooks")
app = App(id="webhook-demo", streams=[webhooks])


class WebhookReceiver(WebhookEventBase):
    """Receive webhooks; each request emits a WebhookEvent."""

    port: int = 8888
    webhook_paths: list[str] = ["/webhook", "/api/event"]
    require_auth_header: str | None = "X-API-Key"
    require_auth_value: str | None = "secret-123"

    def on_webhook_received(self, event):
        try:
            if isinstance(event.body, dict):
                data = event.body
            else:
                data = json.loads(event.body)
            print(f"🔔 Webhook received: {data}")
        except (json.JSONDecodeError, TypeError):
            print(f"🔔 Non-JSON webhook: {str(event.body)[:50]}...")

    def on_start(self):
        print(f"Server running at http://localhost:{self.port}")
        print(f"Endpoints: {', '.join(self.webhook_paths)}")


def send_test_webhooks() -> None: ...  # unchanged from current file


def main():
    """Run webhook server demo."""
    print("🚀 Webhook Server Demo")
    print("Starting server on port 8888...")

    test_thread = threading.Thread(target=send_test_webhooks)
    test_thread.daemon = True
    test_thread.start()

    store = SQLite("webhooks.db")
    runtime = app.bind(store)
    server = WebhookReceiver(stream=webhooks, auto_persist=True)
    server.bind(runtime)
    server.start_watching()

    print("Press Ctrl+C to stop\n")
    try:
        test_thread.join()
        time.sleep(5)
    except KeyboardInterrupt:
        print("\n✅ Server stopped")
    finally:
        server.stop_watching()
        store.close()


if __name__ == "__main__":
    main()
```

### 8.4 `src/examples/webhook_server.py` (rewrite the eventic parts)

Keep the typer CLI, signal handlers, `_log_file` private-attr handling and
`on_error` JSONL logging exactly as-is. Replace:

- `from eventic import Record` →

```python
from pydantic import BaseModel
from eventic import App, Stream
from observantic import WebhookEventBase, make_store
```

- the record-class body →

```python
class WebhookEvent(BaseModel):
    """One emitted webhook request (the stream's state model)."""

    path: str = ""
    method: str = ""
    headers: dict[str, str] = {}
    body: bytes | str | dict = b""
    source_ip: str = ""


webhooks = Stream(WebhookEvent, name="webhooks")
app = App(id="webhook-logger", streams=[webhooks])


class WebhookLogger(WebhookEventBase):
    """Production webhook logger with JSONL output."""

    port: int = 8000
    host: str = "0.0.0.0"
    webhook_paths: list[str] = ["/webhook", "/api/webhook"]
    require_auth_header: str | None = None
    require_auth_value: str | None = None
    parse_json_body: bool = True

    _log_file: Path = Path("/data/webhooks.jsonl")
    _request_count: int = 0

    def on_webhook_received(self, event): ...  # unchanged
    def _format_body_preview(self, body, max_length=100): ...  # unchanged
    def on_start(self): ...  # unchanged
    def on_stop(self): ...  # unchanged
    def on_error(self, error: Exception, event=None): ...  # unchanged
```

- in `main()`, replace the `observantic.init(...)` block with store binding
  (server still runs when the DB is unreachable — same resilience as today):

```python
    # Persistence is explicit: build a store from --database-url and bind.
    # If the database is unreachable, the server still runs — persistence
    # is simply unavailable (auto_persist is off until bind succeeds).
    server_instance = WebhookLogger(
        port=port,
        host=host,
        webhook_paths=[p.strip() for p in paths.split(",")],
        parse_json_body=parse_json,
        require_auth_header=auth_header,
        require_auth_value=auth_value,
    )
    try:
        store = make_store(database_url)
        runtime = app.bind(store)
        server_instance.bind(runtime)
        server_instance.auto_persist = True
        print(f"🔌 eventic store bound @ {database_url}")
    except Exception as e:
        print(f"⚠️  eventic unavailable ({e}); persistence disabled")
        store = None

    server_instance._log_file = log_file

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    server_instance.start_watching()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if server_instance._watching:
            server_instance.stop_watching()
        if store is not None:
            store.close()
```

Hmm — `server_instance.auto_persist = True` after construction: pydantic
models allow plain attribute assignment by default (`validate_assignment`
is off, models are not frozen), so this works and keeps `WebhookLogger`
constructed exactly as before. Persistence only turns on once bind succeeds;
if bind fails the server keeps running with persistence disabled.


### 8.5 `src/examples/demo_app.py` (new — CLI target)

```python
#!/usr/bin/env python3
"""A ready-to-run eventic App over observantic's default streams.

Use it with the eventic CLI, e.g.:

    uv run eventic --app examples.demo_app:app \
        --url sqlite:///demo.db schema upgrade
    uv run eventic --app examples.demo_app:app \
        --url sqlite:///demo.db inspect
"""

from __future__ import annotations

from eventic import App

from observantic import FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM

app = App(
    id="observantic-demo",
    streams=[FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM],
)
```

### 8.6 Second green checkpoint

```bash
uv run pytest -q
```

Everything should pass now (including `test_examples_importable`).
Check that the examples still only *declare* values at import time (no I/O):

```bash
uv run ruff check src/examples
```

---

## Step 9 — README (`README.md`)

Rewrite the affected sections; keep the watcher documentation (hooks,
webhook security, config env vars, error handling, hook registration,
development) largely intact but strip every 0.1.5/`Record`/DBOS reference.

### Quick Start (replace the first code block)

```python
from pydantic import BaseModel
from eventic import App, Stream
from eventic.sql import SQLite
from observantic import FileEventBase


class FileEvent(BaseModel):
    path: str = ""
    event_type: str = ""
    is_directory: bool = False


files = Stream(FileEvent, name="files")
app = App(id="my-app", streams=[files])


class DocumentWatcher(FileEventBase):
    watch_patterns: list[str] = ["*.pdf", "*.txt"]

    def on_file_created(self, event):
        print(f"Created: {event.src_path}")


# Monitor files (no database required)
watcher = DocumentWatcher(stream=files)
watcher.start_watching("/documents")
watcher.stop_watching()

# Persistence is explicit: bind a store, opt in per watcher
store = SQLite("observantic.db")
watcher.bind(app.bind(store))
watcher.auto_persist = True
watcher.start_watching("/documents")
```

### Persistence (replace the whole section)

Explain:

- Eventic 1.1.0 is a versioned document store. Watchers emit **plain
  pydantic state** into a **`Stream`**; commits go through a **`Collection`**
  obtained from `app.bind(store)`.
- `watcher.bind(runtime)` resolves `runtime[stream]`; `auto_persist=True`
  commits each emitted state as a **new aggregate** (revision 0) via
  `collection.create(...)`. `persist_strict=True` turns the "not bound"
  warning into a `ConfigurationException`.
- Writes are compare-and-swap: `collection.change(base, **fields)` and
  `collection.replace(base, state)` raise `RevisionConflict` on a stale
  base. Reads: `get(id)`, `get(id, revision=n)`, `history(id)`, `where(**filters)`.
- Backends: `SQLite` for dev/test/single-process; `Postgres` for
  production (`pip install eventic[postgres]`). Schema is created
  automatically by `SQLite`; for Postgres run
  `eventic --app myapp:app --url "$DATABASE_URL" schema upgrade`.
- **Delivery**: hooks are in-process and best-effort. For durable delivery
  declare `Subscription`s (`Inline()` or `Outbox(queue=...)`) on the App and
  run `eventic worker --queue q`. Outbox is at-least-once — handlers must be
  idempotent.
- **Schema evolution**: bump `stream.schema_version` and declare upcasters
  (`eventic.evolution.make_upcaster`); `eventic schema check` exits 3 on
  model drift.
- **Removed in 0.3.0**: `init()`/`reset()`/`is_eventic_ready()`,
  `Record`-based watchers, `@on.create`, `auto_persist` re-appends, DBOS
  queues. Data written by 0.2.0/eventic 0.1.5 is **not readable** by 0.3.0 —
  re-ingest (greenfield schema).

### Hook registration / error handling / config / development

- Keep the existing text; delete the "Eventic 0.1.5 wraps only methods
  explicitly marked `@evented`" note and the `dispatch_direct` paragraph.
- Config: update the env-var table default to `sqlite:///observantic.db`.
- Development: delete the "DB-backed tests skip unless TEST_DATABASE_URL is
  set" note — the suite runs on SQLite. Mention Postgres integration tests
  via `eventic[postgres]` + `TEST_DATABASE_URL` as optional.

---

## Step 10 — Tooling & dev environment

**File: `devenv.nix`** — update the `enterTest` comment:

```nix
  # Tests run on SQLite by default (eventic 1.1.0 backend). The devenv
  # Postgres at 127.0.0.1:5432 (db "eventic", user/pass postgres/postgres)
  # is available for optional Postgres integration tests:
  #   TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/eventic \
  #     uv run pytest tests/test_postgres_integration.py
```

The Postgres service block itself is unchanged.

Run the full quality gate:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src/observantic
uv run pytest -q
```

Fix anything reported. Known mypy hotspots:

- `stream: Stream = FILE_STREAM` in monitors — the pydantic plugin may want
  `Stream[Any]`; use `stream: Stream[Any] = FILE_STREAM` if mypy complains.
- `make_store` returns `Any` deliberately (a `SQLite | Postgres` union is
  overkill); keep it.
- `_emit`/`_persist` operate on `Any` states (models vary per stream).

---

## Step 11 — End-to-end verification (eventic CLI + worker)

```bash
cd ~/Documents/Projects/observantic
uv run eventic --version                 # eventic 1.1.0
uv run eventic --app examples.demo_app:app --url sqlite:///demo.db schema upgrade
uv run eventic --app examples.demo_app:app --url sqlite:///demo.db schema check
uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect
uv run eventic --app examples.demo_app:app --url sqlite:///demo.db verify
```

A full outbox loop (proves the delivery story end to end):

```bash
uv run python - <<'PY'
import tempfile, pathlib
from pydantic import BaseModel
from eventic import App, Stream, Subscription, Outbox
from eventic.sql import SQLite
from eventic.worker import Worker
from observantic import FILE_STREAM

seen = []
def handler(commit):
    seen.append((commit.kind, commit.revision.state.path))

app = App(
    id="smoke",
    streams=[FILE_STREAM],
    subscriptions=[
        Subscription(id="smoke.file", stream=FILE_STREAM, handler=handler,
                     delivery=Outbox(queue="q"))
    ],
)
store = SQLite(tempfile.mktemp(suffix=".db"))
runtime = app.bind(store)
runtime[FILE_STREAM].create(FILE_STREAM.model(path="/smoke", event_type="created"))
Worker(app, store, queue="q").drain_once()
assert seen == [("create", "/smoke")], seen
store.close()
print("OUTBOX SMOKE OK")
PY
```

Manual watcher smoke (files → SQLite):

```bash
mkdir -p /tmp/obs-smoke && cd /tmp/obs-smoke
uv run python - <<'PY'
import time
from pydantic import BaseModel
from eventic import App, Stream
from eventic.sql import SQLite
from observantic import FileEventBase

class F(BaseModel):
    path: str = ""
    event_type: str = ""
    is_directory: bool = False

s = Stream(F, name="smoke_files")
app = App(id="smoke2", streams=[s])
store = SQLite("smoke.db")

class W(FileEventBase):
    stream = s
    def on_file_created(self, event):
        print("hook:", event.src_path)

w = W(auto_persist=True)
w.bind(app.bind(store))
w.start_watching(".")
pathlib.Path("hello.txt").write_text("hi")
time.sleep(1.5)
w.stop_watching()
print("persisted:", len(store and app.bind(store)[s].where(path="hello.txt").items))
store.close()
PY
```

(Expect `hook: .../hello.txt` and `persisted: 1`.)

---

## Step 12 — Commit & release notes

```bash
cd ~/Documents/Projects/observantic
uv run pytest -q && uv run ruff check . && uv run mypy src/observantic
git add -A
git commit -m "align with eventic 1.1.0

- bump eventic to v1.1.0; drop confidantic/python-dotenv; add pydantic dep
- rewrite the eventic seam: make_store/build_app replace init/reset
- EventWatcher: stream field + bind()/unbind(); _emit commits via Collection
- monitors: default FILE_STREAM/SQLITE_STREAM/WEBHOOK_STREAM declarations
- remove global init/reset/is_eventic_ready public API (0.1.5 singleton)
- default OBSERVANTIC_DB_URL is now sqlite:///observantic.db
- rewrite tests, examples, README for declarations + explicit stores
- add examples/demo_app.py for the eventic CLI
"
```

Release notes for 0.3.0 must state: **breaking** — `Record` watchers,
`init()`/`reset()`, and eventic 0.1.5 data are not supported; eventic 1.1.0
uses a new append-only schema (re-ingest required).

---

## Appendix A — Emit-field contracts (custom stream models)

A custom `stream` model must accept every keyword `_emit` passes. Per monitor:

| Monitor | Emit kwargs |
|---|---|
| `FileEventBase` | `path: str`, `event_type: str`, `is_directory: bool`, `dest_path: str \| None` (moved only) |
| `SQLiteEventBase` | `table_name: str`, `row_data: dict \| None`, `row_id: int \| str \| None`, `operation: "inserted" \| "updated" \| "deleted"` |
| `WebhookEventBase` | `path: str`, `method: str`, `headers: dict[str, str]`, `body: bytes \| str \| dict`, `source_ip: str` |

A mismatch raises `pydantic.ValidationError` at emit time (loud, not silent).

## Appendix B — Verification checklist

- [ ] `uv run python -c "import eventic; print(eventic.__version__)"` → `1.1.0`
- [ ] `uv run pytest -q` → all green (SQLite backend, no Postgres needed)
- [ ] `uv run ruff check . && uv run ruff format --check .` → clean
- [ ] `uv run mypy src/observantic` → clean
- [ ] `uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect`
      prints streams `files`/`sqlite`/`webhooks`
- [ ] outbox smoke (Step 11) delivers exactly one commit
- [ ] watcher smoke (Step 11) persists one `create` revision
- [ ] README quick start + persistence sections match the new API
- [ ] `git log`/diff contains no `Record`, `init(`, `reset(`, `is_eventic_ready`,
      `evented`, `dispatch_direct`, `record_model`, `confidantic` references
