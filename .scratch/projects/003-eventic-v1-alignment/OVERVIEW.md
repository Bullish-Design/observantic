# Observantic × eventic 1.1.0 — Full Alignment Overview

Companion to `.scratch/projects/002-first-principles-review/` (the previous
architecture). This document maps every place observantic currently depends on
eventic 0.1.5 and specifies what must change to align with eventic **1.1.0**
(the rewritten library at `/home/andrew/Documents/Projects/eventic`, tag
`v1.1.0`, commit `b489da2`).

The eventic 1.1.0 library was verified healthy: unit + property suites pass
(132 tests), the conformance suite is the store spec, and every README code
block executes as a doctest.

---

## 1. Executive summary

eventic 1.1.0 is a **complete rewrite**, not an upgrade. **Nothing** from the
0.1.5 public surface survives: `Record`, `Eventic` singleton, `init_eventic`,
`@on.*` decorators, `PropertiesBase`, DBOS, `queues.dispatcher.evented` — all
gone. In their place: pure declarations (`App`, `Stream`, `Subscription`),
explicit store-bound writes through `Runtime`/`Collection` with
compare-and-swap, two backends (`SQLite`, `Postgres`), a transactional outbox
worker, a `StoreAdmin` (migrate / check / rebuild / verify), and an
`eventic` CLI.

Aligning observantic means:

1. **Bump the dependency** to eventic `v1.1.0` and re-resolve `uv.lock`
   (also drop the now-dead `confidantic` / `python-dotenv` deps).
2. **Rewrite the eventic seam** (`src/observantic/_eventic.py`) — it is the
   only module allowed to import eventic internals; today it wraps
   `Record`/`Eventic`/queues, all of which are gone.
3. **Redesign persistence in `EventWatcher`** — `_emit()` builds a plain
   `BaseModel` state (the stream model), and writes go through a bound
   `Collection` (`create`/`change`/`replace`), never an implicit global.
4. **Remove the global singleton API** (`init` / `reset` / `is_eventic_ready`).
5. **Rewrite examples, the README persistence story, and the eventic-facing
   tests** to the declaration + `bind(store)` model.
6. **Bump observantic to 0.3.0** (breaking change).

There is **no data migration path**: eventic 1.1.0 uses a new physical schema
(`eventic_revision` / `eventic_head` / `eventic_intent` / `eventic_schema`),
new revision identity (`uuid5` over `stream:aggregate_id:revision`), and no
longer touches DBOS tables. Anything persisted by observantic 0.2.0 against
eventic 0.1.5 is unreadable by 1.1.0 — treat it as greenfield.

---

## 2. What eventic became (deltas that matter to observantic)

### 2.1 API surface: removed vs. added

| eventic 0.1.5 (current observantic target) | eventic 1.1.0 (new target) |
|---|---|
| `eventic.Record` (subclass your model) | plain `pydantic.BaseModel` + `Stream(T, name=...)` |
| `eventic.Eventic.init(name, database_url)` global singleton | `App(id=..., streams=[...], subscriptions=[...])` — a frozen value |
| `Eventic.instance()`, `Eventic.reset()` | `runtime = app.bind(store)` — no global, no reset |
| `eventic.init_eventic(engine)` | gone (CLI: `eventic --app m:a --url ...`) |
| `@on.create` / `@on.update` decorators | `Subscription(id, stream, handler, kinds, delivery)` — handlers take `Commit[T, M]` |
| `Record._store.append(record)` | `collection.create(state, id=None, meta=None)` |
| `Record.hydrate(id)`, `record.version` | `collection.get(id[, revision])` → `Revision[T, M]` with `.revision`, `.state`, `.digest` |
| `eventic.queues.dispatcher.evented`, `RecordMeta` wrapping | no metaclass, no decorators, no DBOS queues |
| `eventic.PropertiesBase` | `Meta[M]` (app-level, versioned metadata; `NoMeta` default) |
| DBOS / outbox via `@evented` + launch | `Outbox(queue=...)` delivery + `eventic.worker.Worker` / `eventic worker` CLI |
| `init_eventic` / `bootstrap` / `persistence` / `main` modules | `app.py`, `stream.py`, `subscription.py`, `runtime.py`, `planning.py`, `hydration.py`, `canonical.py`, `evolution.py`, `encodings/`, `sql/`, `worker.py`, `cli/`, `testing/` |
| Postgres-only (psycopg2 + DBOS tables) | `SQLite` (dev/test) and `Postgres` (prod), one conformance suite |

### 2.2 The new write/read surface (what observantic must call)

```python
from pydantic import BaseModel
from eventic import App, Stream, Subscription, Outbox
from eventic.sql import SQLite, Postgres

class FileEvent(BaseModel):
    path: str = ""
    event_type: str = ""

files = Stream(FileEvent, name="files")            # name is the durable identity
app = App(id="obs", streams=[files])               # frozen; validated eagerly
runtime = app.bind(SQLite("obs.db"))               # capability check; opens nothing

col = runtime[files]
r0 = col.create(FileEvent(path="/x"))              # Revision[FileEvent, Any], revision 0
r1 = col.change(r0, event_type="modified")         # CAS on r0.revision -> 1
r1 = col.replace(r0, FileEvent(path="/y"))         # whole-state append
col.get(r0.id)                                     # head
col.get(r0.id, revision=0)                         # exact, from the log
col.history(r0.id)                                 # Page[Revision[...]]
col.where(path="/x")                               # Page[Revision[...]], JSON filters
with runtime.batch() as b:                         # one transaction
    b[files].create(FileEvent(path="/z"))
runtime.admin()                                    # StoreAdmin (migrate/check/rebuild/verify)
```

Key semantics observantic must preserve or adopt:

- **I4 pure declarations** — constructing a `Stream`/`App` does no I/O; a
  watcher can be built and started with no database, exactly like today.
- **I5 explicit, store-bound writes** — the *only* way to persist is a
  `Collection` from a bound `Runtime`. This replaces `auto_persist` +
  global-init semantics; there is no ambient store.
- **I7 loud conflicts** — `change`/`replace` carry `expected_revision`; a
  stale write raises `eventic.errors.RevisionConflict` (not silently
  swallowed). Retry loops must handle this.
- **I9 post-durability dispatch** — inline subscriptions run after `COMMIT`;
  outbox delivery is at-least-once and handlers must be idempotent.
- **Stream name rule** — must match `^[a-z0-9][a-z0-9_.-]{0,63}$`
  (`eventic.ids.validate_stream_name`); model must be a plain `BaseModel`
  (no `RootModel`), no `SecretStr` fields (`Stream.__post_init__`).
- **`App.bind` requires store capabilities** — `Outbox` subscriptions need a
  store with `capabilities.outbox` (`CapabilityUnsupported` otherwise).
  Both `SQLite` and `Postgres` advertise `outbox=True`, so dev parity holds.

### 2.3 The new delivery model (hooks vs. subscriptions)

- **Observantic hooks stay** — `on_file_created`, `register_hook(...)`, etc.
  are in-process, best-effort callbacks. They are *not* eventic
  subscriptions and can exist with no store at all.
- **eventic subscriptions are for delivery** — `Subscription(id=..., stream=...,
  handler=..., kinds={"create","change"}, delivery=Inline() | Outbox(queue=...))`.
  Inline = run in the writing process after COMMIT (best-effort, collected as
  `InlineDispatchError`). Outbox = durable intent row written in the commit
  transaction; drained by `eventic worker` / `eventic.worker.Worker`.
- Aligning means observantic **assembles an `App`** from watcher declarations
  (streams + optional subscriptions) so users get the `eventic` CLI
  (`schema upgrade`, `schema check`, `worker`, `verify`, `inspect`) against
  observantic-generated apps — but observantic itself never runs the worker.

### 2.4 Operations and errors observantic must surface

- CLI: `eventic --app module:attr --url <url>` (or `$EVENTIC_URL`);
  commands `schema upgrade|check`, `heads rebuild`, `verify`, `worker
  [--queue Q] [--once]`, `intents list|redrive`, `inspect`; exit codes
  0/1/2/3 (3 = drift).
- Errors (`eventic.errors`): `EventicError` base; `ConfigError`,
  `RevisionConflict`, `NotFound`, `UsageError`, `InlineDispatchError`,
  `DeadLettered`, `CapabilityUnsupported`, `StoreError`, `DeliveryError`,
  `UndecodableRevision`, `EncodingError`.
- `StoreAdmin` on any store via `runtime.admin()`; `Store.close()` releases
  pools (idempotent).

---

## 3. Target architecture for observantic

The monitoring machinery (watchdog observer, sqlite snapshot diff, threaded
webhook server, hook registry, lifecycle state machine, error funneling) is
**unchanged**. Only the persistence seam changes.

### 3.1 New persistence model in `EventWatcher`

Replace `record_model: type[Record] | None` + global `init()` with:

- `stream: Stream | None` — the stream this watcher emits into. Watchers get
  sensible defaults (below); users may supply their own `Stream` (custom
  model, `schema_version`, `upcasters`).
- `bind(runtime: Runtime)` — stores the runtime and resolves
  `runtime[stream]` to a `Collection`. Rejects a stream not installed in the
  runtime's app (`UsageError`). Idempotent; `unbind()` for teardown.
- `auto_persist: bool` (kept name) — when `True` **and bound**, `_emit()`
  also commits the state.
- `persist_strict: bool` (kept name) — when `True`, `auto_persist` with no
  bound collection raises `ConfigurationException`; otherwise log a warning
  and continue (mirrors today's semantics).
- `_emit(**fields)` builds `model = self.stream.model (or record_model, or
  default model)` and returns the instance. If `auto_persist` and bound:
  `collection.create(state, id=...)` where `id` comes from an `id: UUID`
  field on the model when present, else auto `uuid4()`.

```python
# user code (new pattern)
from pydantic import BaseModel
from eventic import App, Stream
from eventic.sql import SQLite
from observantic import FileEventBase

class FileEvent(BaseModel):
    path: str = ""
    event_type: str = ""

files = Stream(FileEvent, name="files")
app = App(id="docs", streams=[files])
store = SQLite("obs.db")                # or observantic.make_store(settings.DB_URL)
watcher = FileEventBase(stream=files, auto_persist=True)
watcher.bind(app.bind(store))
watcher.start_watching("/documents")
```

### 3.2 Default streams per monitor

Each monitor exposes a module-level default `Stream` (frozen value, no I/O):

| Monitor | Default stream | State model |
|---|---|---|
| `FileEventBase` | `Stream(FileRecord, name="files")` | `FileRecord` |
| `SQLiteEventBase` | `Stream(DatabaseRow, name="sqlite")` | `DatabaseRow` |
| `WebhookEventBase` | `Stream(WebhookRecord, name="webhooks")` | `WebhookRecord` |

All three internal models already satisfy `Stream` requirements (plain
`BaseModel`, no `RootModel`, no `SecretStr`; `WebhookRecord` keeps
`arbitrary_types_allowed=True`). Stream names are durable identities — pick
once, document them.

### 3.3 App assembly helper

Provide `observantic.build_app(id, watchers=(), subscriptions=(), meta=NoMeta)`
(or similar) that collects `watcher.stream` declarations into `App.streams`
and merges user subscriptions. This keeps one module (`_eventic.py`) touching
eventic types and gives users a `module:attr` App for the CLI:

```python
# myapp.py
from eventic import App, Stream
from observantic import FileEventBase, SQLiteEventBase

app = App(id="obs-app", streams=[Stream(FileEvent, name="files"), Stream(DatabaseRow, name="sqlite")])
```

### 3.4 Optional (recommended for a later phase): keyed aggregates

SQLite row events map naturally onto revision history: aggregate id =
`uuid5(namespace, f"{table}:{row_id}")`, inserts → `create`, updates →
`change(base, **fields)` / `replace`, deletes → `replace` with a tombstone
or a delete-marker field. This gives updates/deletes durable revision
history, which 0.1.5 could not express. Keep the 0.2 default
(one `create` per event) for `files`/`webhooks`; offer keyed mode as an
opt-in flag on `SQLiteEventBase` (e.g. `key_aggregates: bool = False`).

---

## 4. File-by-file change plan

### 4.1 `pyproject.toml` + `uv.lock`

- `dependencies`: drop `confidantic` and `python-dotenv` (dead — grep shows
  no imports anywhere; `config.py` only references confidantic in a comment
  explaining why it is *not* used).
- `eventic` source: `eventic = { git = "https://github.com/Bullish-Design/eventic.git", tag = "v1.1.0" }`
  (or `rev = "b489da2"`). Re-run `uv lock` / `uv sync`.
- Add `pydantic>=2.9` as a direct dependency (observantic models use pydantic
  directly; eventic 1.1.0 requires it). `sqlalchemy>=2.0.43` arrives via
  eventic.
- Decide on extras: `eventic[postgres]` (psycopg) if the Postgres backend
  stays in scope; `eventic[migrate]` (alembic) if observantic's own docs/CI
  run `eventic schema upgrade`. Dev group can add `eventic[migrate]` for
  integration tests.
- `version = "0.3.0"` (breaking).
- `[project.scripts] start = "examples.webhook_server:main"` — keep; the
  module is rewritten in place.
- Watch `[tool.hatch.build.targets.wheel]` — `src/examples` stays packaged
  (the CLI `--app examples.webhook_server:app` demo relies on it).

### 4.2 `src/observantic/_eventic.py` — the seam (rewrite)

Contract today (all against 0.1.5): `Eventic.init/reset/instance`,
`Record._store.append`, `RecordMeta`/`@evented`, `inspect.unwrap`,
`is_record_class`, `persist`, `can_persist`, `is_launched`.

New seam contract (the only file that imports eventic internals):

- `make_store(url_or_path: str) -> SQLite | Postgres` — mirror of
  `eventic.cli.loader.make_store`; `sqlite://` → `SQLite`, `postgresql://` →
  `Postgres`, anything else → `ConfigurationException`. Handles bare paths
  (`"obs.db"` → `SQLite("obs.db")`).
- Default streams: `file_stream`, `sqlite_stream`, `webhook_stream` (or
  `Stream(FileRecord, name="files")` etc. as module constants).
- `build_app(id, streams=(), subscriptions=(), meta=NoMeta) -> App` — thin
  passthrough (delegates to `eventic.App`) so `core/` never names eventic
  types directly.
- Remove: `EventicNotReadyError` (replaced by `ConfigurationException` at
  the caller), `init_eventic`, `reset_eventic`, `is_ready`, `is_launched`,
  `call_unwrapped`, `is_record_class`, `persist`, `can_persist`.

### 4.3 `src/observantic/core/base.py` — `EventWatcher`

- **Remove** `dispatch_direct`, `call_unwrapped` imports, `is_record_class`,
  `persist`, `EventicNotReadyError`. The new eventic has no metaclass
  wrappers — dispatch is a plain `getattr`, so `_hook_callables` and
  `_safe_call` call methods directly.
- **Keep**: lifecycle state machine (`start_watching`/`stop_watching` with
  H-10 rollback), hook registry (`register_hook`/`unregister_hook`),
  `_dispatch_hook` (never raises; funnels to `on_error`; `raise_on_hook_error`
  collection), `_default_record_model()`.
- **Rework fields**: replace `record_model: type[Any] | None` with
  `stream: Stream | None` (or keep `record_model` as the model override and
  add `stream` — decide below in §7).
- **Add**: `bind(runtime)`, `unbind()`, `_collection: Collection | None`
  private attr, `_runtime: Runtime | None`.
- **Rework `_emit`**: build the state model instance; if `auto_persist` and
  bound → `collection.create(state, id=...)`; if `auto_persist` and unbound →
  `persist_strict` raise / warning (unchanged behavior otherwise).
- `run_async` placeholder: unchanged (still NotImplementedError).

### 4.4 `src/observantic/__init__.py`

- Remove `init`, `reset`, `is_eventic_ready` and the `_eventic` imports that
  back them.
- New exports: watchers, `EventWatcher`, `settings`,
  `make_store`, `build_app`, default streams (`file_stream`,
  `sqlite_stream`, `webhook_stream`), exceptions. `__version__ = "0.3.0"`.
- Do **not** re-export `App`/`Stream`/`Subscription` etc. (recommended):
  users import those from `eventic`; observantic's seam stays the only
  adapter. (Alternative — re-export for convenience — is an open question,
  §7.)

### 4.5 `src/observantic/config.py`

- Keep the import-time env snapshot and the `OBSERVANTIC_*`-wins alias
  behavior; `DB_URL`/`LOG_LEVEL` constants stay (used by the seam default).
- **Decision**: default `DB_URL` — today `"postgresql://localhost/observantic"`.
  With SQLite first-class, recommend `"sqlite:///observantic.db"` so the
  out-of-box experience and tests need no Postgres; the env var still points
  at Postgres in production. (Open question §7.)

### 4.6 `src/observantic/monitors/*` (file / sqlite / webhook)

- No eventic imports today and none needed — the monitors stay as-is except:
  - module-level default streams (§3.2),
  - `record_model` annotations change from "Record subclass" to plain
    `BaseModel` in docstrings/comments,
  - `SQLiteEventBase` optional `key_aggregates` mode (§3.4) — deferrable.

### 4.7 `src/examples/*` (all four)

Every example imports `from eventic import Record` and uses the
`class X(Record, YEventBase)` pattern — rewrite each to the §3.1 pattern:

- `example_file.py` / `example_webhook.py` / `sqlite_example.py` — plain
  `BaseModel` state + `Stream` + `App` + `SQLite("...")` (or
  `make_store(settings.DB_URL)`); watcher `bind`s the runtime.
- `webhook_server.py` (typer CLI, the `start` entry point) — replace
  `observantic.init(name=..., database_url=...)` with
  `store = SQLite(database_url)` (or `Postgres`) and `watcher.bind(app.bind(store))`;
  the `--database-url` option, `_log_file` private-attr handling, signal
  handlers, and `on_error` logging all stay. If the DB is unreachable, bind
  fails → warn and run without persistence (matches today's "server still
  runs" behavior; wrap bind in try/except).
- Update the `# /// script` dependency blocks: `eventic @ git+...` unchanged
  URL, but drop `>=0.1.5` pins.

### 4.8 Tests

- `tests/conftest.py` — delete the autouse `_isolate_eventic` fixture (no
  global state to reset). Add: a `store` fixture (`SQLite(":memory:")`,
  `create_tables=True`, `close()` in teardown) and a `runtime` fixture
  (`App(...).bind(store)`).
- `tests/test_core_eventic.py` — **rewrite**. Remove every `Record`/`@evented`/
  `call_unwrapped`/`hydrate`/`.version` test (imports of
  `eventic.queues.dispatcher` no longer exist). Replace with:
  - `_emit()` builds a plain BaseModel with no store;
  - `bind()` + `auto_persist` commits a `create` revision (assert via
    `collection.get(id).revision == 0`, digest round-trip);
  - `auto_persist` without bind → warning; `persist_strict` → raises;
  - CAS: `change()` on a stale base raises `RevisionConflict`;
  - history: two emits → `history().items` revisions `[0, 1]`;
  - `unbind()`/teardown closes stores cleanly.
- `tests/test_public_api.py` — bump version assertion to `0.3.0`; drop
  `test_is_eventic_ready_callable`; add new-export assertions
  (`make_store`, `build_app`, default streams).
- `tests/test_core_dispatch.py`, `test_core_state.py`, `test_file_monitor.py`,
  `test_sqlite_monitor.py`, `test_webhook_monitor.py`, `test_config.py` —
  mostly unchanged (grep confirms no `Record`/eventic imports beyond the
  seam/tests above). `test_config.py` changes only if the default `DB_URL`
  changes.
- Consider one Postgres-gated test (skip unless `TEST_DATABASE_URL` set)
  exercising `Postgres` against the same helper, mirroring eventic's own
  `tests/conformance/test_postgres.py` pattern.

### 4.9 `README.md`

Rewrite the persistence story:

- Quick Start: `BaseModel` + `Stream` + `App` + `SQLite`/`make_store` +
  `bind` (§3.1), no `Record`, no `init()`.
- "Persistence" section: delete the v0-row/`@on.create`/`auto_persist`
  re-append/DBOS-queue paragraphs; describe explicit
  create/change/replace via a bound `Collection`, CAS conflicts, and
  `auto_persist` as a convenience toggle.
- New sections: declaring subscriptions (`Inline` vs `Outbox`), running the
  `eventic` CLI (`schema upgrade`, `worker --queue`, `verify`, `inspect`),
  schema evolution (`schema_version` + `make_upcaster`, `schema check`
  drift exit code 3), and the SQLite dev backend.
- Remove: DBOS, `@evented`, `launch()`, "Eventic 0.1.5", once-per-process
  init, `TEST_DATABASE_URL` skip notes.

### 4.10 `devenv.nix` / misc

- Keep the Postgres service for `eventic[postgres]` integration work; update
  the `enterTest` comment (tests now run on SQLite by default; Postgres is
  opt-in via `TEST_DATABASE_URL`).
- `.tmuxp.yaml` / `.gitignore`: no change.
- `src/observantic.egg-info/`: regenerated by the build; no manual edit.

---

## 5. Dependency matrix

| Package | 0.2.0 (current) | 0.3.0 (target) | Notes |
|---|---|---|---|
| eventic | git `9a6c2e2` (0.1.5) | git `v1.1.0` | the rewrite |
| pydantic | transitive (via eventic/dbos) | `>=2.9` direct | observantic models use it directly |
| sqlalchemy | — (via dbos) | `>=2.0.43` (via eventic) | not a direct dep |
| confidantic | direct | **remove** | dead (comment-only reference) |
| python-dotenv | direct | **remove** | dead (no imports) |
| watchdog | `>=6.0.0` | keep | |
| psycopg / alembic | via dbos | optional `eventic[postgres]` / `eventic[migrate]` | decide scope |
| dbos | direct (via eventic 0.1.5) | gone | nothing in observantic imports dbos |

---

## 6. Data migration note

No in-place migration exists between eventic 0.1.5 (DBOS-based schema,
`Record`/queue tables) and 1.1.0 (`eventic_revision`/`eventic_head`/
`eventic_intent`/`eventic_schema`). Aligning is a **greenfield switch**:
re-ingest anything persisted under 0.1.5, then operate 1.1.0's `schema
upgrade`/`verify` tooling. Call this out in the README and the 0.3.0 release
notes.

---

## 7. Open decisions for the maintainer

1. **Field shape on `EventWatcher`** — keep `record_model: type[BaseModel]`
   (state model override) *and* add `stream: Stream | None` (declaration),
   or collapse to `stream` only and derive the model from
   `stream.model`/`_default_record_model()`? Recommended: keep both — they
   answer different questions (which *model*, which *durable name/version*).
2. **Default `DB_URL`** — `sqlite:///observantic.db` (recommended) vs keeping
   the Postgres default. Affects `config.py`, `test_config.py`, README, and
   the examples' default store.
3. **eventic extras in observantic's deps** — `eventic[postgres]` in
   `dependencies` vs dev-only vs neither (document-only). The old default was
   Postgres, so production users likely expect the driver present.
4. **Re-export eventic symbols from `observantic`?** Recommended no (seam
   discipline); alternative yes (single import line for users).
5. **Default stream names** — `files`, `sqlite`, `webhooks` as proposed, or
   prefixed (`observantic.files`)? Names are durable identities; decide once.
6. **`auto_persist` naming** — keep for familiarity, or rename to `persist`
   to signal "explicit write"? Keep, and note the semantics change
   (requires `bind()` now; no global).
7. **Keyed aggregates for SQLite rows** (§3.4) — in 0.3.0 or later.
8. **`on_data_changed`** backward-compat hook — keep as-is (unchanged).

---

## 8. Suggested implementation order (keep the tree green)

1. **Dependencies**: edit `pyproject.toml` (deps + version), re-resolve
   `uv.lock` against eventic `v1.1.0`. Run the suite to see the exact breakage
   (expected: seam + core + tests fail).
2. **Seam**: rewrite `_eventic.py` (`make_store`, default streams,
   `build_app`), then `core/base.py` (`bind`/`unbind`, `_emit`, plain
   dispatch) and `__init__.py` (new exports). Keep monitors untouched.
3. **Tests**: rewrite `conftest.py` + `test_core_eventic.py` + `test_public_api.py`;
   run the full suite (now green on SQLite only).
4. **Examples + README**: rewrite the four examples and the README
   persistence/quick-start sections; ensure `test_examples_importable`
   passes.
5. **Config decision** (§7.2) lands with the examples pass.
6. **Docs/ops**: add the `eventic` CLI + outbox/worker + schema-evolution
   sections; update `devenv.nix` comment; release 0.3.0 with the greenfield
   migration note.
