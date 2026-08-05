# Observantic — Reimplementation Proposal (Phase 3)

Companion to `review.md` (findings R-01…R-18). This is the target architecture, argued
and verified, with a phase-by-phase plan that keeps the tree green at every step.

---

## 1. Target layout (file-by-file)

```
pyproject.toml
README.md
src/observantic/
├── __init__.py            # public API + __version__; re-exports Watcher, monitors,
│                          #   init/reset/is_eventic_ready, settings, exceptions
├── _eventic.py            # THE SEAM — slimmed: init_eventic / reset_eventic / is_ready
│                          #   / is_record_class / persist / persist_available
├── config.py              # Settings dataclass, snapshot at import (env, no deps)
├── exceptions.py          # ObservanticException / WatcherException / ConfigurationException
├── core/
│   ├── __init__.py
│   ├── base.py            # Watcher: lifecycle + hook registry + emission (plain class)
│   └── events.py          # FileEvent / DatabaseRow / SchemaChange / WebhookEvent payloads
└── monitors/
    ├── __init__.py        # FileWatcher / SQLiteWatcher / WebhookWatcher
    ├── file.py
    ├── sqlite.py
    └── webhook.py
examples/                  # moved out of the wheel; plain scripts (no # /// git headers)
tests/                     # existing files kept where they still apply, others rewritten
```

**Public API sketch (signatures, not prose):**

```python
# observantic/__init__.py
__version__ = "1.0.0"
__all__ = ["Watcher", "FileWatcher", "SQLiteWatcher", "WebhookWatcher",
           "FileEvent", "DatabaseRow", "SchemaChange", "WebhookEvent",
           "settings", "init", "reset", "is_eventic_ready",
           "ObservanticException", "WatcherException", "ConfigurationException"]


class Watcher:
    """Plain base. No pydantic, no Record. Subclass for hook overrides, or use on()."""
    def __init__(self, *, record_model: type | None = None,
                 persist_required: bool = False, **config) -> None: ...
    # -- lifecycle ------------------------------------------------------ #
    def start_watching(self, *args, **kwargs) -> None: ...   # validate → flip → impl
    def stop_watching(self) -> None: ...                     # idempotent, bounded, deadlock-free
    @property
    def watching(self) -> bool: ...
    # -- hooks ----------------------------------------------------------- #
    def on(self, event_name: str, callback: Callable) -> None: ...     # register
    def off(self, event_name: str, callback: Callable) -> None: ...    # unregister
    def _dispatch(self, event_name: str, event: Any) -> Exception | None:
        """Runs override method (if any) then registered callbacks. NEVER raises.
        Returns the last error (thread-safe by construction — no instance state)."""
    # -- emission / persistence ------------------------------------------ #
    def _emit(self, **fields) -> Any:
        """record_model(**fields). Persistence is implicit (Record construction
        persists v0 when a store is wired) and *honest*: nothing else happens."""
    # -- subclass extension points (no-ops by default) ------------------- #
    def _validate_start(self, *args) -> None: ...
    def _start_impl(self, *args, **kwargs) -> None: ...
    def _stop_impl(self) -> None: ...
    def on_error(self, error: Exception, event: Any = None) -> None: ...


class FileWatcher(Watcher):
    def __init__(self, *, watch_patterns: list[str] | None = None,
                 ignore_patterns: list[str] | None = None,
                 case_sensitive: bool = True,
                 event_throttle_seconds: float = 0.1,
                 record_model: type | None = FileEvent, **kw) -> None: ...
    def start_watching(self, path: str | Path, *, recursive: bool = True) -> None: ...
    # hooks: on_file_created / on_file_modified / on_file_deleted / on_file_moved
    #        + lifecycle on_start / on_stop / on_error
    # (events are FileEvent payloads — typed, uniform, persistable)


class SQLiteWatcher(Watcher):
    def __init__(self, *, poll_interval_seconds: float = 1.0,
                 track_schema_changes: bool = True,
                 db_connect_timeout_seconds: float = 5.0,
                 max_table_rows: int = 100_000,
                 record_model: type | None = DatabaseRow, **kw) -> None: ...
    def start_watching(self, db_path: str | Path) -> None: ...
    # hooks: on_row_inserted / on_row_updated / on_row_deleted / on_schema_changed


class WebhookWatcher(Watcher):
    def __init__(self, *, port: int = 8080, host: str = "0.0.0.0",
                 webhook_paths: list[str] | None = None,
                 require_auth_header: str | None = None,
                 require_auth_value: str | None = None,
                 parse_json_body: bool = True,
                 max_body_bytes: int = 1_048_576,
                 allowed_methods: list[str] | None = None,
                 redact_headers: bool = True,        # strip the auth header from payloads
                 record_model: type | None = WebhookEvent, **kw) -> None: ...
    def start_watching(self) -> None: ...
    # hook: on_webhook_received(WebhookEvent)


# payloads (one type per source; the SAME object goes to hooks and to the store)
class FileEvent:      path, event_type, is_directory, dest_path, timestamp
class DatabaseRow:    table_name, row_data, row_id, operation, timestamp
class SchemaChange:   tables_added, tables_dropped, tables_modified, timestamp
class WebhookEvent:   path, method, headers, body, query_params, timestamp, source_ip
# all frozen dataclasses (or plain dataclasses) — pydantic only where validation earns it
```

**Naming note.** `start_watching` / `stop_watching` / `on_error` / `on_start` /
`on_stop` keep their current names so existing overrides and call sites migrate
mechanically; only the *base* and *construction* change (see §5).

---

## 2. Core data flow

```
        source                          watcher core                    user code             sink
┌──────────────────────┐   ┌───────────────────────────────────────┐   ┌──────────────┐   ┌────────────┐
│ watchdog observer    │──▶│ FileWatcher._on_file_created(event)   │──▶│ on_file_*    │   │            │
│ sqlite poll thread   │──▶│ SQLiteWatcher._check_for_changes()    │──▶│ on_row_*     │──▶│ on_error   │
│   (+ watchdog touch) │   │   diff under lock → dispatch OUTSIDE  │   │ on_schema_*  │   │ (never dies)│
│ HTTP handler threads │──▶│ WebhookWatcher._handle(event)         │──▶│ on_webhook_* │   └────────────┘
└──────────────────────┘   │   err = _dispatch(name, event)        │   └──────────────┘
                           │   → 500 if err else 200               │
                           │ _emit(**fields) ──▶ record_model(**fields)              ┌────────────┐
                           │   │ Record? + store wired? ──▶ v0 persists + @on.create │ eventic    │
                           │   └────────────────────────────────────────────────────▶│ store      │
                           └────────────────────────────────────────────────────────┴────────────┘
```

Invariants enforced at the boundary:
- **Dispatch never runs while holding a source-internal lock** (kills R-06).
- **`_dispatch` returns the error** instead of stashing it on `self` (kills R-04).
- **Handlers check `watching` before emitting** (kills R-07).
- **`_emit` never raises in a source thread** — validation happens at `start_watching`
  (kills R-03).
- **Record construction is the ONLY persistence path** — honest, documented, no knobs
  that pretend to control it (kills R-01/R-02).

---

## 3. Design decisions

### D1 — Plain-class `Watcher` base; no pydantic `BaseModel`, no `Record` inheritance

**Decision.** `Watcher` is a plain class. Config comes from explicit `__init__`
kwargs (merged over annotated class attributes for subclass convenience); runtime
state is plain instance attributes; hook resolution is `getattr(self, name, None)`.

**Rationale.** Every R-15/R-16/R-17 defect traces to pydantic-as-base. A plain base
removes the annotation tax, the schema pollution, the required-field trap, the
Record-MRO merge, and the need for `call_unwrapped`/`dispatch_direct` (R-10) — the
latter exists *only* to undo what the pydantic+Record metaclasses do. Config
validation moves to `__init__` and `_validate_start` using the existing typed
exceptions; the config surface is small (≤10 fields per monitor) and gains nothing
from a full model layer.

**Rejected.** (a) Keep pydantic fields — preserves the entire R-15/16/17 cluster.
(b) `dataclasses` base — fine, but adds nothing over a plain class with a documented
`__init__` and makes subclass config-merging (`class` attrs + kwargs) awkward.
(c) A declarative config object passed to a stateless watcher — over-engineering for
this surface.

### D2 — Keep the subclass-with-overrides UX and the callback registry; drop the record conflation

**Decision.** Users still write
`class MyWatcher(FileWatcher): def on_file_created(self, event): ...` or
`watcher.on("on_file_created", fn)`. The event payload class is *separate* from the
watcher: `record_model=FileEvent` (constructor or class attribute). For Record
persistence the user writes one more small class:

```python
class MyFileEvent(Record):
    path: str = ""
    event_type: str = ""

watcher = FileWatcher(watch_patterns=["*.pdf"], record_model=MyFileEvent)
watcher.on("on_file_created", cb)
watcher.start_watching("/documents")
```

**Rationale.** Workflows 1 and 2 both get a first-class shape; the watcher stops
*being* the record, so schema and config stop cross-contaminating (R-15/R-16), and
Eventic's same-name constraint (R-13) stops biting observantic users (only *their*
record classes are keyed).

**Rejected.** (a) Keep `(Record, FileEventBase)` as a supported pattern — it is the
conflation we are removing; a plain-class base makes the MRO merge fail loudly at
class definition (good: fail fast). (b) An async framework or declarative DSL — no
user value, large rewrite. (c) A "strategy" object per source without subclassing —
the subclass surface is already the smallest thing that serves workflows 1–3.

### D3 — Honest persistence: construction-time durable v0 is the contract; one up-front knob

**Decision.** Persistence happens **iff** `record_model` is a `Record` subclass and a
store is wired (Eventic `init()`). That's eventic 0.1.5 semantics, documented as-is.
The only knob is `persist_required: bool = False`: at `start_watching`, if
`persist_required=True` and `record_model` is a Record but Eventic is not ready →
`ConfigurationException` before any thread starts (fail fast, typed). If
`persist_required=False`, a missing backend degrades to a single documented startup
warning, and events flow without persistence.

**Rationale.** R-01/R-02/R-03 show the current knobs fight the platform: `auto_persist`
is redundant (construction already persists) or impossible (plain models). The target
has exactly one honest question: *is persistence required?* — asked once, up front.
Nothing else.

**Rejected.** (a) `auto_persist` reworked to "construct without persisting, append
only if asked" — fights eventic's durable-v0 construction semantics and would need
another seam function to suppress the v0 row (not exposed by eventic 0.1.5; contract
forbids changing eventic). (b) Removing Eventic entirely — the README promise is
"to Eventic Records"; the seam stays. (c) Auto-detecting readiness per event — that
is exactly the current silent divergence.

### D4 — Threading: dispatch outside locks; return-value errors; a real stop protocol

**Decision.**
1. Source threads produce *immutable event payloads*; `_dispatch` is the only shared
   execution path and holds no source lock.
2. SQLite: compute the diff under `_check_lock`, then dispatch rows **outside** the
   lock (batching deletes/updates so ordering per check is preserved).
3. `_dispatch` returns the last error; the webhook 500 decision reads its own
   dispatch's return value.
4. Stop protocol: `stop_watching` sets `_stopping=True` under the watcher lock
   (with a re-check — kills R-11's TOCTOU on start too), then stops sources; every
   handler checks `self.watching` before emit/dispatch (kills R-07); joins are
   bounded and never self-joined (a `_stop_impl` guard: if called from a source
   thread, mark-and-return, letting the source's own loop observe the flag).
5. Webhook connection tracking becomes per-instance and tracks sockets for the
   handler's lifetime (override `process_request_thread` or add/remove in `handle`),
   so `close_all_connections` can actually unblock wedged handlers (kills R-05).

**Rationale.** The deadlock (R-06), the race (R-04), the TOCTOU (R-11) and the async
stop (R-07) all trace to locks or shared state held across user code. Moving user
code out of the lock region and out of shared state makes each source's threading
local again. The stop-from-hook case becomes: the flag flips, the source's own loop
sees it on the next iteration, no thread joins itself, no deadlock.

**Rejected.** (a) A single global dispatcher queue (all sources → one consumer) —
serializes independent sources and adds a queue-lifetime problem for no benefit.
(b) Lock-free per-source dispatch — the sqlite poll+watchdog pair legitimately needs
the diff lock; the fix is *scope*, not removal.

### D5 — Webhook protocol correctness

**Decision.** Every rejection path (400/401/404/405/413) either drains the request
body (bounded) or sets `Connection: close` before responding (kills R-18). Chunked
`Transfer-Encoding` is decoded (bounded by `max_body_bytes`) — or, if we defer that,
returned `501` explicitly rather than silently dropped (kills R-08). The configured
auth header is stripped from the delivered `headers` (kills R-09); `redact_headers`
defaults to True.

**Rationale.** Verified framing corruption (R-18) is a protocol bug, not a style
question; `Connection: close` on rejection is the minimal, safe fix (draining is
optional perf sugar). Chunked bodies are common enough that silently dropping them is
unacceptable; explicit 501 is the honest fallback.

### D6 — The seam, slimmed

**Decision.** `_eventic.py` keeps `init_eventic`, `reset_eventic`, `is_ready`,
`is_record_class`, `persist` (append; raise `EventicNotReadyError` when not ready).
Adds `persist_available() -> bool` (the only readiness check `persist_required`
needs). Deletes `call_unwrapped`, `can_persist`, `is_launched`, `EventicNotReadyError`
re-export remains internal.

**Rationale.** The seam discipline is one of the library's genuinely good ideas
(review §4.3); it stays. `call_unwrapped` is obsolete once watchers aren't Records;
`is_launched` reads DBOS privates (R-14); `can_persist` is dead.

### D7 — Config and packaging

**Decision.** `config.py` becomes a frozen `Settings` dataclass read once at import
from the documented env vars (`OBSERVANTIC_DB_URL`/`DB_URL`, `OBSERVANTIC_LOG_LEVEL`/
`LOG_LEVEL`, prefixed wins). Drop `confidantic` and `python-dotenv`. Make `eventic`
an **optional** dependency (`[project.optional-dependencies] eventic = ["eventic"]`)
— verified: no module-level `eventic` import exists in `src/`, so the core works
without it. Move `src/examples` → `examples/` outside the wheel; drop the fake
`start` console script; keep `watchdog` as a hard dependency (both file and sqlite
monitors use it).

**Rationale.** R-removals 4/9/10/11/12 are verified dead or misleading; making
eventic optional is the single biggest install-size/UX win for workflow-2 users.

### D8 — Payloads

**Decision.** One event type per source (`FileEvent`, `DatabaseRow` + `SchemaChange`,
`WebhookEvent`) used both as the hook payload and the default `record_model` — frozen
dataclasses with `extra`-like strictness by construction (no dynamic fields). Drops
`WebhookRecord` (R-removal 7) and the `on_data_changed` legacy hook (R-removal 8,
documented in migration).

**Rationale.** The current `FileRecord`/`WebhookRecord` are never handed to hooks —
the hook gets a raw watchdog event or a dataclass — so the record and the event are
two representations of the same data. One type ends the duplication and makes hooks
testable against exactly what gets persisted.

---

## 4. What changes for users (migration notes)

| Today | Target | Migration |
|---|---|---|
| `class X(Record, FileEventBase)` — watcher IS the record | `class X(Record)` + `record_model=X` on a `FileWatcher` subclass | Split the record class out; watcher no longer inherits `Record`. Config stays as annotated class attrs or constructor kwargs. |
| `watcher = FileEventBase()` — config via kwargs | `FileWatcher(...)` | Rename; kwargs unchanged in spirit. |
| `auto_persist=True` | delete the kwarg; persistence is implicit for Record `record_model` + `init()` | If you relied on `auto_persist` to *delay* persistence — you couldn't; it was redundant. Set `persist_required=True` to fail fast instead. |
| `persist_strict=True` | `persist_required=True` | Same intent, up-front, typed. |
| `dispatch_direct=False` | remove | It never did what the README said (R-10). |
| `register_hook("on_x", fn)` | `on("on_x", fn)` | Mechanical rename (alias kept for one release if desired). |
| `on_data_changed(db_path, rows)` | per-row hooks only | Rewrite to `on_row_inserted`; see README "backward compatibility" note removed. |
| Webhook hook receives `WebhookEvent` with `headers` incl. auth | same, minus the auth header by default | No action; `redact_headers=False` restores old behavior. |
| `from observantic import EventWatcher` | `from observantic import Watcher` | Rename. |
| Examples via `# /// script` git headers | plain scripts + `uv run` | No git dependency. |

**Breaking but justified:** watcher-not-a-Record (R-15/16), persistence honesty
(R-01/02/03), return-value dispatch (R-04), `start` entry point removal (R-removal
11). The breaking surface is small and the migration is mechanical (above table).

---

## 5. Phased implementation plan

Each phase ends green. Verification commands are runnable in the devenv.

### Phase A — Foundation

**A1. Baseline + packaging** — `pyproject.toml`: drop `confidantic`/`python-dotenv`,
add `[project.optional-dependencies] eventic`, remove `[project.scripts] start`,
wheel packages = `["src/observantic"]`, move examples out of `src/` (tests reference
`examples.*` — update `test_public_api.py`). Verify: `devenv shell uv run pytest -q`
(green), `devenv shell uv run ruff check .`.

**A2. Config** — `config.py` → frozen dataclass; keep `settings`, `DB_URL`, `LOG_LEVEL`
contracts; delete confidantic comments. Verify: `test_config.py` passes (rewrite the
two monkeypatch tests to the dataclass API if needed).

**A3. Core `Watcher` (plain base)** — new `core/base.py` (keep `EventWatcher` name
importable as a deprecated alias one release, or rename now — decision: rename to
`Watcher`, alias `EventWatcher = Watcher`). Implement: `__init__` config merge
(class attrs + kwargs), lifecycle with locked double-checked start (fixes R-11),
stop protocol (no self-join, `watching` flag, no post-stop dispatch — fixes R-07),
`on`/`off`/`_dispatch` returning the last error (fixes R-04), `_emit` that never
raises in source threads (fixes R-03). Keep `_eventic.py` seam as-is for now.
Verify: new `tests/test_watcher.py` (state machine, dispatch, stop-from-hook,
concurrent start); old `test_core_state.py`/`test_core_dispatch.py` rewritten against
the plain base; `uv run pytest -q`.

**A4. Seam slim** — delete `call_unwrapped`/`can_persist`/`is_launched`; add
`persist_available()`. Verify: `test_core_eventic.py` updated (unwrap tests deleted;
init/reset/persist tests kept); green.

### Phase B — Monitors

**B1. FileWatcher** — port file.py to the plain base; `FileEvent` payload (frozen
dataclass) used for both hooks and `record_model`; throttle map bounded (keep);
stop protocol (no late events). New tests: post-stop no-dispatch, stop-from-hook
(file path: bounded, no self-join error), thread-count leak over 10 start/stop
cycles, throttle-map boundedness under churn. Verify: `uv run pytest tests/test_file_monitor.py -q`.

**B2. SQLiteWatcher** — diff under `_check_lock`, dispatch **outside** the lock
(fixes R-06); wrap `_refresh_snapshot` in the typed-error path (fixes R-12);
stop-from-hook supported (flag-based); keep snapshot/schema/max-rows behavior.
New tests: stop-from-hook completes in <1 s and no threads leak; concurrent
poll+watchdog dispatch preserves exactly-once; random row-op property test
(50 random insert/update/delete sequences → every change reported exactly once).
Verify: `uv run pytest tests/test_sqlite_monitor.py -q`.

**B3. WebhookWatcher** — port to plain base; per-instance connection tracking with
handler-lifetime sockets (fixes R-05); rejection paths set `Connection: close` or
drain (fixes R-18); chunked bodies decoded or explicit 501 (fixes R-08); auth header
redaction (fixes R-09); 500 from `_dispatch` return value (fixes R-04).
New tests: keep-alive reuse after 413/401 (next request intact), chunked JSON event,
concurrent ok/fail pairs always answer correctly (20 pairs × 5 runs), handler-thread
leak after wedged clients + stop, two servers in one process don't cross-talk.
Verify: `uv run pytest tests/test_webhook_monitor.py -q`.

### Phase C — Consumers

**C1. Examples** — rewrite the four examples to the new API; move to `examples/`;
no `# /// script` git headers. Verify: `uv run python examples/example_file.py` runs
(short-lived), `test_public_api.test_examples_importable` updated.

**C2. Docs** — rewrite README sentence-by-sentence against the new behavior table
(review §6): persistence section = durable-v0 + `persist_required`; webhook section =
rejection semantics, chunked support, redaction; remove `dispatch_direct` and
"opt-in persistence" claims.

### Phase D — Hardening & done

**D1. Full suite audit** — every test: survive / rewrite / new per §6 matrix;
add the missing concurrency + leak tests at the library level (not just per-monitor).
Verify: `devenv shell uv run pytest -q` green; `devenv shell uv run ruff check .`;
`devenv shell uv run mypy src/observantic`; `devenv shell uv build` produces a wheel
with only intended files; `uv run python -c "import observantic"` works with eventic
uninstalled (optional-dep check).

---

## 6. Test matrix (survive / rewrite / new)

**Survive as-is (adjusted for renames):** `test_public_api.py` (minus
examples-in-wheel), `test_config.py` (dataclass API), `test_core_eventic.py`
(init/reset/persist/sqlite-backend), most `test_file_monitor.py` integration tests
(events, patterns, throttle, directory-ignore, observer-survives), most
`test_sqlite_monitor.py` (diff correctness, schema, restart, locked-DB-survives,
poll-without-file-events), most `test_webhook_monitor.py` (200/400/401/404/405/413,
JSON, auth, wedged-client, disconnect).

**Rewritten:** `test_core_state.py` / `test_core_dispatch.py` → `test_watcher.py`
against the plain base (drop `_dispatch_hook`/`_watching` white-box coupling where a
black-box equivalent exists; keep a small white-box layer for the state machine);
`test_file_monitor.test_throttle_map_pruned` and other private-attr tests →
behavioral; examples-importable test → examples moved.

**New:**
- *Concurrency (one per monitor):* file — two concurrent `start_watching` → exactly
  one observer; sqlite — stop-from-hook completes <1 s, deadlock-free, exactly-once
  per row under poll+watchdog interleave; webhook — 20 concurrent ok/fail pairs,
  100% correct statuses (5 runs).
- *Resource leak (one per monitor):* N=10 start/stop cycles → `threading.active_count`
  returns to baseline within a small tolerance; throttle map bounded under churn
  (file); handler threads reaped after wedged clients + stop (webhook); sqlite
  connection count flat after locked-DB error cycles.
- *Protocol:* keep-alive reuse after 413/401; chunked request; oversized header;
  multi-server isolation.
- *Persistence honesty:* `persist_required=True` without init raises typed error at
  start (not per event); Record watcher + init + `persist_required=False` → rows
  still written (documented platform behavior — a *contract* test, not a bug test);
  plain `record_model` + `persist_required=True` → start-time ConfigurationException.
- *Property:* sqlite random-op diff correctness; webhook fuzz (malformed
  Content-Length/headers/body → 4xx, server stays up).

---

## 7. Definition of done

1. All invariants in review §1.4 hold **with tests that prove them**: observer
   survival, exactly-once dispatch, fail-fast typed start, bounded deadlock-free
   stop (including from inside hooks) with no events after stop, honest persistence,
   bounded resources (thread-count flat over cycles).
2. Every finding R-01…R-18 has a passing test that fails on the current code
   (the repros become regression tests).
3. `devenv shell uv run pytest -q` green; `ruff check` + `ruff format --check` +
   `mypy src/observantic` clean; `uv build` produces the intended wheel.
4. README matches behavior sentence-by-sentence (the review §6 table is the checklist).
5. No dead code: `rg` shows no unused imports, no unused public functions, no
   unused dependencies in pyproject.
6. `import observantic` and the core watchers work with `eventic` uninstalled;
   Eventic features work with `pip install observantic[eventic]`.
