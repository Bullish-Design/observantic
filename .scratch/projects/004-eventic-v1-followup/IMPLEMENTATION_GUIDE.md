# Observantic × eventic 1.1.0 — Follow-up Implementation Guide (004)

Hardening pass on top of project 003 (`align/eventic-v1.1.0`, commit
`4e50cc0`). Every block below was validated against the live environment:

- eventic **v1.1.0** (`/home/andrew/Documents/Projects/eventic`, tag
  `v1.1.0`, commit `b489da2`), installed in the devenv venv and `.venv`
  (`uv sync --group dev`).
- Python 3.13, pydantic 2.11.7, watchdog 6.0.0, SQLAlchemy + psycopg3
  (`psycopg[binary] 3.2.9`).
- Baseline: 88 tests pass, `ruff check`/`format` clean, `mypy` clean,
  `uv build` succeeds, `eventic --app examples.demo_app:app ... inspect`
  prints `files`/`sqlite`/`webhooks`.

## The five findings driving this project

- **F1 — `:memory:` SQLite corrupts under concurrency.** eventic's
  `SQLite(":memory:")` uses `StaticPool` + `check_same_thread=False` (one
  connection shared by all threads). Concurrent `collection.create()` calls
  from observer threads lose writes and raise `StoreError('commit failed')` /
  `NotFound('revision absent')`. Reproduced: 4 threads × 50 emits → 76
  errors, ~100/200 writes persisted. File-based SQLite (QueuePool + WAL +
  `busy_timeout`) is safe: same probe, 0 errors, 400/400 persisted.
- **F2 — handler exceptions kill the watchdog observer.** watchdog 6.x
  `EventDispatcher.run` catches only `queue.Empty`; `handler.dispatch(event)`
  exceptions propagate out of the thread. Any raise from `_emit`/`_persist`
  (ValidationError, StoreError, strict-mode ConfigurationException) silently
  ends all monitoring while `_watching` stays `True`.
- **F3 — `schema upgrade` is broken on the v1.1.0 wheel.** `admin.migrate()`
  reads `resources.files("eventic.sql.migrations") / "alembic.ini"`; the
  installed wheel has `env.py`/`versions/` but **no `alembic.ini`** (the file
  is untracked in the eventic repo at tag `v1.1.0`). Result:
  `CommandError: No 'script_location' key found in configuration`.
  `inspect`/`schema check`/`verify` work; SQLite and Postgres both
  auto-create with the default `create_tables=True`.
- **F4 — bare `postgresql://` URLs are unusable with eventic's own extra.**
  eventic's `[project.optional-dependencies] postgres = ["psycopg[binary]>=3.2"]`
  (psycopg3), but SQLAlchemy's bare `postgresql://` dialect defaults to
  psycopg2. `make_store("postgresql://...")` → `ModuleNotFoundError:
  psycopg2`. `postgresql+psycopg://` works.
- **F5 — non-annotated `stream = s` breaks pydantic ≥ 2.11.** Overriding a
  base-class pydantic field without a type annotation raises
  `PydanticUserError` ("All field definitions, including overrides, require
  a type annotation"). The 003 guide's Step 11 smoke used the broken form;
  `stream: Stream = s` is required.

## Step 0 — Preflight

```bash
cd ~/Documents/Projects/observantic
git status --short            # clean, on align/eventic-v1.1.0
git checkout -b harden/eventic-v1-followup
```

## Step 1 — Serialize persistence writes (F1)

**File: `src/observantic/core/base.py`**

Add a module-level write lock after the logger (the only place observantic
writes to a bound Collection):

```python
logger = logging.getLogger("observantic")

# eventic's SQLite(":memory:") store shares one connection across threads
# (StaticPool + check_same_thread=False) and is not safe under concurrent
# create() calls. Serialize all observantic persists process-wide; the lock
# is a no-op for file-based SQLite (QueuePool + WAL) and Postgres.
_persist_lock = Lock()
```

Hold it across the commit in `_persist`:

```python
    def _persist(self, state: Any) -> None:
        """Commit one emitted state to the bound Collection.

        Writes are serialized process-wide (see ``_persist_lock``). Store
        failures are reported via ``on_error`` and swallowed unless
        ``persist_strict`` is set — persistence is best-effort, the observer
        thread must never die (C-04).
        """
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
        with _persist_lock:
            try:
                self._collection.create(state)
            except Exception as e:
                if self.persist_strict:
                    raise
                logger.warning("persist failed (state not committed): %s", e)
                self._safe_call("on_error", e, state)
```

Note the deliberate contract change: with the default `persist_strict=False`,
a transient store error (DB down, lock contention) no longer raises into the
caller — it surfaces through `on_error` and the watcher keeps running. This
matches the resilience story already documented in
`src/examples/webhook_server.py`. `persist_strict=True` keeps the loud
raise (webhook 500, tests).

**Verify (concurrency probe — previously 76 errors / ~half lost):**

```bash
cd ~/Documents/Projects/observantic
uv run python - <<'PY'
import threading
from pydantic import BaseModel
from eventic import App, Stream
from eventic.sql import SQLite
from observantic import EventWatcher

class P(BaseModel):
    path: str = ""

s = Stream(P, name="conc4")
store = SQLite(":memory:")
runtime = App(id="t", streams=[s]).bind(store)
col = runtime[s]

class W(EventWatcher):
    stream: Stream = s

watchers = [W(auto_persist=True) for _ in range(4)]
for w in watchers:
    w.bind(runtime)

errors = []
def worker(w, prefix):
    for i in range(50):
        try:
            w._emit(path=f"{prefix}-{i}")
        except Exception as e:
            errors.append((prefix, i, repr(e)))

threads = [threading.Thread(target=worker, args=(w, f"t{t}")) for t, w in enumerate(watchers)]
for t in threads: t.start()
for t in threads: t.join()
print("errors:", len(errors), errors[:5])
print("persisted:", len(col.where(limit=1000).items))
assert not errors and len(col.where(limit=1000).items) == 200
store.close()
print("CONCURRENCY OK")
PY
```

(Expect `errors: 0` and `persisted: 200`.)

## Step 2 — Observer-thread safety (F2)

**File: `src/observantic/core/base.py`** — add an emit helper that never
raises, right below `_emit`:

```python
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
```

**File: `src/observantic/monitors/file.py`** — the four watchdog handlers
call `_emit_safe` with the watchdog event instead of `_emit`:

```python
            def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
                if not event.is_directory and not parent._should_throttle(
                    str(event.src_path)
                ):
                    parent._emit_safe(
                        event,
                        path=str(Path(str(event.src_path)).resolve()),
                        event_type="created",
                        is_directory=event.is_directory,
                    )
                    parent._dispatch_hook("on_file_created", event)
```

Same shape for `on_modified` (`"modified"`), `on_deleted`
(`"deleted"`, no throttle check today), and `on_moved` (`"moved"` +
`dest_path=...`). Hooks still fire after a failed emit — the emit failure
goes to `on_error`, the hook pipeline is unaffected.

**File: `src/observantic/monitors/sqlite.py`**

- `_emit_row`: route through `_emit_safe(row, ...)`:

```python
        self._emit_safe(
            row,
            table_name=table,
            row_data=row_data,
            row_id=rid,
            operation=operation,
        )
```

- The watchdog handler wraps `_check_for_changes` (which can raise on
  emit/persist) so the observer thread survives:

```python
        class SQLiteHandler(FileSystemEventHandler):
            def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
                if not event.is_directory:
                    if str(Path(str(event.src_path)).resolve()) == parent._db_path:
                        try:
                            parent._check_for_changes()
                        except Exception as e:
                            parent._safe_call("on_error", e, event)
```

(The poll thread already has its own `try/except` in `_poll_loop`.)

**File: `src/observantic/monitors/webhook.py`** — no change needed: the
handler's `_handle_request` already wraps `_emit` in a broad
`except Exception` that produces a generic 500 and logs via `on_error`.

## Step 3 — psycopg3 URL translation (F4)

**File: `src/observantic/_eventic.py`** — in `make_store`, translate a bare
`postgresql://` URL to eventic's documented driver (psycopg3):

```python
        if url.startswith("postgresql"):
            from eventic.sql import Postgres

            if url.startswith("postgresql://"):
                # SQLAlchemy's bare postgresql:// dialect defaults to
                # psycopg2, but eventic's [postgres] extra ships psycopg3
                # (psycopg[binary]). Translate so the documented URL works.
                url = "postgresql+psycopg" + url[len("postgresql"):]
            return Postgres(url, create_tables=create_tables)
```

Update the module docstring line:

```text
* Stores: ``eventic.sql.SQLite`` (dev/test) and ``eventic.sql.Postgres``
  (production). Bare ``postgresql://`` URLs are translated to
  ``postgresql+psycopg://`` (eventic's ``[postgres]`` extra ships psycopg3).
  ``Store.close()`` is idempotent.
```

Verified driver path (psycopg3 is installed by `uv sync --group dev`):

```bash
uv run python -c "import psycopg; print(psycopg.__version__)"   # 3.2.x
```

Live Postgres is **not** reachable from this shell (the devenv Postgres
service socket is not visible and the listener on :5432 rejects known
credentials), so the translation is unit-tested with a stub (Step 6) and the
real round-trip lives in the opt-in integration test (Step 8).

## Step 4 — Document the migration gap (F3)

**File: `src/examples/demo_app.py`** — replace the docstring:

```python
"""A ready-to-run eventic App over observantic's default streams.

Use it with the eventic CLI, e.g.:

    uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect
    uv run eventic --app examples.demo_app:app --url sqlite:///demo.db verify

``schema upgrade`` (Alembic) is for Postgres production only. eventic
v1.1.0's wheel omits its alembic.ini (untracked upstream), so the CLI
command fails until upstream ships a fix; prefer ``Postgres(url)`` with the
default ``create_tables=True`` for Postgres bootstrapping. SQLite creates
its tables automatically on store construction.
"""
```

**File: `README.md`** — Persistence/backends paragraph:

```markdown
* **Backends**: `SQLite` for dev/test/single-process; `Postgres` for
  production (`pip install eventic[postgres]` — ships the psycopg3 driver,
  and `make_store` translates bare `postgresql://` URLs automatically).
  Schema is created automatically by both backends (`create_tables=True`
  default). `eventic schema upgrade` (Alembic) is Postgres-only and
  currently unavailable: eventic v1.1.0's wheel omits its `alembic.ini`
  (untracked upstream). `eventic schema check` / `verify` work on SQLite.
```

## Step 5 — examples package + version bump

**File: `src/examples/__init__.py`** (new):

```python
"""Runnable Observantic examples (also used by the eventic CLI).

    uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect
"""

from __future__ import annotations
```

**File: `pyproject.toml`** — `version = "0.4.0"`.
**File: `src/observantic/__init__.py`** — `__version__ = "0.4.0"`.

## Step 6 — Tests

### 6.1 `tests/test_core_eventic.py` — additions

```python
from eventic.errors import StoreError
```

New tests (append to the persistence section):

```python
def test_persist_error_reports_to_on_error_when_not_strict():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        errors = []

        class W(EmittingWatcher):
            def on_error(self, error, event=None):
                errors.append((error, event))

        w = W(auto_persist=True)
        w.bind(runtime)
        w._collection.create = lambda *a, **k: (_ for _ in ()).throw(
            StoreError("boom")
        )
        state = w._emit(path="/a", event_type="created", is_directory=False)
        assert state is not None  # emit still returns the state
        assert len(errors) == 1
        assert isinstance(errors[0][0], StoreError)
        assert errors[0][1] is state  # on_error receives the state
    finally:
        store.close()


def test_persist_error_reraises_when_strict():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(auto_persist=True, persist_strict=True)
        w.bind(runtime)
        w._collection.create = lambda *a, **k: (_ for _ in ()).throw(
            StoreError("boom")
        )
        with pytest.raises(StoreError):
            w._emit(path="/a", event_type="created", is_directory=False)
    finally:
        store.close()


def test_emit_safe_routes_errors_to_on_error_and_returns_none():
    errors = []

    class W(EmittingWatcher):
        stream = STRICT_STREAM  # wrong fields -> ValidationError at emit time

        def on_error(self, error, event=None):
            errors.append((error, event))

    w = W()
    event = object()
    state = w._emit_safe(event, bogus=1)
    assert state is None
    assert len(errors) == 1
    assert isinstance(errors[0][0], ValidationError)
    assert errors[0][1] is event


def test_concurrent_emits_on_memory_store_are_serialized():
    import threading

    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        watchers = [EmittingWatcher(auto_persist=True) for _ in range(4)]
        for w in watchers:
            w.bind(runtime)
        errors = []

        def worker(w, prefix):
            for i in range(25):
                try:
                    w._emit(path=f"{prefix}-{i}", event_type="created")
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(w, f"t{t}"))
            for t, w in enumerate(watchers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(runtime[PROBE_STREAM].where(limit=1000).items) == 100
    finally:
        store.close()


def test_make_store_translates_bare_postgres_url_to_psycopg3(monkeypatch):
    import eventic.sql as sq

    captured = {}

    class StubPostgres:
        def __init__(self, url, *, create_tables=True):
            captured["url"] = url
            captured["create_tables"] = create_tables

        def close(self):
            pass

    monkeypatch.setattr(sq, "Postgres", StubPostgres)
    store = make_store("postgresql://u:p@h/db")
    assert captured["url"] == "postgresql+psycopg://u:p@h/db"
    assert captured["create_tables"] is True
    store.close()


def test_make_store_keeps_explicit_psycopg_dialect(monkeypatch):
    import eventic.sql as sq

    captured = {}

    class StubPostgres:
        def __init__(self, url, *, create_tables=True):
            captured["url"] = url

        def close(self):
            pass

    monkeypatch.setattr(sq, "Postgres", StubPostgres)
    store = make_store("postgresql+psycopg://u:p@h/db")
    assert captured["url"] == "postgresql+psycopg://u:p@h/db"
    store.close()
```

(Simplify `test_concurrent_emits...` to just bind explicitly — the first
list comprehension line above is a red herring; bind in a loop.)

### 6.2 `tests/test_outbox_worker.py` (new)

```python
"""Outbox delivery loop: watcher persists -> Outbox subscription -> Worker.

Proves observantic's durable-delivery story end to end on SQLite: an
auto-persisting watcher writes commits, the outbox drains exactly once, and
a second drain is a no-op.
"""

from __future__ import annotations

from eventic import App, Outbox, Stream, Subscription
from eventic.sql import SQLite
from eventic.worker import Worker
from pydantic import BaseModel

from observantic import EventWatcher


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


PROBE_STREAM = Stream(ProbeEvent, name="probe")


class EmittingWatcher(EventWatcher):
    stream: Stream | None = PROBE_STREAM


def test_outbox_worker_delivers_persisted_commits(tmp_path):
    seen = []

    def handler(commit):
        seen.append((commit.kind, commit.revision.state.path))

    app = App(
        id="outbox-test",
        streams=[PROBE_STREAM],
        subscriptions=[
            Subscription(
                id="outbox-test.probe",
                stream=PROBE_STREAM,
                handler=handler,
                delivery=Outbox(queue="q"),
            )
        ],
    )
    store = SQLite(str(tmp_path / "events.db"))
    try:
        runtime = app.bind(store)
        w = EmittingWatcher(auto_persist=True)
        w.bind(runtime)
        w._emit(path="/a", event_type="created")
        w._emit(path="/b", event_type="created")

        report = Worker(app, store, queue="q").drain_once()
        assert report.claimed == 2 and report.delivered == 2
        assert seen == [("create", "/a"), ("create", "/b")]

        # second drain is a no-op (intents are claimed, not re-delivered)
        assert Worker(app, store, queue="q").drain_once().claimed == 0
        assert seen == [("create", "/a"), ("create", "/b")]
    finally:
        store.close()
```

### 6.3 `tests/test_public_api.py` — version bump

`test_version_consistent_with_metadata`: `"0.3.0"` → `"0.4.0"` (both
occurrences).

### 6.4 `tests/test_postgres_integration.py` (new, opt-in)

```python
"""Optional Postgres integration tests (skipped without TEST_DATABASE_URL).

Run against the devenv Postgres, e.g.:

    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic \
      uv run pytest tests/test_postgres_integration.py
"""

from __future__ import annotations

import os

import pytest
from eventic import App, Stream
from pydantic import BaseModel

from observantic import make_store

URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not URL, reason="TEST_DATABASE_URL not set (needs a live Postgres)"
)


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


PROBE_STREAM = Stream(ProbeEvent, name="pg_probe")


def test_make_store_roundtrip_on_postgres():
    store = make_store(URL)
    try:
        # bare postgresql:// URLs are translated to psycopg3
        assert store.engine.url.drivername == "postgresql+psycopg"
        runtime = App(id="pg-test", streams=[PROBE_STREAM]).bind(store)
        runtime[PROBE_STREAM].create(ProbeEvent(path="/pg", event_type="created"))
        items = runtime[PROBE_STREAM].where(path="/pg").items
        assert len(items) == 1
        assert items[0].state.path == "/pg"
    finally:
        store.close()
```

### 6.5 First green checkpoint

```bash
uv run pytest -q
```

Expected: all previous tests still pass (the `_persist` contract change only
touches the failure path; `test_unbind_stops_persistence` uses
`persist_strict=True` and still raises).

## Step 7 — README updates

- **Quick Start**: construct with `auto_persist=True` instead of assigning
  after the fact:

```python
# Monitor files (no database required)
watcher = DocumentWatcher(stream=files)
watcher.start_watching("/documents")
watcher.stop_watching()

# Persistence is explicit: bind a store, opt in at construction
store = SQLite("observantic.db")
watcher = DocumentWatcher(stream=files, auto_persist=True)
watcher.bind(app.bind(store))
watcher.start_watching("/documents")
```

- Add a **subclass note** near the watcher docs: overriding the default
  `stream` field requires the annotated form — `stream: Stream = my_stream`
  — because pydantic rejects non-annotated overrides of base-class fields
  (pydantic ≥ 2.11).
- **Error Handling**: add a sentence — persistence failures are best-effort:
  with the default `persist_strict=False` a failed commit reports via
  `on_error(error, state)` and monitoring continues; `persist_strict=True`
  raises (e.g. the webhook monitor answers 500).
- **Config**: note that `make_store` accepts `postgresql://` and
  `postgresql+psycopg://` (bare URLs are translated to the psycopg3 driver
  that `eventic[postgres]` installs).
- Add a **0.4.0** release-notes block: serialized/safe persistence writes,
  observer-thread error routing (`on_error`), `_emit_safe`, psycopg3 URL
  translation, outbox/worker test, opt-in Postgres integration tests.

## Step 8 — Postgres integration test (live)

From a shell where the devenv Postgres is running (`devenv up` or a manual
`pg_ctl`), the suite exercises the psycopg3 path:

```bash
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic \
  uv run pytest tests/test_postgres_integration.py -q
```

Not run here: the devenv service socket is not visible from this shell and
the listener on :5432 rejects the documented credentials. The test is
skipped by default, exactly as `devenv.nix`'s `enterTest` comment promises.

## Step 9 — Full quality gate

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src/observantic
uv run pytest -q
uv build --no-sources        # wheel builds and contains src/examples
uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect
uv run eventic --app examples.demo_app:app --url sqlite:///demo.db verify
```

Known mypy note: `w._collection.create = ...` in tests is fine (mypy only
checks `src/observantic`).

## Step 10 — Commit

```bash
git add -A
git commit -m "harden eventic 1.1.0 followup (v0.4.0)

- serialize persistence writes; eventic :memory: SQLite (StaticPool) is
  unsafe under concurrent create() from observer threads (F1)
- observer-thread safety: _persist routes store errors to on_error unless
  persist_strict; monitors emit via _emit_safe so a raise never kills the
  watchdog observer thread (F2, C-04)
- make_store translates bare postgresql:// to postgresql+psycopg://
  (eventic[postgres] ships psycopg3) (F4)
- document schema upgrade gap: eventic v1.1.0 wheel omits alembic.ini (F3)
- examples is now a regular package (__init__.py)
- add outbox/worker delivery test and opt-in Postgres integration test
- bump to 0.4.0; README/release notes
"
```

## Appendix A — Decisions locked during implementation

1. **`_persist` swallows store errors by default.** The alternative —
   propagating into watchdog/webhook handler threads — either kills the
   observer thread (file/sqlite) or 500s every webhook while the DB is down
   (webhook). `on_error` keeps observability; `persist_strict=True` opts
   back into loud raises. The "not bound" warning behavior is unchanged.
2. **Writes are serialized process-wide, not per-watcher.** Multiple
   watchers can share one stream/collection; only a global lock makes the
   `:memory:` single-connection path deterministic. The lock is held only
   around `create`, not around `_emit`/hook dispatch.
3. **The psycopg3 translation lives in observantic, not eventic.** eventic
   is a pinned git dep; observantic's `make_store` is the documented seam for
   store construction, so the fix belongs there (plus a stub unit test and an
   opt-in integration test).

## Appendix B — Verification checklist (004)

- [ ] concurrency probe (Step 1): 200/200 persisted, 0 errors on `:memory:`
- [ ] `uv run pytest -q` → all green (incl. new outbox + persistence tests)
- [ ] `uv run ruff check . && uv run ruff format --check .` → clean
- [ ] `uv run mypy src/observantic` → clean
- [ ] `uv build --no-sources` → wheel contains `examples/__init__.py` + the
      five example modules and the `start` entry point
- [ ] `uv run eventic --app examples.demo_app:app --url sqlite:///demo.db
      inspect` prints `files`/`sqlite`/`webhooks`
- [ ] `uv run eventic --app examples.demo_app:app --url sqlite:///demo.db
      verify` → `verified 0 revisions ... 0 mismatches`
- [ ] Postgres integration test exists and skips cleanly without
      `TEST_DATABASE_URL`
- [ ] README quick start uses `auto_persist=True` at construction; the
      subclass `stream: Stream = ...` annotation is documented
- [ ] no lingering `0.3.0` version strings outside `.scratch/` history

## Appendix C — Corrections to project 003

- **Step 1 verify**: `import eventic; print(eventic.__version__)` works in
  the devenv venv and after `uv sync --group dev` (eventic 1.1.0 defines
  `__version__`); a stale `.venv` from before the 003 sync may still import
  eventic 0.1.5 — run `uv sync --group dev` first.
- **Step 11 watcher smoke**: the subclass must annotate the stream override —
  `stream: Stream = s` — or pydantic ≥ 2.11 raises `PydanticUserError`.
- **Step 11 `schema upgrade`**: fails on the v1.1.0 wheel (`alembic.ini`
  missing); use `inspect`/`verify` for SQLite verification (F3).
- **`where()` page size**: `Collection.where(limit=...)` defaults to 100 —
  count assertions must pass `limit=1000` (or page).
- **Step 11 watcher smoke, assertion**: `where(path="hello.txt")` is an
  **exact** match — the persisted state path is absolute
  (`Path(src_path).resolve()`), so the smoke's
  `where(path="hello.txt")` returns 0. Count all items
  (`where(limit=1000).items`) or filter by the resolved absolute path.
  Also note: watching a directory that contains the store's own database
  files will observe their WAL/journal churn as `modified` events — point
  `watch_patterns` at the real content or keep the DB outside the watched
  tree.
