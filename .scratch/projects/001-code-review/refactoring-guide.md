# Observantic — Step-by-Step Refactoring Guide

Companion to `.scratch/projects/001-code-review/review.md` (finding IDs C-01…C-09, H-10…H-20).
**Eventic is assumed to be fixed in a separate session**; this guide therefore (a) states the
exact contract Observantic needs from the fixed Eventic, and (b) isolates **all** Eventic
interaction behind one seam module (`observantic/_eventic.py`) so the two libraries can land
in either order.

---

## 0. Guiding principles

1. **Every step leaves the tree green.** Each phase ends with a runnable verification command.
2. **Foundation before monitors, monitors before consumers.** Core dispatcher → config →
   persistence seam → then the three monitors → then examples/tests/docs.
3. **The observer thread must never die** (fixes C-04). All errors funnel to `on_error` and are
   swallowed (or, when configured, stop the watcher cleanly — never propagate into watchdog).
4. **Fail fast at `start_watching()`, not on the first event** (fixes C-03 surface). Anything
   the watcher needs (valid path, initialized persistence backend) is checked up front.
5. **One integration seam for Eventic.** `observantic/_eventic.py` is the only module that
   imports `eventic` internals. If the fixed Eventic differs from the contract below, only
   that file changes.
6. **Typed exceptions everywhere.** Replace bare `ValueError`/`RuntimeError` with the
   existing (currently dead) hierarchy in `exceptions.py`.

### 0.1 Contract expected from the fixed Eventic

The guide assumes the fixed Eventic provides (adjust `_eventic.py` if not):

| Concern | Current behavior (broken) | Required post-fix contract |
|---|---|---|
| `Eventic.init(name=…, database_url=…)` | keyword-only args; singleton | Same signature; must be safe to call repeatedly (idempotent singleton) |
| `RecordMeta` public-method wrapping | Wraps **every** public method of `Record` subclasses with `evented()` (sync run + queue enqueue) | Either stop wrapping methods that aren't decorated, or expose the raw function via `__wrapped__` (observantic uses `inspect.unwrap`, which works with `functools.wraps` today) |
| `Record.__setattr__` copy-on-write | Writes a new version to the store; raises `RuntimeError` if store unset | Keep; must raise a **typed, catchable** error (e.g. `EventicNotInitializedError`) when no store is configured |
| Store persistence | `Record._store.append(record)` | Keep as the append API, or document its replacement in `_eventic.py` |
| `Queue(name, concurrency=1)` duplicate creation | One per class + one per decorator → “already declared” warnings | Deduplicate queues by name |
| `launch()` / enqueue behavior | Enqueue before launch raises `DBOSException: No DBOS was created yet` | Provide a checkable `Eventic.is_launched()` (or equivalent) so observantic can validate up front |

### 0.2 Target layout after the refactor

```
src/observantic/
├── __init__.py            # public API, __version__ (single source of truth)
├── _eventic.py            # ★ THE SEAM: all eventic imports
├── config.py              # settings; consumed for real
├── exceptions.py          # wired in everywhere
├── core/
│   ├── __init__.py
│   └── base.py            # EventWatcher state machine + dispatch + hooks
└── monitors/
    ├── __init__.py
    ├── file.py
    ├── sqlite.py
    └── webhook.py
tests/                     # moved out of src/, standard pytest layout
src/examples/…             # fixed examples (delete empty app.py)
```

---

## Phase A — Foundations

### Step 1. Make the test harness real

**Files:** `pyproject.toml`, `tests/` (new), `tests/conftest.py` (new), delete `src/tests/`.

1. Add a dev-dependency group and pytest config to `pyproject.toml`:

```toml
[dependency-groups]
dev = ["pytest>=8", "requests>=2.31"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

2. Move `src/tests/tests.py` → `tests/` and split it later (Step 10). For now, create
   `tests/conftest.py` with fixtures that don't require Postgres:

```python
import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    import sqlite3

    p = tmp_path / "test.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
```

3. **Do not** keep the `setup_eventic` session fixture that requires a live Postgres.
   Eventic-dependent tests are marked and skipped when no DB is configured (Step 10).

**Verify:** `uv sync --group dev && uv run pytest` collects and shows the *known* failures
(baseline for the refactor). Record the baseline: `7 failed`.

---

### Step 2. Fix `config.py` (H-13, H-14)

**File:** `src/observantic/config.py`

Goals: honor the documented env vars, stop the double-singleton confusion, and make the
settings actually reach the rest of the library.

1. Remove unused imports; add `AliasChoices`-based env aliases so **both** `DB_URL` and
   `OBSERVANTIC_DB_URL` work:

```python
from typing import Final
from pydantic import AliasChoices, Field
from confidantic import SettingsType, PluginRegistry


class ObservanticMixin(SettingsType):
    DB_URL: str = Field(
        default="postgresql://localhost/observantic",
        description="Database URL for Eventic",
        validation_alias=AliasChoices("DB_URL", "OBSERVANTIC_DB_URL"),
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Python logging level",
        validation_alias=AliasChoices("LOG_LEVEL", "OBSERVANTIC_LOG_LEVEL"),
    )


PluginRegistry.register(ObservanticMixin)
ObservanticSettings = PluginRegistry.build_class()
settings: Final[ObservanticSettings] = ObservanticSettings()
```

> Note: this depends on confidantic passing the env dict through pydantic validation
> (it does — `model_cls.model_validate(env_data | kwargs)`), so `validation_alias` applies.
> **Verify in Step 2** (below); if the alias doesn't take effect with the installed
> confidantic, fall back to the explicit plan: stop inheriting these two fields from
> confidantic and read them with `os.getenv("OBSERVANTIC_DB_URL", os.getenv("DB_URL", …))`
> in a small `env.py` helper. Decide once, document it.

2. Add explicit values read from the environment *once* (import time is fine; document that
   settings are snapshot at import):

```python
DB_URL = settings.DB_URL
LOG_LEVEL = settings.LOG_LEVEL
```

3. Fix the double-singleton story: do **not** re-export confidantic's `settings`; only
   `observantic.settings` exists publicly. Add a note in the module docstring that
   `confidantic.settings` is a different object and must not be used.

**Verify:** run the H-13 repro with `OBSERVANTIC_DB_URL` set → `settings.DB_URL` reflects it.
If not, apply the `os.getenv` fallback.

---

### Step 3. Rebuild `EventWatcher` in `core/base.py` (C-03, C-04, H-10, H-12, H-19)

**File:** `src/observantic/core/base.py`

This is the heart of the refactor. Four sub-steps:

#### 3.1 State machine — validate before flipping state, template method pattern

```python
def start_watching(self, path: str, **kwargs: Any) -> None:
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
            self._watching = False  # rollback (fixes H-10)
        raise
    self._safe_call("on_start")


def stop_watching(self) -> None:
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
```

- `_validate_start(path)`, `_start_impl(**kwargs)`, `_stop_impl()` are the subclass extension
  points; base versions are no-ops (file/sqlite/webhook implement them).
- **Lifecycle hooks can never corrupt state**: `on_start`/`on_stop`/`on_error` are invoked
  via `_safe_call` (below), never directly.

#### 3.2 Error contract — observers survive, `on_error` gets the real event

```python
def _dispatch_hook(self, event_name: str, *args: Any, **kwargs: Any) -> None:
    """Invoke override method + registered callbacks. NEVER raises into the caller.

    Errors are reported via on_error(event_obj, error) and swallowed (or, if
    raise_on_hook_error=True, collected and raised after all hooks have run —
    but never from within the observer thread's stack).
    """
    event = args[0] if args else None
    for hook in self._hook_callables(event_name):  # method + registered callbacks
        try:
            hook(*args, **kwargs)
        except Exception as e:
            self._safe_call("on_error", e, event)  # ← event, not event_name (H-12)
            if self.raise_on_hook_error:
                self._last_hook_error = e


def _hook_callables(self, event_name: str) -> list[HookFn]:
    fn = call_unwrapped(type(self), event_name)  # from _eventic.py seam
    callbacks = list(self._hooks.get(event_name, ()))
    return ([fn] if fn is not None else []) + callbacks


def _safe_call(self, name: str, *args: Any) -> None:
    """Call a lifecycle method; a failure in it is logged, never raised."""
    try:
        fn = call_unwrapped(type(self), name)
        if fn is not None:
            fn(self, *args)
    except Exception as e:
        logger.error("lifecycle hook %s failed: %s", name, e, exc_info=True)
```

- New config fields on `EventWatcher`:

```python
raise_on_hook_error: bool = Field(
    default=False, description="Collect hook errors instead of swallowing"
)
```

- `_dispatch_hook` returns normally; the watchdog observer thread can no longer die from user
  hook code (fixes C-04). For the webhook path, wrap the 500 decision around
  `self._last_hook_error` (Step 8).
- Remove the base no-op hook stubs (`on_file_created` etc. defined as `pass` in the monitors)
  — see Step 6/7/8. `call_unwrapped` returns `None` when the class has no override, so
  dispatch falls through to registered callbacks cleanly. (Keeps `_dispatch_hook` cheap and
  avoids dispatching a no-op every event.)

#### 3.3 Dispatch bypasses Eventic's metaclass wrappers (C-03)

The seam function (lives in `_eventic.py`, Step 4) resolves the **raw** function, stripping
`evented`/`@Eventic.step()` wrappers via `inspect.unwrap`:

```python
# in observantic/_eventic.py
import inspect
from typing import Any, Callable, Optional


def call_unwrapped(cls: type, name: str) -> Optional[Callable[..., Any]]:
    """Return the raw function `name` from `cls`'s MRO, wrapper-stripped."""
    if not hasattr(cls, name):
        return None
    fn = getattr(cls, name)
    return inspect.unwrap(fn) if callable(fn) else None
```

Because `evented()` uses `functools.wraps`, `inner.__wrapped__` points at the original
function; `inspect.unwrap` walks the chain (`@Eventic.step()` → raw). This is the single
most important change: **the dispatcher no longer touches DBOS queues or `Record.__setattr__`
wrappers**, so Record-based watchers work without `launch()` and never double-execute.

> If the fixed Eventic stops wrapping undecorated methods entirely, `inspect.unwrap` is a
> no-op passthrough — this seam keeps observantic correct either way.
>
> Users who *do* want DBOS queue semantics should opt in explicitly: register an
> explicitly-decorated callback via `register_hook` (see Step 4, `dispatch_direct`).

#### 3.4 Cleanups

- Drop the unused `Field` import; keep `_hooks`, `_watching`, `_lock` as `PrivateAttr`.
- `register_hook`/`unregister_hook` already lock; also lock in `_hook_callables` when copying
  the callback list (cheap and correct).
- `run_async` stays as a documented placeholder.

**Verify (pure unit test, no DBOS, no Postgres):**

```python
def test_hook_error_does_not_raise():
    class W(EventWatcher):
        def on_file_created(self, event):
            raise ValueError("boom")

    w = W()
    w._dispatch_hook("on_file_created", object())  # returns normally


# on_error was invoked with the event object
```

```python
def test_start_rolls_back_on_validation_failure():
    class W(EventWatcher):
        def _validate_start(self, path):
            raise ConfigurationException("nope")

    w = W()
    with pytest.raises(ConfigurationException):
        w.start_watching("/x")
    assert w._watching is False  # fixes H-10
```

---

### Step 4. Persistence seam `_eventic.py` + real `_emit` (C-08, C-03)

**Files:** `src/observantic/_eventic.py` (new), `src/observantic/core/base.py`.

1. Move **all** eventic imports into the seam. Public API of the seam:

```python
# observantic/_eventic.py  (complete)
"""Single integration point for the fixed Eventic library."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("observantic.eventic")


class EventicNotReadyError(RuntimeError):
    """Raised when persistence is requested before Eventic is initialized."""


_initialized = False


def init_eventic(*args: Any, **kwargs: Any) -> Any:
    """Thin wrapper over Eventic.init — keeps observantic decoupled."""
    from eventic import Eventic

    result = Eventic.init(*args, **kwargs)
    global _initialized
    _initialized = True
    return result


def is_ready() -> bool:
    return _initialized


def call_unwrapped(cls: type, name: str) -> Optional[Callable[..., Any]]:
    ...
    # (as in 3.3)


def persist(record: Any) -> None:
    """Append `record` to its Eventic store. Raises EventicNotReadyError if unavailable."""
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
    try:
        from eventic.core.record import Record as _Record
    except ImportError:
        return False
    return _initialized
```

2. Rework `RecordMixin._emit` into a real, documented emission path:

```python
class EventWatcher(BaseModel, ABC):
    record_model: type[Any] = Field(
        default=None,
        description="Model emitted per event; defaults to the monitor's internal record model",
    )  # noqa: E501
    auto_persist: bool = Field(
        default=False, description="Append emitted records to Eventic's store"
    )
    dispatch_direct: bool = Field(
        default=True, description="Bypass Eventic metaclass wrappers (recommended)"
    )

    def _emit(self, **fields: Any) -> Any:
        model = self.record_model or self._default_record_model()
        record = model(**fields)
        if self.auto_persist:
            try:
                persist(record)
            except EventicNotReadyError:
                if self.persist_strict:
                    raise ConfigurationException(str(e)) from e
                logger.warning(
                    "auto_persist=True but Eventic not ready; record not persisted"
                )
        return record
```

   - Each monitor sets `_default_record_model()` to its internal model (`FileRecord`,
     `DatabaseRow`, `WebhookRecord`) **and** exposes the `record_model` field so a user can
     point it at their own `Record` subclass:

```python
class FileEvent(Record, FileEventBase):
    record_model = FileEvent  # ← now _emit() creates *your* record
```

   - `persist_strict: bool = False` decides whether a missing Eventic backend is a warning
     or a hard error. `auto_persist` defaults to `False` so the library is usable with zero
     Eventic setup; the README (Step 11) documents turning it on.
   - Because dispatch is direct (3.3) and `auto_persist` is explicit, the library no longer
     depends on `Record.__setattr__` side effects for persistence — no more accidental
     double DB writes (fixes C-03/C-08).

3. Replace `EventicShim` with a seam-backed façade in `__init__.py`:

```python
# observantic/__init__.py
def init(*args, **kwargs):
    return init_eventic(*args, **kwargs)


def is_eventic_ready() -> bool:
    return is_ready()
```

   `EventicShim` is removed from the public API (or kept as a deprecated alias).

**Verify:** with `auto_persist=False` (default), a `Record`-based watcher dispatches events
with **no** `DBOSException` and **no** double execution (run the C-03 repro → now prints
“hook ran” once, no raise). With `auto_persist=True` and no `init()` → warning logged, event
still delivered to hooks.

---

### Step 5. Wire the exception hierarchy (H-20)

**File:** `src/observantic/exceptions.py`, then sweep the tree.

Replace every bare raise:

| Before | After |
|---|---|
| `RuntimeError("Already watching")` | `WatcherException("Already watching")` |
| `ValueError(f"Path does not exist: {path}")` | `ConfigurationException(f"Path does not exist: {path}")` |
| `RuntimeError(f"Failed to start observer: …")` | `WatcherException(…)` |
| `RuntimeError(f"Failed to check for changes: …")` | `WatcherException(…)` (but see Step 7 — this should no longer escape at all) |
| `NotImplementedError` (async stub) | keep |

Also delete the unused imports (`Field` in base.py, `Optional`/`Path`/`field_validator` in
config.py, `Callable` in webhook.py) and run a lint pass.

**Verify:** `grep -rn "raise " src/observantic | grep -v exceptions.py` shows only
`WatcherException`/`ConfigurationException`/`ObservanticException`/`EventicNotReadyError`.

---

## Phase B — Monitors

### Step 6. File monitor (`monitors/file.py`)

**Finding coverage:** H-10 (shared with Step 3), H-19 (throttle pruning, join timeout,
moved-event fields), C-02 (annotations).

1. **State machine** — implement `_validate_start` / `_start_impl` / `_stop_impl`:

```python
def _validate_start(self, path: str) -> None:
    if not Path(path).exists():
        raise ConfigurationException(f"Path does not exist: {path}")
    if not Path(path).is_dir():
        raise ConfigurationException(f"Not a directory: {path}")


def _start_impl(self, path: str, recursive: bool = True) -> None:
    self._watch_path = str(Path(path).resolve())
    self._observer = Observer()
    try:
        self._observer.schedule(
            self._create_handler(), self._watch_path, recursive=recursive
        )
        self._observer.start()
    except Exception as e:
        raise WatcherException(f"Failed to start observer: {e}") from e


def _stop_impl(self) -> None:
    if self._observer:
        self._observer.stop()
        self._observer.join(timeout=5)  # ← bounded join (H-19)
        self._observer = None
```

2. **Patterns / config** — keep as pydantic fields; README/examples must use annotations
   (C-02). Add a `case_sensitive` config field (currently hardcoded `True`).

3. **Throttle** — prune the map so it can't grow unbounded:

```python
def _should_throttle(self, path: str) -> bool:
    if self.event_throttle_seconds <= 0:
        return False
    now = time.time()
    cutoff = now - max(60.0, self.event_throttle_seconds * 100)
    for stale in [p for p, t in self._last_event_times.items() if t < cutoff]:
        del self._last_event_times[stale]
    ...
```

4. **Moved events** — extend the record and the hook payload:

```python
class FileRecord(BaseModel):
    path: str
    event_type: str
    is_directory: bool = False
    dest_path: Optional[str] = None  # set for "moved"
    timestamp: float = Field(default_factory=time.time)
    model_config = {"frozen": True, "extra": "forbid"}
```

   Emit `dest_path=str(Path(event.dest_path).resolve())` on moved, throttle moved events too,
   and drop the base no-op `on_file_*` stubs (dispatch now resolves overrides via
   `call_unwrapped`; registered hooks still work).

**Verify:** plain-watcher integration test — create `.pdf`, edit it, delete it, move it →
hooks fire with the right `event_type`; a raising hook does **not** kill the observer
(assert `w._observer.is_alive()` after the error).

---

### Step 7. Rewrite the SQLite monitor (`monitors/sqlite.py`)

**Finding coverage:** C-01 (data_version gate), H-15 (rowid-diff lossiness, stale checkpoints,
injection, conn leaks), H-16 (`track_schema_changes`), H-19 (conn close on error),
H-20 (`poll_interval_seconds` dead).

**Design decisions:**

1. **Delete the `data_version` early-return gate.** It is demonstrably stale (C-01). The
   file-modification event (watchdog) is the trigger; we diff snapshots.
2. **Snapshot-based diffing instead of rowid-diffing.** Per table, keep
   `{rowid: tuple(cell, …)}`. On change, compare → classify rows as
   `inserted` / `updated` / `deleted`. This fixes updates/deletes/rowid-reuse.
3. **Parameterize all identifiers** — never f-string table names into SQL.
4. **`track_schema_changes` becomes real**: snapshot `sqlite_master`; on change, emit a
   `SchemaChange` record and dispatch `on_schema_changed`.
5. **`poll_interval_seconds` becomes real or disappears.** Implement an optional background
   poll thread (helps for DBs on filesystems where inotify misses writes, WAL, network
   mounts). Keep it default-on so monitoring still works even if watchdog fires early/delayed.
6. **Reset state on every `start_watching`** (checkpoints/snapshots must not leak across
   restarts).
7. **Resolve the DB path to absolute at start** and compare against resolved `src_path`
   (fixes the relative/absolute mismatch that can prevent events entirely).
8. **Errors stay inside the handler** — `on_error`, no re-raise (C-04 contract).

Sketch of the new module core:

```python
def _validate_start(self, db_path: str) -> None:
    self._db_path = str(Path(db_path).resolve())
    if not Path(self._db_path).exists():
        raise ConfigurationException(f"Database does not exist: {db_path}")


def _start_impl(self, **kwargs: Any) -> None:
    self._snapshots: dict[str, dict[Any, tuple[Any, ...]]] = {}
    self._schema: dict[str, str] = {}  # name -> sql
    self._refresh_snapshot()  # also seeds _schema
    self._observer = Observer()
    try:
        self._observer.schedule(
            self._create_handler(), str(Path(self._db_path).parent), recursive=False
        )
        self._observer.start()
    except Exception as e:
        raise WatcherException(f"Failed to start database observer: {e}") from e
    if self.poll_interval_seconds > 0:
        self._poll_thread = Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()


def _poll_loop(self) -> None:
    while self._watching:
        time.sleep(self.poll_interval_seconds)
        try:
            self._check_for_changes()
        except Exception as e:  # never escape the poll thread
            self._safe_call("on_error", e, self._db_path)


def _check_for_changes(self) -> None:
    conn = sqlite3.connect(self._db_path, timeout=self.db_connect_timeout_seconds)
    try:
        with conn:
            self._check_schema(conn)  # DDL events (H-16)
            for table in self._list_rowid_tables(conn):
                self._diff_table(conn, table)  # insert/update/delete events
    except sqlite3.Error as e:
        self._safe_call("on_error", e, self._db_path)
    finally:
        conn.close()  # ← always closed (H-19)


def _diff_table(self, conn, table: str) -> None:
    qname = self._quote(table)  # '"t"'-style quoting
    cur = conn.execute(f"SELECT rowid, * FROM {qname}")
    cols = [d[0] for d in cur.description]
    now = {row[0]: tuple(row[1:]) for row in cur.fetchall()}
    old = self._snapshots.setdefault(table, {})
    for rid, cells in now.items():
        if rid not in old:
            self._emit_row(table, rid, cols, cells, "inserted")
        elif old[rid] != cells:
            self._emit_row(table, rid, cols, cells, "updated")
    for rid in old.keys() - now.keys():
        self._emit_row(table, rid, cols, None, "deleted")
    self._snapshots[table] = now


@staticmethod
def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
```

New records:

```python
class DatabaseRow(BaseModel):
    table_name: str
    row_data: dict[str, Any] | None = None
    row_id: int | str | None = None
    operation: Literal["inserted", "updated", "deleted"] = "inserted"
    timestamp: float = Field(default_factory=time.time)
    model_config = {"frozen": True, "extra": "forbid"}


class SchemaChange(BaseModel):
    tables_added: list[str]
    tables_dropped: list[str]
    tables_modified: list[str]
    timestamp: float = Field(default_factory=time.time)
    model_config = {"frozen": True, "extra": "forbid"}
```

New/updated hooks (with base stubs removed):

```python
def on_row_inserted(self, row: DatabaseRow) -> None: ...
def on_row_updated(self, row: DatabaseRow) -> None: ...
def on_row_deleted(self, row: DatabaseRow) -> None: ...
def on_schema_changed(self, change: SchemaChange) -> None: ...
```

`on_data_changed(db_path, new_rows)` is kept for backward compatibility and reimplemented as:
“collect all inserted rows since the last check and dispatch once per check.”

New config fields (all annotated — C-02): `poll_interval_seconds: float = 1.0`,
`track_schema_changes: bool = True`, `db_connect_timeout_seconds: float = 5.0`,
`max_table_rows: int = 100_000` (guard against snapshotting enormous tables — skip with a
warning instead of OOM).

**Verify (the old C-01 repro now passes):**

```python
w = SW(); w.start_watching(db)
insert 3 rows → on_row_inserted × 3, on_data_changed × 1 with 3 rows
UPDATE a row → on_row_updated fires (previously impossible)
DELETE a row → on_row_deleted fires
CREATE TABLE / DROP TABLE → on_schema_changed fires (when track_schema_changes=True)
stop_watching(); start_watching() again → snapshots are empty/fresh (no stale checkpoints)
```

---

### Step 8. Harden the webhook monitor (`monitors/webhook.py`)

**Finding coverage:** C-05 (DoS + hanging stop), C-06 (Content-Length), H-11 (traceback spam,
error leakage), H-17 (size cap, auth compare, query decoding, GET/PUT), H-12 (on_error
signature).

1. **`ThreadingHTTPServer` + daemon threads + request timeout:**

```python
class _WebhookServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WebhookHandler(BaseHTTPRequestHandler):
    timeout = 30  # socket read timeout → idle clients can't wedge a thread forever
    protocol_version = "HTTP/1.1"
```

2. **Body limits and strict Content-Length (C-06):**

```python
max_body_bytes: int = Field(
    default=1_048_576, description="Max request body (413 above this)"
)


def _read_body(self) -> tuple[int | None, bytes]:
    raw = self.headers.get("Content-Length")
    if raw is None:
        return None, b""  # explicit: no body, no silent read
    try:
        length = int(raw)
    except ValueError:
        return 400, b""  # invalid header → 400 (was: crash)
    if length < 0:
        return 400, b""
    if length > parent.max_body_bytes:
        return 413, b""  # too large → 413, no read
    return length, self.rfile.read(length)
```

3. **Bounded, non-hanging shutdown (C-05).** Track live sockets and close them on stop:

```python
class _ConnectionTrackingMixIn:
    _connections: set[socket.socket] = set()
    _conn_lock = Lock()

    def process_request(self, request, client_address):
        with self._conn_lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        finally:
            with self._conn_lock:
                self._connections.discard(request)

    def close_all_connections(self):
        with self._conn_lock:
            for sock in list(self._connections):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            self._connections.clear()


def _stop_impl(self) -> None:
    if self._server:
        self._server.close_all_connections()  # unblock any stuck handler
        self._server.shutdown()  # now returns promptly
        self._server.server_close()
        self._server = None
    if self._server_thread:
        self._server_thread.join(timeout=5)
        self._server_thread = None
```

   With the `timeout=30` socket timeout, even a client that never closes is reaped in ≤30 s,
   so `shutdown()` cannot hang.

4. **Response hygiene (H-11):** send a generic `500 {"error": "internal"}` on hook failure
   (log the real exception via `logging`); never `send_error(500, str(e))`. Wrap the whole
   `_handle_request` in one try/except so client disconnects produce a logged debug line,
   not a stack trace.

5. **Auth (H-17):** constant-time compare; enforce both header and value are configured
   together; validate in `_validate_start`:

```python
if (self.require_auth_header is None) != (self.require_auth_value is None):
    raise ConfigurationException(
        "require_auth_header and require_auth_value must be set together"
    )


def _authorized(self) -> bool:
    if not self.require_auth_header:
        return True
    got = self.headers.get(self.require_auth_header, "")
    return hmac.compare_digest(got, self.require_auth_value or "")
```

6. **Query params (H-17):** `dict(urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True))`
   instead of the manual split (adds URL-decoding).

7. **Methods (H-17):** `allowed_methods: list[str] = ["POST", "PUT"]` config; GET returns 405
   unless added. Keep `do_GET`/`do_PUT`/`do_POST` dispatching through one `_handle_request`.

8. **`log_message`** → route to `logging.getLogger("observantic.webhook")` (info level),
   not fully suppressed.

9. **Record field**: `WebhookRecord.body` stays `bytes | str | dict`; add `extra="forbid"`
   and `max_body_bytes` validation upstream so `dict` bodies can't be absurd.

**Verify:**

- Invalid `Content-Length` → **400** (no traceback). **REPRODUCE the old failure first**, then
  confirm it's gone.
- No `Content-Length` → 200 with empty body event (documented behavior, no silent read).
- Huge `Content-Length` (> cap) → **413**, server stays responsive.
- Idle-client wedge test (old C-05 repro): second request still served; `stop_watching()`
  returns in <1 s.
- Auth mismatch → 401; correct → 200.
- `on_error` receives the `WebhookEvent`, not the string "on_webhook_received" (H-12).
- A raising hook → client gets generic 500; **server keeps serving** subsequent requests.

---

## Phase C — Consumers

### Step 9. Fix examples (C-02, C-07)

**Files:** `src/examples/example_file.py`, `example_webhook.py`, `sqlite_example.py`,
`webhook_server.py`; delete `src/examples/app.py` (0 bytes).

1. **Every class-level configuration becomes an annotated field or constructor arg**
   (C-02):

```python
# example_file.py
class DocumentEvent(Record, FileEventBase):
    path: str
    event_type: str
    size: int = 0
    watch_patterns: list[str] = [
        "*.pdf",
        "*.txt",
        "*.docx",
        "*.md",
        "*.py",
    ]  # annotated!
```

2. **`example_webhook.py`** — provide defaults for the required Record fields and annotate
   overrides:

```python
class WebhookEvent(Record, WebhookEventBase):
    endpoint: str = ""
    payload: dict | str = {}
    timestamp: float = 0.0
    port: int = 8888  # annotated
    webhook_paths: list[str] = ["/webhook", "/api/event"]
    require_auth_header: str | None = "X-API-Key"
    require_auth_value: str | None = "secret-123"
    record_model = WebhookEvent  # _emit now creates THIS record
    auto_persist = False  # or True after init()
```

3. **`webhook_server.py`** — constructor-based configuration (fixes C-07), honor every
   typer option, drop the hardcoded URL:

```python
def main(...):
    ...
    server = WebhookLogger(
        port=port, host=host,
        webhook_paths=[p.strip() for p in paths.split(",")],
        parse_json_body=parse_json,
        require_auth_header=auth_header, require_auth_value=auth_value,
        _log_file=log_file,
    )
```

   - Remove the `WebhookLogger.port = …` style assignments entirely.
   - `database_url` option must be passed to `init()` (delete the hardcoded `real_url`).
   - `webhook_paths` split on `,` properly (the commented-out line becomes real code).
   - `_log_file` stays a `PrivateAttr`/constructor kwarg; `_request_count` likewise.
   - Keep the signal handler but make it call `server.stop_watching()` only if `_watching`
     (it already does).

4. **`sqlite_example.py`** — after Step 7 it will actually receive events; update the hook
   to the new per-row API (`on_row_inserted`) or keep `on_data_changed`.

**Verify:** `python -c "import examples.webhook_server"` etc. — every example module
imports and its class definitions succeed; `webhook_server --port 9123` actually binds 9123
(check with `ss -tlnp`).

---

### Step 10. Rewrite the test suite (C-09)

**Files:** `tests/` (move out of `src/`, split per component).

Test matrix (pure unit tests need no DBOS/Postgres):

| Area | What it proves |
|---|---|
| `test_core_dispatch.py` | hooks fire; overrides fire; registered callbacks fire; exceptions swallowed → `on_error` with the **event object**; `raise_on_hook_error=True` collects; observer survives |
| `test_core_state.py` | start/stop idempotency; `_watching` rolls back on validation failure; “Already watching”; restart resets state |
| `test_core_eventic.py` | `init` idempotent; `is_eventic_ready()`; `call_unwrapped` strips wrappers; `auto_persist=False` default does not touch Eventic; `auto_persist=True` + no init → warning (no crash); with init (marked, skipped if no `TEST_DATABASE_URL`) → record appended |
| `test_config.py` | env aliases `OBSERVANTIC_DB_URL`/`DB_URL`; defaults |
| `test_file_monitor.py` | create/modify/delete/move; patterns; throttle (event bursts coalesce); directory events ignored; raising hook doesn't kill observer; stop joins within timeout |
| `test_sqlite_monitor.py` | inserts/updates/deletes detected; schema changes; restart freshness; locked DB → `on_error`, watcher alive; poll thread detects changes when file events are suppressed |
| `test_webhook_monitor.py` | 200/400/401/404/405/413 paths; JSON parsing; query decoding; auth compare; concurrent requests (Threading); raising hook → 500 + server alive; stop_watching bounded under a wedged client |
| `test_public_api.py` | `__all__` imports; `init` re-export; version consistency with `importlib.metadata` |

DB-backed tests use `pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), …)`.

**Verify:** `uv run pytest` → **all green** without any external services except the
explicitly-marked DB integration tests.

---

## Phase D — Docs & packaging

### Step 11. Docs, packaging, cleanup (H-18, H-20)

1. **`pyproject.toml`**:
   - `version = "0.2.0"` to match `__init__.__version__` (or pick one and sync both).
   - Real `description`.
   - Keep `[dependency-groups].dev`; add `[tool.ruff]` and `[tool.mypy]` config so the
     README’s `uv run ruff format` / `uv run mypy` actually work; add ruff+mypy to dev deps.
   - `packages = ["src/observantic"]` (drop `src/examples`, or keep only non-empty examples).
2. **`README.md`** — rewrite the Quick Start and every snippet:
   - Annotated config (`watch_patterns: list[str] = [...]`, `port: int = 8080`).
   - `init(...)` then optionally `auto_persist=True` and `record_model` for auto-persistence.
   - A **“Persistence”** section that honestly describes `auto_persist` semantics (C-08).
   - Correct env vars (`OBSERVANTIC_DB_URL` or `DB_URL` — state exactly which one wins).
   - Webhook security defaults (auth recommended, size cap, timeouts).
   - Remove the false “Watcher continues running” phrasing — replace with “hook errors are
     reported via `on_error` and do not stop the watcher” (true after Step 3).
3. **Delete/repair dead code:** unused imports (Step 5), empty `app.py`, unused
   `RecordCreationException` if still unused (or wire it into `_emit` persistence), stale
   `.tmuxp.yaml` path.
4. **`devenv.nix`** — make `enterTest` run `uv run pytest`; fix the postgres port conflict
   noted in the review (or document it).

**Verify:** `uv run ruff format --check . && uv run ruff check . && uv run mypy src/observantic`
pass; `uv run pytest` green; `python -m build` produces a wheel containing only intended
files.

---

## Appendix A — Finding → Step map

| Finding | Fixed by |
|---|---|
| C-01 SQLite no-op | Step 7 (remove data_version gate) |
| C-02 PydanticUserError on config | Steps 6/7/8 (annotated fields) + Step 9/11 (examples/docs) + Step 3 (no-op stub removal) |
| C-03 Record dispatch crash / double-exec | Step 3.3 (call_unwrapped) + Step 4 (seam, auto_persist) |
| C-04 observer thread death | Step 3.2 (swallow + on_error) |
| C-05 webhook DoS / hanging stop | Step 8.1–8.3 (Threading server, timeout, tracking, bounded shutdown) |
| C-06 Content-Length handling | Step 8.2 |
| C-07 typer options ignored | Step 9.3 (constructor config) |
| C-08 persistence fiction | Step 4 (record_model + auto_persist) |
| C-09 broken tests | Steps 1, 10 |
| H-10 stale `_watching` | Step 3.1 (rollback) |
| H-11 traceback spam / error leakage | Step 8.4 |
| H-12 on_error gets string | Step 3.2 (`event`, not `event_name`) |
| H-13 env vars don't work | Step 2 |
| H-14 settings dead / double singleton | Steps 2, 4 |
| H-15 rowid-diff lossiness / SQL injection / stale checkpoints / conn leaks | Step 7 |
| H-16 track_schema_changes dead | Step 7 |
| H-17 webhook input gaps | Step 8.5–8.8 |
| H-18 version/docs/tooling mismatch | Step 11 |
| H-19 throttle growth, joins, conn close | Steps 6.3, 6.4, 7, 8.3 |
| H-20 dead exceptions/imports | Step 5, 11 |

## Appendix B — Suggested commit sequence

1. `test: add pytest harness and baseline failing tests`
2. `fix(config): honor OBSERVANTIC_* env vars; consume settings`
3. `refactor(core): state machine + resilient dispatch (call_unwrapped)`
4. `feat(core): persistence seam (_eventic.py) + record_model/auto_persist`
5. `refactor(exceptions): wire typed exceptions throughout`
6. `fix(file): start validation, throttle pruning, moved dest_path, bounded join`
7. `feat(sqlite): snapshot diffing, schema events, poll thread`
8. `fix(webhook): threading, size caps, auth, bounded shutdown`
9. `fix(examples): annotated config, constructor-based server options`
10. `test: full suite per component`
11. `docs+packaging: README, version, tooling, dead code`

Each commit keeps `uv run pytest` green (except the first two, which only set up the
harness and its baseline).
