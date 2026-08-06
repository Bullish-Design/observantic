# Observantic 0.3.0 — Follow-up Implementation Guide

Companion to `.scratch/projects/003-eventic-v1-alignment/` (the eventic 1.1.0
alignment, **already implemented** on branch `align/eventic-v1.1.0`, commit
`4e50cc0`). This guide implements everything the 003 guide left open:

1. Merge the alignment to `main` and prepare the 0.3.0 release.
2. Finish landing the **eventic `alembic.ini` packaging fix** (un-ignored but
   still uncommitted/untagged) and pin observantic to `v1.1.2`.
3. Fix the broken devenv Postgres service (stale `unix_socket_directories`
   hardcode) and add a `TEST_DATABASE_URL`-gated **Postgres integration
   test**.
4. Add `key_aggregates` to `SQLiteEventBase` (per-row durable revision
   history — OVERVIEW §7.7, deferred from 0.3.0).
5. Fix the broken `start` console script (typer entry point).
6. Full quality gate, merge, tag **v0.3.0**, release notes.

Every code block in this guide was validated live against eventic 1.1.0
(`SQLite(":memory:")` and a throwaway `Postgres` 17 cluster):

- `postgresql://` URLs default to the **psycopg2** driver, which
  `eventic[postgres]` does not install → all Postgres URLs must use
  **`postgresql+psycopg://`**.
- `make_store` → `Postgres` (tables created on construction via
  `create_all`), `App.bind` → writes/reads/CAS/NotFound all work; `schema
  upgrade` works once `alembic.ini` ships.
- Keyed-aggregate semantics: `create(state, id=key)` on an existing
  aggregate with identical content is a replay no-op; with different content
  it raises `RevisionConflict`; `collection.get(key)` raises `NotFound` for
  pre-existing rows never emitted. The `persist_row` helper below handles
  every case (rowid reuse, pre-existing rows, tombstones).

The tree is **expected to stay green** throughout: each step ends with a
verification gate.

---

## Step 0 — Preflight: merge the alignment to `main`

The alignment (0.3.0 API, `4e50cc0`) is on `align/eventic-v1.1.0`, not yet on
`main`. Merge it first so the follow-up work builds on it.

```bash
cd ~/Documents/Projects/observantic
git status --short            # clean
git checkout main
git pull origin main
git merge align/eventic-v1.1.0   # or: open a PR and merge it on GitHub
git push origin main
```

Verify main is green before touching anything else:

```bash
devenv shell "uv run pytest -q"            # 88 passed
devenv shell "uv run ruff check ."          # clean
devenv shell "uv run mypy src/observantic"  # clean
```

Create the follow-up branch:

```bash
git checkout -b feature/eventic-v1-followup
```

---

## Step 1 — Land the upstream eventic fix (finish the alembic.ini packaging)

In the eventic repo the fix is **half-done**: `.gitignore` no longer ignores
`alembic.ini` and the file exists on disk, but it is **not committed and no
tag exists** — so a fresh clone/wheel still cannot run `eventic schema
upgrade` ("No 'script_location' key found"). Land it:

```bash
cd ~/Documents/Projects/eventic
git status --short            # expect: M .gitignore, ?? src/eventic/sql/migrations/alembic.ini
git rev-parse v1.1.2 >/dev/null 2>&1 && echo "already tagged — skip to Step 2" || true

# 1. commit the fix (alembic.ini must be included in the sdist)
git add src/eventic/sql/migrations/alembic.ini .gitignore
git commit -m "fix(packaging): ship alembic.ini so 'eventic schema upgrade' works

alembic.ini was gitignored, so the v1.1.0 wheel could never run
migrations (SqlAdmin.migrate: 'No script_location key found in
configuration'). Un-ignore it and include it in the sdist."

# 2. bump the version (uv derives the package version from the tag)
sed -i 's/^version = "1.1.0"/version = "1.1.1"/' pyproject.toml
sed -i 's/^__version__ = "1.1.0"/__version__ = "1.1.1"/' src/eventic/__init__.py
git add pyproject.toml src/eventic/__init__.py
git commit -m "release: 1.1.1"

# 3. tag and push
git tag v1.1.2
git push origin main --tags
```

Verify the tag ships the file:

```bash
git show v1.1.2:src/eventic/sql/migrations/alembic.ini | head -3   # [alembic]
```

> If the maintainer already tagged `v1.1.2` (or you cannot push to the
> eventic remote), pin observantic to `rev = "<commit-hash>"` instead and
> skip the `git tag`/`push` lines.

---

## Step 2 — Pin observantic to eventic `v1.1.2`

**File: `pyproject.toml`** — one line:

```toml
[tool.uv.sources]
eventic = { git = "https://github.com/Bullish-Design/eventic.git", tag = "v1.1.2" }
```

Re-resolve and verify:

```bash
cd ~/Documents/Projects/observantic
devenv shell "uv lock && uv sync --group dev"
devenv shell "uv run python -c 'import eventic; print(eventic.__version__)'"   # 1.1.1
```

Prove `schema upgrade` now works **without any shim** (this is the exact
failure the fix cures):

```bash
devenv shell "uv run eventic --app examples.demo_app:app --url sqlite:///demo.db schema upgrade"
# expected: INFO alembic ... Running upgrade  -> 0001, baseline
#           schema upgraded
rm -f demo.db
```

Commit:

```bash
git add pyproject.toml uv.lock
git commit -m "chore: pin eventic v1.1.2 (ships alembic.ini for schema upgrade)"
```

---

## Step 3 — Fix the devenv Postgres service

Two problems in the current `devenv.nix`:

1. `settings.unix_socket_directories = "/run/user/1000/devenv-11f13c9/postgres"`
   hardcodes a **stale devenv instance directory** that no longer exists —
   Postgres dies at startup: `could not create lock file
   "/run/user/1000/devenv-11f13c9/postgres/.s.PGSQL.<port>.lock"`.
2. The Postgres URL in docs/comments must use **`postgresql+psycopg://`**,
   not `postgresql://` (psycopg2 is not installed; see the probe note in the
   header).

**File: `devenv.nix`** — replace the postgres `settings` block:

```nix
    settings = {
      unix_socket_directories = "/tmp";
    };
```

The rest of the service block (`enable`, `package`, `initialScript`,
`initialDatabases`, `listen_addresses`, `port = 5432`) is unchanged. This
was validated with a scratch devenv project: the server starts, the
`eventic` database is created, and `postgres:postgres` password auth works.
(The `initialScript`'s `CREATE USER postgres` logs "role already exists" —
harmless: the role pre-exists as the superuser and `ALTER USER ... WITH
SUPERUSER` + `CREATE DATABASE eventic` still succeed.)

Also update the `enterTest` comment (it still says `postgresql://...` and
references a test file that Step 4 creates):

```nix
  # Tests run on SQLite by default (eventic 1.1.1 backend). The devenv
  # Postgres at 127.0.0.1:5432 (db "eventic", user/pass postgres/postgres)
  # is available for optional Postgres integration tests. Note the
  # postgresql+psycopg:// scheme — SQLAlchemy's plain postgresql:// defaults
  # to the psycopg2 driver, which eventic[postgres] (psycopg 3) does not
  # install:
  #   TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic \
  #     uv run pytest tests/test_postgres_integration.py
```

Regenerate the stale service state (it has an old port baked in) and start
the service:

```bash
rm -rf .devenv/state/postgres            # dev database — nothing to lose
devenv up                                # long-running; keep it in a terminal
```

Verify connectivity (from another terminal):

```bash
devenv shell "uv run python -c \"from eventic.sql import Postgres; s = Postgres('postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic'); print('PG OK'); s.close()\""
```

> **Port conflict:** this machine also runs a system Postgres on 5432. If the
> devenv service cannot bind (`address already in use`), either stop the
> other server or change `port` in `devenv.nix` (e.g. `5434`) and use the
> matching `TEST_DATABASE_URL` everywhere below.

Commit:

```bash
git add devenv.nix
git commit -m "chore(devenv): fix postgres unix socket dir; document postgresql+psycopg:// URL"
```

---

## Step 4 — Postgres integration test

**File: `tests/test_postgres_integration.py`** (new). Skip-gated on
`TEST_DATABASE_URL`; exercises observantic's public API end to end
(`make_store` → `Postgres`, `build_app` → `bind` → watcher `_emit` with
`auto_persist` → `where`/`get`/CAS). Validated against a real Postgres 17
cluster.

```python
"""Postgres integration tests (skip unless TEST_DATABASE_URL is set).

Uses observantic's public API end to end: make_store -> Postgres,
build_app -> bind -> watcher emit (auto_persist) -> where/get/history, plus
CAS conflicts and NotFound. Mirrors test_core_eventic on the SQLite backend.

The URL must use the postgresql+psycopg:// scheme: SQLAlchemy's plain
postgresql:// defaults to the psycopg2 driver, which eventic[postgres]
(psycopg 3) does not install. See devenv.nix enterTest for the devenv
Postgres example.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from eventic import Stream
from eventic.errors import NotFound, RevisionConflict
from pydantic import BaseModel

from observantic import EventWatcher, build_app, make_store

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "TEST_DATABASE_URL not set (e.g. "
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic)"
    ),
)


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


PROBE_STREAM = Stream(ProbeEvent, name="pg_probe")


class EmittingWatcher(EventWatcher):
    stream: Stream | None = PROBE_STREAM


def test_make_store_postgres_and_outbox_capability():
    store = make_store(TEST_DATABASE_URL)
    try:
        assert "postgres" in str(store.engine.url)
        assert store.capabilities.outbox is True
    finally:
        store.close()


def test_watcher_emit_persists_through_bound_runtime():
    store = make_store(TEST_DATABASE_URL)
    try:
        runtime = build_app(id="pg-t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(auto_persist=True)
        w.bind(runtime)

        marker = f"/pg/{uuid4()}"  # unique per run: the DB persists across runs
        w._emit(path=marker, event_type="created", is_directory=False)
        w._emit(path=marker, event_type="modified", is_directory=False)

        page = runtime[PROBE_STREAM].where(path=marker)
        assert len(page.items) == 2  # one new aggregate per event
        assert all(it.revision == 0 for it in page.items)
    finally:
        store.close()


def test_cas_conflict_on_stale_base():
    store = make_store(TEST_DATABASE_URL)
    try:
        runtime = build_app(id="pg-cas", streams=[PROBE_STREAM]).bind(store)
        col = runtime[PROBE_STREAM]
        r0 = col.create(ProbeEvent(path="/cas"))
        r1 = col.change(r0, path="/cas2")
        assert r1.revision == 1
        assert col.get(r0.id, revision=0).state.path == "/cas"
        with pytest.raises(RevisionConflict):  # stale base (I7)
            col.change(r0, path="/cas3")
    finally:
        store.close()


def test_get_missing_raises_not_found():
    store = make_store(TEST_DATABASE_URL)
    try:
        runtime = build_app(id="pg-nf", streams=[PROBE_STREAM]).bind(store)
        with pytest.raises(NotFound):
            runtime[PROBE_STREAM].get(uuid4())
    finally:
        store.close()
```

Run it against the devenv Postgres (still running from Step 3):

```bash
devenv shell "TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic uv run pytest tests/test_postgres_integration.py -v"
# 4 passed
```

Confirm the suite stays green without the env var (tests skip):

```bash
devenv shell "uv run pytest -q"     # 88 passed, 4 skipped (all green)
```

Commit:

```bash
git add tests/test_postgres_integration.py
git commit -m "test: add Postgres integration test (TEST_DATABASE_URL-gated)"
```

---

## Step 5 — `key_aggregates` on `SQLiteEventBase` (per-row revision history)

Adds OVERVIEW §3.4/§7.7: an opt-in `key_aggregates: bool = False` flag. When
`True`, each SQLite row maps to a **stable aggregate** —
`uuid5(NAMESPACE_URL, "observantic:sqlite:{table}:{row_id}")` — so inserts
`create`, updates/deletes `replace` on the head, and every row accumulates a
durable revision history (updates/deletes were impossible to express in
eventic 0.1.5). Default `False` keeps the current one-create-per-event
behavior.

### 5.1 Seam helpers — `src/observantic/_eventic.py`

Add the stdlib import with the existing stdlib block (after `from typing
import Any`):

```python
from uuid import NAMESPACE_URL, UUID, uuid5
```

Add the eventic imports with the existing eventic block (after `from
.eventic.subscription import Subscription`):

```python
from eventic.errors import NotFound, RevisionConflict
from eventic.runtime import Collection
```

Add the helpers after `build_app`:

```python
def sqlite_aggregate_key(table: str, row_id: int | str | None) -> UUID:
    """Deterministic aggregate id for one SQLite row.

    ``uuid5(NAMESPACE_URL, f"observantic:sqlite:{table}:{row_id}")`` — stable
    across processes and restarts, so updates and deletes append to the same
    aggregate (durable revision history per row).
    """
    return uuid5(NAMESPACE_URL, f"observantic:sqlite:{table}:{row_id}")


def persist_row(collection: Collection[Any], state: Any, *, keyed: bool) -> None:
    """Commit one emitted row state through the collection.

    ``keyed=False`` (legacy): every event is a fresh aggregate (revision 0).
    ``keyed=True``: inserts -> ``create(state, id=key)``; updates and deletes
    -> ``replace`` on the head (delete states are already tombstones:
    ``row_data=None``, ``operation="deleted"``). Rowid reuse after a delete
    falls back to replace on the existing aggregate; a pre-existing row that
    was never emitted (first snapshot) creates on first change/delete.
    """
    if not keyed:
        collection.create(state)
        return
    key = sqlite_aggregate_key(
        getattr(state, "table_name", ""), getattr(state, "row_id", None)
    )
    if getattr(state, "operation", "inserted") == "inserted":
        try:
            collection.create(state, id=key)
        except RevisionConflict:
            collection.replace(collection.get(key), state)  # rowid reused
        return
    try:
        head = collection.get(key)
    except NotFound:
        collection.create(state, id=key)  # row pre-existed at start
        return
    collection.replace(head, state)
```

Add both names to the seam's `__all__`:

```python
__all__ = [
    "DEFAULT_DB_URL",
    "build_app",
    "make_store",
    "persist_row",
    "sqlite_aggregate_key",
]
```

### 5.2 Monitor field + override — `src/observantic/monitors/sqlite.py`

Add the seam import (with the other imports):

```python
from .._eventic import persist_row
```

Add the field to `SQLiteEventBase` (next to the other `Field` config, after
`max_table_rows`):
```python
    key_aggregates: bool = Field(
        default=False,
        description=(
            "Key row events to stable aggregates (one revision history per "
            "row): inserts -> create, updates/deletes -> replace on the head. "
            "Aggregate id is uuid5(NAMESPACE_URL, "
            "'observantic:sqlite:{table}:{row_id}'). Default False: every "
            "event is a fresh aggregate (revision 0)."
        ),
    )
```

Add the `_persist` override (near `_default_record_model`):

```python
    def _persist(self, state: Any) -> None:
        """Commit one emitted row state; keyed mode derives the aggregate id."""
        if self._collection is None:
            return super()._persist(state)  # warn / strict via the base
        persist_row(self._collection, state, keyed=self.key_aggregates)
```

No circular import: `observantic._eventic` imports only `config`/`exceptions`.

### 5.3 Tests — `tests/test_keyed_aggregates.py` (new)

```python
"""Keyed-aggregate persistence for SQLiteEventBase (key_aggregates=True).

Validated semantics (eventic 1.1.0):
* create(state, id=key) on an existing aggregate: identical content -> replay
  no-op; different content -> RevisionConflict (the rowid-reuse fallback).
* collection.get(key) -> NotFound for pre-existing rows never emitted
  (first snapshot); persist_row creates on first change/delete instead.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid5

from eventic import App, Stream
from eventic.sql import SQLite

from observantic import EventWatcher
from observantic._eventic import persist_row, sqlite_aggregate_key
from observantic.monitors.sqlite import DatabaseRow, SQLiteEventBase

KEYED_STREAM = Stream(DatabaseRow, name="keyed_rows")


def row(operation: str, rid: int, data: dict | None = None) -> DatabaseRow:
    return DatabaseRow(table_name="t", row_id=rid, row_data=data, operation=operation)


def col_fixture():
    store = SQLite(":memory:")
    runtime = App(id="keyed", streams=[KEYED_STREAM]).bind(store)
    return store, runtime[KEYED_STREAM]


# -- unit: persist_row ------------------------------------------------------


def test_aggregate_key_is_deterministic():
    k1 = sqlite_aggregate_key("t", 42)
    k2 = sqlite_aggregate_key("t", 42)
    assert k1 == k2 == uuid5(NAMESPACE_URL, "observantic:sqlite:t:42")
    assert sqlite_aggregate_key("t", 42) != sqlite_aggregate_key("t", 43)


def test_insert_update_delete_single_aggregate_history():
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 1, {"id": 1, "data": "a"}), keyed=True)
        persist_row(col, row("updated", 1, {"id": 1, "data": "b"}), keyed=True)
        persist_row(col, row("deleted", 1), keyed=True)  # tombstone
        r = col.get(sqlite_aggregate_key("t", 1))
        assert r.revision == 2
        assert r.state.operation == "deleted"
        assert [
            x.revision for x in col.history(sqlite_aggregate_key("t", 1)).items
        ] == [0, 1, 2]
    finally:
        store.close()


def test_distinct_rowids_distinct_aggregates():
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 1, {"id": 1}), keyed=True)
        persist_row(col, row("inserted", 2, {"id": 2}), keyed=True)
        assert col.get(sqlite_aggregate_key("t", 1)).revision == 0
        assert col.get(sqlite_aggregate_key("t", 2)).revision == 0
        assert len(col.where(table_name="t").items) == 2
    finally:
        store.close()


def test_rowid_reuse_appends_to_existing_aggregate():
    """A rowid reused after a delete must continue the aggregate, not clash."""
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 1, {"id": 1}), keyed=True)
        persist_row(col, row("deleted", 1), keyed=True)
        persist_row(col, row("inserted", 1, {"id": 1, "data": "again"}), keyed=True)
        r = col.get(sqlite_aggregate_key("t", 1))
        assert r.revision == 2  # insert -> replace on the head (rev 1 -> 2)
        assert [
            x.revision for x in col.history(sqlite_aggregate_key("t", 1)).items
        ] == [0, 1, 2]
    finally:
        store.close()


def test_pre_existing_row_delete_creates_fallback():
    """A row deleted before it was ever emitted must create, not NotFound."""
    store, col = col_fixture()
    try:
        persist_row(col, row("deleted", 99), keyed=True)
        r = col.get(sqlite_aggregate_key("t", 99))
        assert r.revision == 0
        assert r.state.operation == "deleted"
    finally:
        store.close()


def test_keyed_false_keeps_legacy_random_aggregates():
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 7, {"id": 7}), keyed=False)
        assert len(col.where(row_id=7).items) == 1
        assert col.where(row_id=7).items[0].revision == 0
    finally:
        store.close()


def test_keyed_watcher_warns_without_bind(caplog):
    """key_aggregates alone is not persistence; the base warn/strict applies."""
    w = SQLiteEventBase(key_aggregates=True, auto_persist=True)
    with caplog.at_level(logging.WARNING, logger="observantic"):
        w._emit(table_name="t", row_data=None, row_id=1, operation="deleted")
    assert "not bound" in caplog.text


# -- integration: real monitor through the poll loop ------------------------


def _build_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
    conn.execute("INSERT INTO t (id, data) VALUES (5, 'pre-existing')")
    conn.commit()
    conn.close()


def _mutate(db, op, rid, data=None):
    conn = sqlite3.connect(db)
    if op == "insert":
        conn.execute("INSERT INTO t (data) VALUES (?)", (data,))
    elif op == "update":
        conn.execute("UPDATE t SET data=? WHERE id=?", (data, rid))
    else:
        conn.execute("DELETE FROM t WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def wait_for(predicate, timeout=6.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_monitor_keyed_integration(tmp_path):
    db = tmp_path / "keyed.db"
    _build_db(str(db))
    seen = []

    class W(SQLiteEventBase):
        def on_row_inserted(self, row):
            seen.append(("inserted", row.row_id))

        def on_row_updated(self, row):
            seen.append(("updated", row.row_id))

        def on_row_deleted(self, row):
            seen.append(("deleted", row.row_id))

    store = SQLite(":memory:")
    runtime = App(id="keyed-live", streams=[KEYED_STREAM]).bind(store)
    w = W(
        stream=KEYED_STREAM,
        key_aggregates=True,
        auto_persist=True,
        poll_interval_seconds=0.1,
    )
    w.bind(runtime)
    w.start_watching(str(db))
    try:
        time.sleep(0.4)  # initial snapshot seeds row 5 (never emitted)
        _mutate(str(db), "insert", None, "fresh")
        time.sleep(0.3)
        _mutate(str(db), "update", 5, "changed")  # pre-existing -> update path
        time.sleep(0.3)
        _mutate(str(db), "delete", 5, None)
        time.sleep(0.3)
        assert wait_for(lambda: ("inserted", 6) in seen), seen
        assert wait_for(lambda: ("updated", 5) in seen), seen
        assert wait_for(lambda: ("deleted", 5) in seen), seen
    finally:
        w.stop_watching()

    col = runtime[KEYED_STREAM]
    agg5 = col.get(sqlite_aggregate_key("t", 5))
    assert agg5.state.operation == "deleted"
    assert agg5.revision >= 1  # updated + deleted appends
    assert col.get(sqlite_aggregate_key("t", 6)).revision == 0
    store.close()
```

### 5.4 README — one subsection under `SQLiteEventBase` in "Watchers"

```markdown
### SQLiteEventBase — keyed aggregates (opt-in)

By default every row event is a fresh aggregate (revision 0). Set
`key_aggregates=True` to give each row a durable revision history: inserts
`create`, updates and deletes `replace` on a stable aggregate id
(`uuid5(NAMESPACE_URL, "observantic:sqlite:{table}:{row_id}")`), so
`collection.history(aggregate_key(table, row_id))` returns the row's full
lifecycle (insert → update → … → delete tombstone).

```python
sync = DatabaseSync(stream=rows, auto_persist=True, key_aggregates=True)
```
```

### 5.5 Gate

```bash
devenv shell "uv run pytest tests/test_keyed_aggregates.py -q"   # 8 passed
devenv shell "uv run pytest -q"                                   # 96 passed, 4 skipped
devenv shell "uv run ruff check . && uv run ruff format --check ."
devenv shell "uv run mypy src/observantic"
```

Commit:

```bash
git add src/observantic/_eventic.py src/observantic/monitors/sqlite.py \
        tests/test_keyed_aggregates.py README.md
git commit -m "feat(monitors): key_aggregates for SQLiteEventBase — per-row revision history

key_aggregates=True maps each SQLite row to a stable aggregate
(uuid5(NAMESPACE_URL, 'observantic:sqlite:{table}:{row_id}')): inserts
create, updates/deletes replace on the head, giving every row a durable
history (insert -> update -> ... -> delete tombstone). Handles rowid reuse
and pre-existing rows never emitted (first snapshot). Default False keeps
one-create-per-event behavior."
```

---

## Step 6 — Fix the `start` console script

`[project.scripts] start = "examples.webhook_server:main"` calls the typer
*function* directly, so the `typer.Option` defaults are never applied
(`AttributeError: 'OptionInfo' object has no attribute 'split'`). Point the
entry point at the typer app instead (validated: `app()` renders `--help`,
exit 0).

**File: `pyproject.toml`** — one line:

```toml
[project.scripts]
start = "examples.webhook_server:app"
```

Re-sync so uv regenerates the entry point, then verify:

```bash
devenv shell "uv sync --group dev"
devenv shell "uv run start --help"    # Usage: start [OPTIONS] ... exit code 0
```

Commit:

```bash
git add pyproject.toml
git commit -m "fix(scripts): point the start entry point at the typer app

Calling main() directly never applies typer.Option defaults
('OptionInfo' has no 'split'); app() is the real CLI entry point."
```

---

## Step 7 — Full gate, merge, release 0.3.0

### 7.1 Full quality gate (repeat the whole suite)

```bash
cd ~/Documents/Projects/observantic
devenv shell "uv run ruff format --check ."
devenv shell "uv run ruff check ."
devenv shell "uv run mypy src/observantic"
devenv shell "uv run pytest -q"        # 96 passed, 4 skipped
```

### 7.2 End-to-end CLI + outbox smoke (unchanged from the 003 guide)

```bash
devenv shell "uv run eventic --version"                 # eventic 1.1.1
devenv shell "uv run eventic --app examples.demo_app:app --url sqlite:///demo.db schema upgrade"
devenv shell "uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect"   # files/sqlite/webhooks
devenv shell "uv run eventic --app examples.demo_app:app --url sqlite:///demo.db verify"
rm -f demo.db
```

### 7.3 Merge and tag

```bash
git checkout main
git merge feature/eventic-v1-followup     # or open a PR and merge it
git push origin main
git tag v0.3.0
git push origin v0.3.0
git branch -d align/eventic-v1.1.0 feature/eventic-v1-followup   # after merge
```

### 7.4 Release notes (GitHub release for v0.3.0)

```markdown
## 0.3.0 — align with eventic 1.1.x (BREAKING)

Observantic 0.3.0 aligns with eventic 1.1.0/1.1.1 — a complete rewrite
(declaration-based App/Stream/Subscription, store-bound Collections with
compare-and-swap, SQLite + Postgres backends, transactional outbox worker,
eventic CLI).

**Breaking:**
- `Record`-based watchers are gone — watchers declare a `stream` and emit
  plain pydantic state; persistence is explicit via `bind(runtime)` +
  `auto_persist` (`persist_strict=True` turns the unbound warning into a
  `ConfigurationException`).
- `init()` / `reset()` / `is_eventic_ready()` (the 0.1.5 global-singleton
  API) are removed — use `make_store(url)` / `build_app(...)` /
  `watcher.bind(app.bind(store))`.
- eventic 0.1.5 data is **not readable**: 1.1.x uses a new append-only
  schema (`eventic_revision` / `eventic_head` / `eventic_intent` /
  `eventic_schema`) — re-ingest (greenfield).

**Added:**
- Default streams: `FILE_STREAM` (files), `SQLITE_STREAM` (sqlite),
  `WEBHOOK_STREAM` (webhooks).
- `SQLiteEventBase.key_aggregates=True` — per-row revision history
  (inserts -> create; updates/deletes -> replace on a stable aggregate id).
- `tests/test_postgres_integration.py` — `TEST_DATABASE_URL`-gated Postgres
  coverage through the public API.
- eventic pinned to v1.1.2, which ships `alembic.ini` so `eventic schema
  upgrade` works out of the box (v1.1.0's wheel could not).

**Fixed:**
- `start` entry point now runs the typer CLI (`examples.webhook_server:app`).
- devenv Postgres service starts reliably (stale socket-dir hardcode
  removed); docs use the `postgresql+psycopg://` scheme (eventic[postgres]
  ships psycopg 3, not psycopg2).
```

---

## Appendix A — Environment notes

- **psycopg driver**: `eventic[postgres]` installs `psycopg[binary]` (psycopg
  3). SQLAlchemy's plain `postgresql://` scheme selects the psycopg2 driver
  → `ModuleNotFoundError: No module named 'psycopg2'`. Always use
  `postgresql+psycopg://` for Postgres URLs (eventic's `Postgres(url)`
  passes the URL through to `create_engine`).
- **devenv Postgres**: `services.postgres` needs a writable
  `unix_socket_directories` (Step 3); the Nix-built Postgres default
  (`/run/postgresql`) does not exist. If port 5432 is occupied, change
  `port` in `devenv.nix` and the `TEST_DATABASE_URL` together.
- **eventic `create(state, id=key)` on an existing aggregate**: identical
  content → replay no-op; different content → `RevisionConflict`. This is
  the contract `persist_row` relies on for rowid reuse.
- **`postgresql://` vs `sqlite://` in `make_store`**: the seam dispatches on
  `url.startswith("postgresql")` / `url.startswith("sqlite")`, so the
  `+psycopg` driver token is handled transparently.

## Appendix B — Verification checklist

- [ ] `main` contains the 003 alignment merge (`4e50cc0`); full gate green
- [ ] eventic `v1.1.2` exists on the remote and ships `alembic.ini`
- [ ] `uv.lock` pins eventic `v1.1.2`; `import eventic` prints `1.1.1`
- [ ] `eventic schema upgrade` works with **no shim** (SQLite; Postgres too)
- [ ] devenv Postgres starts; `Postgres("postgresql+psycopg://postgres:postgres@127.0.0.1:<port>/eventic")` connects
- [ ] `TEST_DATABASE_URL=... uv run pytest tests/test_postgres_integration.py -v` → 4 passed
- [ ] `uv run pytest -q` → 96 passed, 4 skipped
- [ ] `uv run ruff check . && uv run ruff format --check .` → clean
- [ ] `uv run mypy src/observantic` → clean
- [ ] `uv run start --help` renders the typer CLI
- [ ] keyed aggregates: insert/update/delete produce one aggregate with history `[0, 1, 2]`; rowid reuse and pre-existing rows don't error
- [ ] `git log`/diff on `feature/eventic-v1-followup` contains no stray artifacts (`demo.db`, `smoke.db`, `*.db-wal`)
- [ ] tagged `v0.3.0`; release notes state the breaking changes
