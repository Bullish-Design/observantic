# Observantic — Full Adversarial Code Review

Reviewer: pi (automated adversarial review)
Date: 2026-08-04 — Commit `716687f`

## 0. Executive summary

Observantic is a small (~850 LOC) library whose core promise is: *“bridges external
events to Eventic Records through customizable hooks… Automatic event tracking and
persistence.”* A deep review — including running the test suite against a live
Postgres and driving the watchers and webhook server with real sockets — shows that
**the primary documented workflows do not work**:

1. The README Quick Start and most README/example snippets **crash at class-definition
   time** with a pydantic `PydanticUserError` (`watch_patterns = [...]`, `port = 8080`
   etc. are unannotated overrides of annotated pydantic fields).
2. The SQLite monitor, its example, and its test expectations are built on a
   `PRAGMA data_version` change gate that **never passes in practice**, so it is a
   silent no-op.
3. `Record`-based watchers (the flagship usage) **raise `DBOSException: No DBOS was
   created yet` on the first dispatched event** unless the user calls `launch()`;
   with `launch()`, every hook executes twice (synchronous + queued) because of
   Eventic’s `evented` metaclass wrapper.
4. Any exception in a hook **kills the watchdog observer thread** — the README’s
   “Watcher continues running” guarantee is false.
5. The webhook server is a single-threaded `HTTPServer` with unbounded body reads and
   no timeouts: one idle client claiming a huge `Content-Length` blocks the entire
   server and makes `stop_watching()` **hang forever**.
6. “Automatic persistence” does not exist: the internal `_emit()` constructs a plain
   pydantic model and discards it. Nothing is ever written to Eventic/Postgres by the
   library itself.
7. The test suite is **7/7 failing**.

Scoring is intentionally harsh: this is a pre-1.0 prototype whose documented surface
area is largely non-functional. The core dispatcher design (`EventWatcher`,
`register_hook`, `_dispatch_hook`) is sound and salvageable; the data-layer and
production-server layers need real work.

---

## 1. CRITICAL findings

### C-01 — SQLite monitor is a silent no-op: `PRAGMA data_version` gate never passes

`src/observantic/monitors/sqlite.py` (lines ~140–235)

```python
current_version = int(cursor.execute("PRAGMA data_version").fetchone()[0])
...
if (
    current_version is not None
    and self._last_data_version is not None
    and current_version == self._last_data_version
):
    conn.close()
    return
```

`PRAGMA data_version` returns a **per-connection cached** copy of the database-header
change counter. Opening a fresh connection per check does not guarantee a fresh
counter, and on the platform under test (Python 3.13, SQLite via `sqlite3`) the value
was observed to be permanently stale.

**Reproduction (REPRODUCED, three separate runs):**

| Scenario | Result |
|---|---|
| Create table, insert rows while watcher running | `on_data_changed` never called; `_last_checkpoint` unchanged |
| Same, with `_last_data_version = None` (gate disabled) | row `[1, 1]` **detected correctly** |
| Raw SQLite check: header change counter after commit | 4 → 5 (increments) |
| `PRAGMA data_version` from a fresh connection | still `1` — **stale** |

This is not merely a heuristic miss: it makes the monitor detect *nothing*, ever, in
the default configuration. The user-visible consequence for `sqlite_example.py` is
“Monitoring complete - processed 0 rows” after inserting three rows.

**Why it fails:** the counter in the file header advances at commit, but the value
surfaced by `PRAGMA data_version` on a newly opened connection is the connection’s
cached value, which only refreshes when that connection reads the header again. The
code compares a connection-cached value against a value captured earlier by a
different connection — the comparison is essentially meaningless.

**Fix direction:** drop the gate entirely (or compare with a value re-read from the
*same* connection after forcing a header read); rely on the file-modification event +
rowid scan, and make the rowid scan correct (see H-15).

---

### C-02 — README/example snippets crash at class definition (PydanticUserError)

All of these documented patterns fail the moment the class body is evaluated:

```python
class FileEvent(Record, FileEventBase):
    watch_patterns = ["*.pdf", "*.txt"]  # README Quick Start


class DocumentWatcher(Record, FileEventBase):
    watch_patterns = ["*.docx", "*.pdf"]  # README Watchers


class WebhookReceiver(Record, WebhookEventBase):
    port = 8080  # README Watchers


class TestWatcher(Record, FileEventBase):
    path: str  # tests/tests.py (required field, never provided)
```

```
pydantic.errors.PydanticUserError: Field 'watch_patterns' defined on a base class
was overridden by a non-annotated attribute. All field definitions, including
overrides, require a type annotation.
```

This affects **plain pydantic watchers too** (verified with a non-`Record`
`FileEventBase` subclass): any unannotated override of `watch_patterns`,
`ignore_patterns`, `port`, `host`, `webhook_paths`, `require_auth_header`,
`require_auth_value`, `parse_json_body`, `poll_interval_seconds`,
`track_schema_changes`, or `event_throttle_seconds` raises. Every README example that
configures a watcher via class attributes — which is *the* documented configuration
mechanism — is broken.

**Reproduction:** see README Quick Start executed verbatim → `PydanticUserError` at
`class FileEvent(...)`.

**Fix direction:** document/require annotations (`watch_patterns: list[str] = [...]`,
`port: int = 8080`), or relax the base fields (e.g. make them `ClassVar`-style plain
attributes and read them via `getattr(type(self), ...)`), or configure via
constructor kwargs only. Note the tests themselves use the unannotated style
(`port = 18888`), so tests and library disagree with reality.

---

### C-03 — `Record`-based watchers crash on first event; double-execute with `launch()`

Eventic’s `RecordMeta` (`eventic/core/record.py`) wraps **every public method defined
in a `Record` subclass** with `evented()` (`eventic/queues/dispatcher.py`):

```python
def inner(self, *args, **kwargs):
    result = fn(self, *args, **kwargs)  # run synchronously
    q.enqueue(fn, self, *args, **kwargs)  # enqueue AGAIN for background processing
    return result
```

`EventWatcher._dispatch_hook` (core/base.py) then calls `getattr(self, "on_file_created")`,
which is this wrapper.

**Without `launch()`** (i.e. the README Quick Start, which never calls `launch()`):

```
dispatch raised: DBOSException DBOS Error: No DBOS was created yet
```

The exception propagates out of the watchdog handler and kills the observer thread
(see C-04). **REPRODUCED.**

**With `launch()`** every hook runs twice (synchronously *and* from the queue worker),
so `on_webhook_received`, `on_data_changed`, `on_file_*`, `on_error`, `on_start`,
`on_stop` all execute twice per event — with duplicate side effects (JSONL writes,
`_request_count` increments, Record copy-on-write DB writes). A further
`@Eventic.step()` decorator stacks a third layer of semantics on top.

**Other consequences:**

- `Queue(queue_name)` is created twice per class (once in `RecordMeta`, once inside
  `evented`) → repeated “Queue queue_… has already been declared” warnings.
- `EventicShim.init(*args, **kw)` forwards positionally to
  `Eventic.init(*, name, database_url, …)` which is keyword-only; a positional call
  raises `TypeError`. The shim’s `_instance` cache is redundant with Eventic’s own
  singleton.

**Fix direction:** don’t let the watcher’s hook dispatch path go through Record’s
metaclass wrappers — call the *raw* function (e.g. `self.__class__.__dict__[...]`
with MRO walk, or have `EventWatcher` not inherit from `Record`), and make the
DBOS dependency explicit (auto-`launch()` or degrade gracefully with a clear error at
`start_watching`, not on the first event).

---

### C-04 — Hook exceptions kill the observer thread; README’s “Watcher continues running” is false

`core/base.py::_dispatch_hook` calls `self.on_error(e, event_name)` and **re-raises**.
For file/sqlite watchers the exception unwinds through `watchdog/observers/api.py`
`dispatch_events` → `handler.dispatch(event)`, terminating the observer thread.

**Reproduction (REPRODUCED):** a subclass whose `on_file_created` raises
`ValueError("boom")` →

```
Exception in thread Thread-1: ... ValueError: boom
observer alive after exception: False      # ← monitoring is dead
```

`stop_watching()` then “succeeds” only because the thread is already dead; nothing
resumes monitoring. The README says: *“Errors are caught and passed to on_error…
Watcher continues running.”* — not true.

**Fix direction:** decide the contract. Either swallow (log + `on_error`, no re-raise)
so the observer survives, or stop the watcher cleanly and record state. Re-raising
into a third-party thread with no recovery path is the worst option. The webhook
path already catches (`send_error(500, …)`), so file/sqlite should mirror that.

---

### C-05 — Webhook server: single-threaded DoS + `stop_watching()` hangs forever

`webhook.py` uses the **synchronous** `HTTPServer` (not `ThreadingHTTPServer`) and
reads the body with an **unbounded, untimed** `self.rfile.read(content_length)`.

**Reproduction (REPRODUCED):**

1. Attacker opens a socket, sends `Content-Length: 99999999999`, sends nothing else,
   keeps the connection open.
2. Any subsequent valid request **hangs** (no response within 4 s) — the single
   worker thread is parked in `rfile.read()`.
3. `stop_watching()` (which calls `HTTPServer.shutdown()`) **does not return within
   5 s** — `shutdown()` waits for the blocked handler. The process is now
   unstoppable except by SIGKILL.

This works against the **default configuration** (README’s webhook example sets no
auth). Even with auth configured, a legitimate client that stalls mid-body has the
same effect. There is no `Content-Length` cap, no socket timeout, no body size limit,
and no per-request deadline.

**Fix direction:** `ThreadingHTTPServer`, a maximum body size (reject early with 413),
a socket timeout (`handler.timeout` / `rfile.read(n)` bounded), and `daemon_threads`;
guard `stop_watching()` so a wedged handler can’t block shutdown.

---

### C-06 — Invalid / absent `Content-Length` → unhandled exception / silent body loss

`webhook.py::_handle_request`:

```python
content_length = int(self.headers.get("Content-Length", 0))
body = self.rfile.read(content_length)
```

- `Content-Length: abc` → `ValueError` **outside** the request’s `try/except` →
  unhandled traceback dumped to stderr, connection killed with no HTTP response.
  **REPRODUCED.**
- No `Content-Length` header with a body → `int(0)` → reads 0 bytes → 200 OK with an
  **empty body event**; the body is silently dropped. **REPRODUCED** (sent
  `{"real":"body"}`, hook received `b''`).
- A `Content-Length` larger than the actual body → truncated body accepted (no
  validation); a smaller one → leftover bytes corrupt the next pipelined request
  framing.
- Negative `Content-Length` (`int("-5")` parses) → `read(-5)` returns `b''`.

**Fix direction:** validate `Content-Length` (numeric, `0 <= n <= cap`), handle
parse failure with a proper `400`, and enforce a cap with `413`.

---

## 2. HIGH findings

### C-07 — `webhook_server.py`: typer options are silently ignored

`src/examples/webhook_server.py` (production example) does:

```python
WebhookLogger.port = port
WebhookLogger.host = host
WebhookLogger.webhook_paths = [paths]
WebhookLogger._log_file = log_file
WebhookLogger.parse_json_body = parse_json
```

`port`, `host`, `webhook_paths`, `parse_json_body` are **pydantic fields**;
assigning them on the class after creation does not change instance defaults.

**Reproduction (REPRODUCED):**

```python
WebhookLogger.port = 9999  # after class creation
inst = WebhookLogger()
inst.port  # → 8000  (option ignored)
```

Only `_log_file` (a pydantic *private* attribute) honors the class-level assignment.
So the server always binds `0.0.0.0:8000`, always serves `["/webhook","/api/webhook"]`,
always parses JSON — `--port/--host/--paths/--parse-json` are dead options.
Additionally the auth options are commented out, so `--auth-header/--auth-value`
do nothing either, and `--database-url` is printed but **ignored** — the code
hardcodes `real_url = "postgresql://eventic_user:eventic_pass@postgres:5432/eventic_db"`
(a Docker-style hostname that does not resolve in the provided devenv).

**Fix direction:** pass options to the constructor (`WebhookLogger(port=port, …)`), or
make the config fields `ClassVar` and read them in `start_watching` from `type(self)`.

---

### C-08 — “Automatic event tracking and persistence” does not exist

Every monitor calls `self._emit(<InternalModel>, …)` where `RecordMixin._emit` is:

```python
@staticmethod
def _emit(record_cls, **fields):
    return record_cls(**fields)
```

- `FileRecord`, `DatabaseRow`, `WebhookRecord` are **plain pydantic models**, not
  `eventic.Record`s — no store, no persistence, no create-event.
- The constructed object is **discarded** by every caller (`_emit(...)` as a bare
  expression).
- The user’s own `Record` subclass (e.g. `FileEvent(Record, FileEventBase)`) is never
  instantiated by the library, so no Eventic create/update events fire from the
  library at all.

The README’s “Record already emitted by parent class” comment in `example_file.py`
is therefore incorrect. Persistence only happens if the user manually does
`SomeRecord(...)` or mutates a Record field inside a hook (which then writes one new
version row per assignment — see C-03/C-07 for the double-execution multiplier).

**Fix direction:** emit an instance of the user’s record type (the mixin class), and
either persist it (store append) or document loudly that hooks must persist. Also
note `_emit`/`_dispatch_hook` are public-ish internals with no locking around the
record-creation side effects.

---

### C-09 — Test suite is 100 % broken

`src/tests/tests.py`: **7/7 fail** (run against a live Postgres at
`postgresql://postgres@127.0.0.1:5544/eventic_test`):

| Test | Failure |
|---|---|
| `test_start_stop` / `test_file_events` / `test_double_start_error` / `test_hook_registration` / `test_invalid_hook_registration` | `ValidationError: path Field required` — `class TestWatcher(Record, FileEventBase): path: str` is instantiated as `TestWatcher()` |
| `test_server_lifecycle` / `test_lifecycle_hooks` | `PydanticUserError` — unannotated `port = 18888` override (see C-02) |

Additional blockers if those were fixed: the session fixture requires a live Postgres
(after the fixture, every test needs the `eventic_test` DB); Record-based dispatch
raises `DBOSException` without `launch()` (C-03); and the tests directly poke
`watcher._observer` / `_watching` private state (fragile white-box coupling).

**Fix direction:** give the test record classes defaults (`path: str = ""`), annotate
overrides, mock or launch DBOS, and avoid private attribute assertions.

---

### H-10 — `start_watching()` leaves `_watching=True` on validation failure

`FileEventBase.start_watching` (and `SQLiteEventBase.start_watching`) call
`super().start_watching(path)` first — which sets `_watching = True` and fires
`on_start()` — **before** validating that the path exists.

```python
def start_watching(self, path, recursive=True):
    super().start_watching(path)  # _watching=True, on_start() called
    if not Path(path).exists():
        raise ValueError(f"Path does not exist: {path}")  # ← state left dirty
```

**Reproduction (REPRODUCED):** `start_watching("/nonexistent/…")` raises
`ValueError`; `watcher._watching` is then `True`, so a second `start_watching`
raises “Already watching” and `on_start()` was already invoked for a watcher that
never started.

**Fix direction:** validate first, then set state; or reset `_watching` in the error
path; wrap the whole start in try/finally with rollback.

---

## 3. MEDIUM findings

### H-11 — Traceback spam on client disconnects; internal errors leaked to clients

`webhook.py`: after `self.wfile.write(b'{"status": "ok"}')` raises
`BrokenPipeError` (client disconnected), the `except` path calls
`self.send_error(500, str(e))` which **raises again** (broken pipe), producing a
double traceback per event. **REPRODUCED** (multiple stacked tracebacks per
disconnect). Meanwhile `send_error(500, str(e))` in the hook-error path leaks
exception strings (and potentially internals) to remote callers. Also note
`_dispatch_hook` calls `on_error` *and then re-raises*, so the webhook path calls
`on_error` once but the file/sqlite paths leave the thread dead (C-04).

### H-12 — `on_error` receives the event-name string, not the event

`core/base.py`:

```python
except Exception as e:
    self.on_error(e, event_name)   # event_name is "on_file_created" (a str)
    raise
```

The README documents `on_error(self, error, event)` receiving the event. In practice
`event` is the hook name string, so `webhook_server.py`’s
`getattr(event, "path", None)` always yields `None`. Only the first (method) dispatch
passes a string; registered-callback errors get the same treatment.

### H-13 — Documented environment variables do nothing

README:

```bash
OBSERVANTIC_DB_URL=postgresql://user:pass@localhost/db
OBSERVANTIC_LOG_LEVEL=DEBUG
```

Confidantic (`confidantic/__init__.py::_SettingsSingleton.init`) merges env vars
**by exact field name** — there is no prefix stripping. `OBSERVANTIC_DB_URL` never
matches the `DB_URL` field. **REPRODUCED:** setting `OBSERVANTIC_DB_URL` leaves
`settings.DB_URL` at the default. The working variable would be plain `DB_URL` (and
`LOG_LEVEL`). Docs and implementation disagree.

### H-14 — Confusing global settings state; config is otherwise dead code

- Confidantic auto-creates its own singleton at import time, *before* observantic’s
  mixin is registered, so `confidantic.settings` lacks `DB_URL` while
  `observantic.settings` has it — two different settings objects with the same shape
  of name. **REPRODUCED** (`confidantic.settings is not observantic.settings`).
- `ObservanticSettings.DB_URL` / `LOG_LEVEL` are **never consumed** by the library:
  no code path reads `settings.DB_URL` to initialize Eventic. `EventicShim.init`
  requires explicit arguments. So `config.py` is effectively dead weight plus
  documentation surface that doesn’t work.
- Confidantic side effects at import: recursively `rglob("*.env")` from the project
  root (may load unrelated `.env` files from nested dirs), and **writes
  `.config/confidantic.yaml` to disk on import**.

### H-15 — Rowid-diff change tracking is lossy by design

Even when the C-01 gate passes:

- **Updates** never change `rowid` → never reported.
- **Deletes** are never reported; worse, in `WITHOUT rowid` reuse (and even plain
  `INTEGER PRIMARY KEY` after DELETE), new rows can land on `rowid <= checkpoint`
  and be silently skipped.
- `_last_checkpoint` is **not reset** in `start_watching`, so a restart against a
  rebuilt/truncated DB misses everything below the old checkpoint.
- Tables named from `sqlite_master` are interpolated straight into SQL
  (`f"SELECT rowid, * FROM {table_name}"`, `f" WHERE rowid > {last_rowid}"`). Low
  practical risk (names come from the DB itself) but it is injection-style string
  building and should use parameterization/identifier quoting.
- `except:` bare clauses swallow real failures (e.g. “database is locked” →
  `OperationalError` → row silently skipped; a genuinely locked DB in the outer
  handler becomes a raised `RuntimeError` that kills the observer thread, see C-04).
- `track_schema_changes` is declared but **never read**; no schema-change event is
  ever dispatched (H-16).

### H-16 — `track_schema_changes` is dead configuration

`SQLiteEventBase.track_schema_changes: bool = True` appears nowhere in the code
except its declaration. Setting `False` changes nothing; DDL is never reported even
when `True` (only `on_data_changed` with row diffs exists).

### H-17 — Webhook input handling gaps

- No request-size cap → memory exhaustion risk for large bodies (compounded by
  single-threaded DoS, C-05).
- Auth optional by default and a plain `!=` string comparison (timing side channel —
  low impact here, but trivially better via `hmac.compare_digest`).
- `do_GET`/`do_PUT` are accepted as webhooks; a GET “webhook” with no body still
  emits an event.
- `query_params` are split on `&`/`=` with **no URL-decoding** (`%20`, `+` left raw).
- Path matching is exact (`path not in parent.webhook_paths`) — `/hook/`,
  `/hook?x=1` handled (urlparse strips query) but trailing slash 404s; no wildcards.
- `WebhookRecord.body` may be `bytes`/`str`/`dict`; JSON parse only when
  `Content-Type: application/json`, otherwise attempts utf-8 decode — reasonable, but
  `errors="ignore"` on the fallback silently corrupts binary payloads.

### H-18 — Packaging / dev-workflow mismatches

- `pyproject.toml` says `version = "0.1.0"`; `observantic/__init__.py` says
  `__version__ = "0.2.0"`. Installed metadata reports 0.1.0. **REPRODUCED.**
- README promises `uv run ruff format` and `uv run mypy` — neither ruff nor mypy is
  declared anywhere (no dev-dependency group, no `[tool.ruff]`, no `[tool.mypy]`).
- `description = "Add your description here"` placeholder in pyproject.
- `src/examples/app.py` is **0 bytes** and is shipped in the wheel
  (`packages = ["src/observantic", "src/examples"]`).
- `[project.scripts] start = "examples.webhook_server:main"` — `main` is a
  typer-command function; invoking it directly bypasses CLI parsing (runs with
  defaults).
- Tests can’t run from a clean checkout: no `pytest` dev dependency (I had to add
  it), no test config, and they require an external Postgres with a specific DB
  (`user:pass@localhost/test_db`).

### H-19 — Resource handling / lifecycle gaps

- `sqlite.py::_check_for_changes`: on the outer `except` path, `conn.close()` is
  skipped → connection leak per failed check; same in `_initialize_checkpoints`.
- `FileEventBase._last_event_times` grows unboundedly (never pruned) — long-running
  watches on high-churn directories leak memory.
- `Observer.join()` has no timeout; a wedged emitter thread blocks `stop_watching`
  indefinitely (mirrors the webhook C-05 shutdown hang).
- `on_moved` emits with `src_path` only; `dest_path` is lost from the event payload
  (FileRecord has no destination field), and moved events are not throttled while
  created/modified are — inconsistent.
- `stop_watching()` after the observer died (C-04) leaves `_observer` set to a dead
  object; a subsequent `start_watching` overwrites it, but state introspection
  (`_observer.is_alive()`) misleads.

### H-20 — Cleanliness / dead code

- `exceptions.py` defines `WatcherException`, `RecordCreationException`,
  `ConfigurationException` — **never used**; the library raises bare
  `ValueError`/`RuntimeError` instead, so the “fail fast with clear error messages”
  contract is unfulfilled.
- Unused imports: `Field` in `core/base.py`; `Optional`, `Path`, `field_validator` in
  `config.py`; `Callable` in `monitors/webhook.py`.
- `EventWatcher.run_async` raises `NotImplementedError` — fine as a placeholder, but
  it is part of the public API promised in `__all__`.
- `RecordMixin._emit` duplicates what `DatabaseRow(...)`/`FileRecord(...)` construct
  anyway; in `sqlite.py` the same `DatabaseRow` is constructed twice per row
  (once via `_emit`, once explicitly).
- `.tmuxp.yaml` references `~/Documents/Notes/Projects/observantic` — a stale path
  from a different machine layout.

---

## 4. Verified reproduction scripts

All were run against commit `716687f` with `uv sync` + editable install, a throwaway
Postgres 17 on `127.0.0.1:5544`, and a 3.13 venv.

1. **README Quick Start** → `PydanticUserError` at `class FileEvent` (C-02).
2. **SQLite no-op** (C-01): insert 3 rows → 0 `on_data_changed` calls; with
   `_last_data_version=None` → rows detected.
3. **Record dispatch** (C-03): `_dispatch_hook("on_file_created", …)` →
   `DBOSException: No DBOS was created yet`.
4. **Observer death** (C-04): raising hook → `observer.is_alive() == False`.
5. **Webhook DoS** (C-05): huge `Content-Length`, keep socket open → subsequent
   request times out; `stop_watching()` doesn’t return in 5 s.
6. **Content-Length errors** (C-06): `abc` → ValueError traceback; absent → 200 with
   `b''` body event.
7. **Typer no-op** (C-07): `WebhookLogger.port = 9999` → instance still 8000.
8. **Env var** (H-13): `OBSERVANTIC_DB_URL` set → `settings.DB_URL` unchanged.
9. **Tests**: `pytest src/tests/tests.py` → 7 failed.

## 5. Recommendations (priority order)

1. **Fix the dispatch/error contract first** (C-03, C-04): don’t let hook exceptions
   kill observer threads; decouple dispatch from Record’s metaclass wrapping; make
   the DBOS launch requirement explicit.
2. **Make configuration actually work** (C-02, H-13, H-14): annotated config style,
   honor documented env vars (or fix the docs), consume `settings.DB_URL` or delete
   it.
3. **Rewrite the SQLite change detector** (C-01, H-15): drop the `data_version` gate
   or use it correctly; snapshot by `rowid`+`mtime`/`ctime` per row (or use a
   trigger/`WITHOUT ROWID`-aware strategy); report updates and deletes explicitly;
   parameterize SQL.
4. **Harden the webhook server** (C-05, C-06, H-17): `ThreadingHTTPServer`,
   size caps, timeouts, strict `Content-Length` validation, `hmac.compare_digest`,
   avoid leaking exception strings, make `stop_watching()` non-blocking-safe.
5. **Fix the examples and tests** (C-07, C-09): constructor-based config in
   `webhook_server.py`, repair test record classes, wire `launch()`/mocks, add a
   CI job that actually runs them.
6. **Make persistence real or honest** (C-08): emit the user’s Record type and append
   to the store, or rename the feature and update docs.
7. **Packaging hygiene** (H-18): version consistency, real description, dev
   tooling, drop the empty example, remove dead exceptions/imports.

## 6. What’s actually good

- `EventWatcher`’s hook registry design (`register_hook`/`unregister_hook`/
  `_dispatch_hook`, per-name callback lists, lock-protected mutation) is clean and
  testable.
- Pattern matching via watchdog’s `PatternMatchingEventHandler` is the right tool,
  and the plain (non-`Record`) file watcher happy path works end-to-end
  (pattern filtering, throttle) — verified.
- `FileRecord`/`DatabaseRow`/`WebhookRecord` frozen models with `extra="forbid"` are
  a good idea (the `WebhookRecord` one drops `extra` accidentally).
- Clear separation of monitors from core; the async stub is a reasonable placeholder.
- The `.env` cascade in confidantic is a nice idea, even if the integration is
  unfinished.

Overall: **Not production-ready; the flagship flows do not work as documented.** The
core dispatcher is a good foundation to build on, but the persistence claim, the
SQLite monitor, the webhook server’s robustness/security, and the example/test suite
each need a dedicated pass.
