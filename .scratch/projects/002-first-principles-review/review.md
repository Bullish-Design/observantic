# Observantic — First-Principles Reimagination & Adversarial Review

Reviewer: pi (adversarial senior architect)
Date: 2026-08-04 — commit `4da93c3`, review #002 (post-refactor, eventic 0.1.5)

Companion: `proposal.md` (Phase-3 reimplementation), `repros.py` (all reproductions,
run `devenv shell uv run python .scratch/projects/002-first-principles-review/repros.py`).

---

## 0. Executive summary

The previous review (001) found 16 issues, and the code was rewritten: the suite now
passes 85/85, ruff and mypy are clean, and most of the review-001 contract holds —
validation-before-state-flip, error containment, `call_unwrapped` dispatch bypass,
snapshot-based SQLite diffing, hardened webhook server, real persistence. **This
review is not a bug hunt; it is a design audit.** The verdict: the reimplementation
fixed the *symptoms* but preserved a set of *structural* decisions that cannot be
repaired by another patching pass. Four of them are critical:

1. **The persistence contract is incoherent and the README's "opt-in" promise is
   false.** For the flagship pattern (`class MyEvent(Record, FileEventBase)`), calling
   `init()` alone causes every emitted event to write a durable v0 row and fire
   `@on.create` — regardless of `auto_persist=False` (R-01). `auto_persist=True` on a
   plain (non-Record) watcher logs a warning per event and persists nothing (R-02).
   The two knobs do not control what the README says they control.

2. **`stop_watching()` can permanently deadlock.** Called from inside a SQLite hook,
   the poll thread holds `_check_lock` and waits on watchdog's internal `_lock`, while
   the watchdog thread waits on `_check_lock`. Neither wait has a timeout. Verified
   with a thread-stack dump (R-06). File events can also fire *after*
   `stop_watching()` returns (R-07).

3. **The webhook server has a request-framing bug on every rejection path.** 400/401/
   404/405/413 responses are sent without draining the request body, so the leftover
   bytes are parsed as the *next* request line on the same keep-alive connection
   (verified: `501 Unsupported method ('JUNKPOST')`, R-18). The concurrent 500
   decision also races on shared `_last_hook_error` state (R-04).

4. **The watcher-as-Record conflation pollutes the persisted schema and constrains
   the record shape.** Emitted records carry 9 watcher-config fields (R-15), a
   required record field makes the watcher unconstructible (R-16), and subclassing in
   the natural Python style raises `PydanticUserError` (R-17). The library's core
   abstraction — a pydantic `BaseModel` base that users must subclass and merge with
   `Record` — is the root cause of all three, and of the `dispatch_direct` escape
   hatch that is documented but does not do what it claims (R-10).

Scoring (per dimension, current → target) is in §8. The gap is the work estimate.

---

## 1. Phase 1 — First-principles analysis

### 1.1 What is the library's true job?

The README's framing — "bridges external events … to Eventic Records through
customizable hooks" — names the *integration target* but not the *essence*. From
first principles, the job is:

> **Provide a uniform, safe, lifecycle-managed bridge from external event sources
> (filesystem, SQLite, HTTP) to user code, with honest, optional persistence of
> typed event records via Eventic.**

The eventic coupling is **incidental** to the core value. The core value is the
bridge: a watcher that (a) validates before starting, (b) never dies from user-code
errors, (c) dispatches typed events to user code exactly once, (d) stops cleanly and
boundedly, and (e) optionally records events as Eventic Records. Eventic is one
optional sink among potentially many (logfile, Kafka, stdout…). Every design decision
should be evaluated against the bridge first and the sink second. The current code
inverts this: the sink (durable v0 at construction) drives behavior that the bridge
knobs pretend to control.

### 1.2 Users and workflows

| # | User | Workflow | Served today? |
|---|------|----------|---------------|
| 1 | "Record the world" | Subclass `(Record, FileEventBase)`; every event becomes an Eventic Record with create/update handlers | Yes, but the *knobs are lies* (R-01/R-02) and the schema is polluted (R-15) |
| 2 | "React" | No Eventic; watch a dir/DB/HTTP endpoint and run callbacks (`register_hook` or overrides) | Yes, mostly — but `persist_strict`/`auto_persist` traps can kill the observer (R-03), and plain models can't persist at all (R-02) |
| 3 | "Service" | Long-lived webhook receiver with auth, JSONL audit, graceful shutdown | Yes, but framing bugs (R-18), auth leakage (R-09), shared-state races (R-04/R-05) |
| 4 | "Audit/sync" | Row-level DB change tracking for replication/backup | Yes — snapshot diffing is correct; but stop-from-hook deadlocks (R-06) and raw errors (R-12) |

Features serving **none** of these users: `run_async` (R-removal 1), `dispatch_direct`
(R-10), `auto_persist`/`persist_strict` in their current semantics (R-01/R-02/R-03),
`can_persist` (R-removal 2), `is_launched` (R-removal 3), `confidantic` and
`python-dotenv` (R-removal 4), the `start` console script (R-removal 5).

### 1.3 Smallest coherent core

```
Watcher (lifecycle + hook registry + emission)
  ├── validate → start (spawn source threads) → dispatch events → stop (join bounded)
  ├── hooks: typed names, override methods + registered callbacks, errors → on_error
  └── record_model → record(**fields); Eventic seam handles persistence, if wired
Source per external type: file / sqlite / webhook (validate, produce events, teardown)
```

**Delete entirely:** `run_async`, `dispatch_direct`, `call_unwrapped`, `can_persist`,
`is_launched`, `WebhookRecord` (duplicate of `WebhookEvent`), `auto_persist` +
`persist_strict` (replaced by a single up-front `persist_required` check), the
`record_model` *field* (replaced by constructor config), `confidantic`,
`python-dotenv`, the `start` entry point, the `examples` package in the wheel.

### 1.4 Invariants — the contract that must never break

1. **Observer survival** — a user hook raising must never kill the source thread.
2. **No double-execution** — every event dispatches to each target exactly once.
3. **Fail-fast start** — `start_watching` validates everything it can before spawning
   threads; on implementation failure, state rolls back and typed errors propagate.
4. **Bounded, honest stop** — `stop_watching` returns promptly, is idempotent, is
   deadlock-free even when called from within a hook, and dispatches **no** events
   after it returns.
5. **Honest persistence** — what the docs say is persisted, is persisted, and nothing
   more; a "disabled" persistence path must not write.
6. **Bounded resources** — throttle maps pruned, body caps enforced, table snapshots
   capped, thread count flat over start/stop cycles (no leaks).

The current code violates **1** (R-03), **4** (R-06, R-07), and **5** (R-01).

### 1.5 Is "monitor" (subclass the base class) the right abstraction?

The subclass-with-override-methods pattern is *one* good UX for workflow 1. But the
current implementation conflates four concerns in one class hierarchy:

- **Configuration** (`watch_patterns`, `port`, `poll_interval_seconds`…)
- **Event schema** (the Record's fields)
- **Behavior** (hook methods)
- **Runtime state** (`PrivateAttr`s)

The conflation is the source of R-15 (config fields in persisted JSON), R-16
(required record fields break construction), R-17 (unannotated overrides raise), and
the Record-MRO fragility. Composition (a `Watcher` + a separately-declared
`record_model`) keeps the subclass UX for behavior but separates schema from watcher.
That is the target (§ proposal D1/D2). The plain callback registry already exists and
stays (workflow 2). A declarative DSL and an async framework were considered and
rejected (§ proposal D2).

### 1.6 Is `EventWatcher` as a pydantic `BaseModel` the right shape?

**No.** Verified consequences of pydantic-as-base:

- Unannotated class-attribute config raises `PydanticUserError` (R-17) — a
  permanent footgun for the natural Python style, which even *this review's own
  repros* hit twice.
- Config fields become part of the record schema (R-15), because for Record users
  the watcher *is* the record.
- Required record fields make the watcher unconstructible (R-16).
- Mutable state lives in `PrivateAttr` (fine) but the model carries `Lock` objects,
  is not picklable, and — when merged with `Record` — inherits `frozen=True`,
  `extra="allow"` and copy-on-write `__setattr__` semantics that make *any*
  accidental config mutation a store write.

A plain class with an explicit `__init__` and a tiny manual config-resolution
(class attributes + constructor kwargs) buys: no metaclass merging, no schema
pollution, no annotation tax, no Record MRO fragility. Losing pydantic's config
validation is acceptable — the config surface is small and validated in
`__init__`/`_validate_start` with the typed exceptions the library already has.

### 1.7 Is the stringly-typed hook dispatch right?

The dispatch *behavior* (override + registered callbacks, swallow → `on_error`) is
good and tested. The *mechanism* has two defects:

1. **`call_unwrapped` exists only because watchers can be `Record` subclasses.**
   With the target plain-class base, `getattr(self, name)` is the raw method and the
   seam function, the `inspect.unwrap` chain, and the `dispatch_direct` escape hatch
   all disappear. The README's "DBOS queue semantics via `register_hook` +
   `dispatch_direct=False`" is false today: registered callbacks are never wrapped by
   RecordMeta (only class methods are), so they run inline (R-10).
2. **`_last_hook_error` is instance state shared across threads.** The webhook 500
   decision reads it after dispatch; a concurrent request can clear or overwrite it
   (R-04, 3–9 wrong answers per 20 pairs). Dispatch should **return** the error, not
   stash it on `self`.

### 1.8 Is the persistence model coherent with eventic 0.1.5?

**No — it is fighting the platform.** eventic 0.1.5's durable-v0 semantics are:
constructing a `Record` with a wired store appends the v0 row and fires `@on.create`
*at construction*. Observantic's `auto_persist` is therefore redundant for Record
models (construction already persists — R-01) and impossible for plain models (no
store — R-02). The only scenario where `auto_persist` does something is a record
constructed before `init()` and appended after — not a real workflow. The honest
model is: **persistence happens iff (a) the watcher's `record_model` is a Record
subclass and (b) Eventic is initialized** — plus one explicit up-front knob,
`persist_required=True`, that makes a missing backend a start-time
`ConfigurationException` instead of a runtime surprise.

### 1.9 Is the threading model sound?

The funnel design (source threads → `_dispatch_hook`) is sound. The details are not:

- **Lock-order deadlock (R-06, Critical):** sqlite poll thread holds `_check_lock`
  while dispatching hooks; watchdog's `dispatch_events` holds its `_lock` while
  calling handlers; `stop_watching` → `observer.stop()` → `unschedule_all()` needs
  the watchdog `_lock`. The poll thread's hook calls `stop_watching`: poll waits on
  watchdog `_lock`, watchdog waits on `_check_lock`. Permanent deadlock, verified by
  stack dump. File monitor's stop-from-hook instead raises a swallowed RuntimeError
  (self-join).
- **Async stop (R-07):** `observer.join(timeout=5)` doesn't stop the current dispatch
  batch; handlers complete after `stop_watching` returns and can emit late events.
- **TOCTOU start (R-11):** the "Already watching" check and the state flip are
  separate lock acquisitions; two concurrent starts both pass.
- **Shared `_last_hook_error` (R-04).**
- **Class-level connection tracking shared across servers (R-05)** — and it never
  tracks a socket long enough to unblock a wedged handler anyway.
- **`_check_lock` held during user hooks** — a slow hook blocks the other checker
  (poll ↔ watchdog) and, via the watchdog handler path, delays file events.

### 1.10 Should the library be synchronous? Is `run_async` a lie?

Synchronous is right: watchdog, sqlite polling, and HTTP serving are naturally
thread-based; an async rewrite would change the programming model for no user value.
But `run_async` is a lie — an `async def` that unconditionally raises
`NotImplementedError`, exported nowhere and documented nowhere. Delete it; if async
is ever needed it will be a new API, not this stub.

---

## 2. Findings

Severity: Critical / High / Medium / Low. Each finding is **verified** (repro in
`repros.py`) unless marked *by inspection*. "Could not reproduce" is stated
explicitly where relevant (R-07 timing, R-10 semantics).

### R-01 — `auto_persist` does not gate persistence; `init()` alone persists everything (Critical)

**Where:** `src/observantic/core/base.py:51-56` (`auto_persist`/`persist_strict`),
`_emit` at 182-215; `src/observantic/_eventic.py:135-156` (`persist`); README
"Persistence is opt-in and explicit".

**Verified reproduction (repros.py R-01):** with `auto_persist=False` (the default),
a `(Record, FileEventBase)` watcher + `init(sqlite:///…)` → `_emit(...)` puts a
durable **v0 row in the store and fires `@on.create` handlers** (counted: 2).
Persistence is not opt-in: any Record-based watcher writes a row per event the moment
`init()` is called. The `webhook_server.py` example's comment "auto_persist is off by
default; persistence disabled" is therefore false.

**Impact:** the flagship pattern silently writes DB rows users never asked for; the
README's central persistence promise is false. Users who want events without rows
cannot get them.

**Fix direction:** separate the *knobs* from the *platform behavior*. Document that
Record construction persists v0 (platform semantics); the only opt-out is "don't use
a Record as `record_model`". Replace `auto_persist`/`persist_strict` with a single
up-front `persist_required: bool = False` validated at `start_watching`.

### R-02 — `auto_persist=True` on a plain watcher warns per event and persists nothing (Medium)

**Where:** `core/base.py:196-210`.

**Verified (R-02):** `FileEventBase(auto_persist=True)` (no `record_model`) →
`_emit` constructs `FileRecord` (plain pydantic, no store) → `persist()` raises
`EventicNotReadyError` → logged warning per event; nothing is persisted. The knob
has no working configuration for plain watchers — it is a per-event warning generator.

**Fix direction:** remove the knob; if a user sets `record_model` to a non-Record
class, that's their business, but persistence is documented as Record-only.

### R-03 — `persist_strict=True` kills the file observer (emit escapes dispatch) (High)

**Where:** `core/base.py:205-209` (raise inside `_emit`); `monitors/file.py:125-131`
(`on_created` calls `_emit` outside `_dispatch_hook`).

**Verified (R-03):** `auto_persist=True, persist_strict=True` + no `init()` → a file
create raises `ConfigurationException` inside `_emit`, inside the watchdog handler →
**`observer.is_alive() == False`** (full traceback in repro output). Violates the
"observer never dies" invariant. The same path exists in `monitors/sqlite.py`'s
watchdog-triggered check (`_emit_row` at sqlite.py:232-238).

**Fix direction:** never raise from `_emit` inside a source thread; validate
`persist_required` up front at `start_watching`, and report emit/persist failures
through `on_error`.

### R-04 — Webhook 500 decision races on shared `_last_hook_error` (High)

**Where:** `webhook.py:120` (`raise_on_hook_error: bool = True`); `core/base.py:147,
155-156` (clear-then-write on `self`); `webhook.py:232-240` (500 decision).

**Verified (R-04):** 20 concurrent pairs of (successful request, raising request) →
**3–9 of 20 pairs answered the wrong status** (e.g. a successful `/ok` got 500 because
a concurrent `/fail` raised). `_last_hook_error` is shared instance state, cleared at
the start of every dispatch.

**Impact:** wrong HTTP semantics under concurrency — a hook failure can 500 an
unrelated successful request, or a failure can be masked as success.

**Fix direction:** `_dispatch_hook` **returns** the last error (thread-local by
construction); the webhook answers 500 from the return value of its own dispatch
call. Delete `_last_hook_error`.

### R-05 — `_ConnectionTrackingMixIn` state is class-level, and the tracking never works (Medium)

**Where:** `webhook.py:58-83` (`_connections: set = set()` and `_conn_lock` are class
attributes; `process_request` removes the socket in a `finally` immediately after the
handler *thread is spawned*, not after it *finishes*).

**Verified (R-05):** two `WebhookEventBase` servers share the same `_connections`
set (`w1._server._connections is w2._server._connections` → True) — one server's
`stop_watching()` can close the other's sockets. Separately, with a genuinely wedged
client (Content-Length 100, no body), the set holds **0** sockets while the handler
is blocked — `close_all_connections()` can never unblock anything, because
`ThreadingMixIn.process_request` returns before the handler thread runs.

**Impact:** multi-server processes can kill each other's connections; the
close-all-connections hardening is dead code (the bounded stop works only because
handler threads are daemons with a 30 s socket timeout).

**Fix direction:** per-instance state; track sockets for the handler's lifetime
(override `process_request_thread` or add/remove in `handle`); or remove the mixin
and rely on daemon threads + documented timeout for stop.

### R-06 — `stop_watching()` from inside a SQLite hook permanently deadlocks (Critical)

**Where:** `sqlite.py:73` (`_check_lock`), `sqlite.py:141-161` (`_check_for_changes`
holds the lock across dispatch), `sqlite.py:114-123` (`_stop_impl` → `observer.stop()`),
`core/base.py:110-123`.

**Verified (R-06):** hook on the poll thread calls `stop_watching()` → poll holds
`_check_lock` and blocks in watchdog's `stop()` → `on_thread_stop()` →
`unschedule_all()` → `with self._lock:`; the watchdog thread is in
`SQLiteHandler.on_modified` → `_check_for_changes` → `with self._check_lock:`.
**Neither wait has a timeout — the watcher is permanently wedged** (thread-stack
dump in repro output; `_poll_thread.is_alive()` True forever). The file monitor's
stop-from-hook instead hits `observer.join()` on the current thread → RuntimeError,
silently swallowed by `stop_watching` (verified in the same repro run); stop is
therefore *not* actually synchronous there either.

**Impact:** a user hook that calls `stop_watching()` (a natural thing to do on a
terminal event) hangs the process's worker forever. Also: `stop_watching()` from the
*main* thread while a slow hook runs on the poll thread blocks until the hook
finishes (lock ordering).

**Fix direction:** never hold `_check_lock` (or any source-internal lock) while
dispatching user hooks — snapshot the diff under the lock, dispatch outside it.
Document and test stop-from-hook; make the stop protocol a `_stopping` flag that
handlers check before emitting, with bounded joins and no self-join.

### R-07 — File events can fire after `stop_watching()` returns (Medium)

**Where:** `file.py:100-162` (handlers never check `_watching`); `file.py:72-77`
(`_stop_impl` joins the observer, but a mid-batch dispatch completes afterwards);
`core/base.py:110-123`.

**Verified (R-07):** with a handler parked in dispatch, `stop_watching()` returns in
~0.3 s while the parked dispatch completes after — i.e. **a hook runs after stop**,
and can emit/persist late records. The invariant "no events after stop" is not held.

**Fix direction:** handlers check `self._watching` before `_emit`/`_dispatch`; the
stop protocol drains or cancels pending batches; document that stop is synchronous
with respect to *dispatching*, not necessarily *thread exit*.

### R-08 — Chunked `Transfer-Encoding` bodies are silently dropped (Medium)

**Where:** `webhook.py:259-272` (`_read_body` only honors `Content-Length`).

**Verified (R-08):** a `Transfer-Encoding: chunked` request with a real JSON body →
`200` with an **empty-body event**. Real clients (many HTTP libraries, proxies) send
chunked bodies; the data vanishes silently.

**Fix direction:** implement chunked decoding (bounded), or return `501` for
`Transfer-Encoding` requests so the drop is at least honest.

### R-09 — The auth header value is forwarded into event payloads and persisted records (Medium)

**Where:** `webhook.py:209-218` (`headers = {k: v for k, v in self.headers.items()}`;
`WebhookEvent.headers`; `_emit(path=…, headers=…, …)`).

**Verified (R-09):** with `require_auth_header="X-API-Key"`, the hook's
`event.headers` contains `X-API-Key: super-secret`. If the watcher persists a Record,
the secret is stored with the event.

**Impact:** credential leakage into logs/stores for every authenticated webhook.

**Fix direction:** strip the configured auth header from the delivered headers
(and/or add a `redact_headers` option).

### R-10 — `dispatch_direct=False` + `register_hook(@evented)` does not give DBOS queue semantics (Medium)

**Where:** `core/base.py:159-172` (`_hook_callables`); README "If you want DBOS queue
semantics, register an explicitly-decorated callback via register_hook and set
dispatch_direct=False."

**Verified (R-10):** a `@evented`-marked function passed to `register_hook` runs
**inline** — `RecordMeta` only wraps *class methods*, so the "explicitly-decorated
callback" is never scheduled on a queue. The documented escape hatch is false.

**Fix direction:** remove `dispatch_direct` and the README paragraph; if queue
semantics are ever needed, they belong to Eventic (which already offers
`@evented` on its own classes).

### R-11 — Concurrent `start_watching` has a TOCTOU double-start (Medium)

**Where:** `core/base.py:96-105` (check and flip are separate lock sections).

**Verified (R-11):** with a widened `_validate_start` window, two threads both pass
the "Already watching" guard; both call `_start_impl`; the second observer silently
replaces the first (leaked thread, duplicated dispatch).

**Fix direction:** re-check `_watching` under the lock at flip time
(no-op or raise if another thread flipped first).

### R-12 — Locked DB at `start_watching` raises a raw `sqlite3.OperationalError` (Medium)

**Where:** `sqlite.py:89-111` (`_refresh_snapshot` at 163 is outside the
try/except that wraps the observer setup), `sqlite.py:83-86`.

**Verified (R-12):** `BEGIN EXCLUSIVE` held by another connection →
`start_watching` raises `sqlite3.OperationalError` (module `sqlite3`), not a typed
`ObservanticException`. Violates the typed-exceptions contract.

**Fix direction:** wrap `_refresh_snapshot` in the same `try/except → WatcherException`
path as the observer setup.

### R-13 — Same-named Record classes raise at class-definition time (Low, documented)

**Where:** eventic `RecordMeta` queue keying; README documents it.

**Verified (R-13):** second `class DupRec(Record)` → `Exception: Queue queue_dup_rec
has already been declared`. This is a platform constraint, not an observantic bug —
but it makes the "examples are importable" test brittle: importing the examples
twice, or any user code colliding with example names, raises at import. In the target
design (watchers no longer inherit `Record`) the constraint stops biting observantic
users entirely.

### R-14 — *by inspection* — `_eventic.is_launched` reads a DBOS private attribute (Low)

`_eventic.py:95-103` does `getattr(instance, "_launched", False)` on the DBOS
singleton — a private attribute of another library, with no test coverage except
"is False before init". Unused by the library. Delete (R-removal 3).

### R-15 — Watcher-as-Record pollutes the persisted schema with 9 config fields (High)

**Where:** `core/base.py:38-60` (config fields), `_emit` (182-215); README flagship
pattern `class FileEvent(Record, FileEventBase)`.

**Verified (R-15):** an emitted record's `model_dump` contains
`record_model, auto_persist, persist_strict, dispatch_direct, raise_on_hook_error,
watch_patterns, ignore_patterns, case_sensitive, event_throttle_seconds` — every
persisted event carries the watcher's full configuration. The record schema and the
watcher schema are one and the same.

**Impact:** persisted rows are cluttered with config that will drift as the watcher
is reconfigured; `where()`-style queries see noise; the schema no longer models the
event.

### R-16 — A required record field makes the watcher unconstructible (Medium)

**Where:** same conflation as R-15.

**Verified (R-16):** `class ReqRec(Record, FileEventBase): path: str` (no default) →
`ReqRec()` raises `ValidationError: Field required`. Users cannot write the natural
pydantic record schema; every field needs a default, and the defaults leak into the
stored rows as event data.

### R-17 — Unannotated config overrides raise `PydanticUserError` (Medium, recurring)

**Where:** any monitor config field; `core/base.py` base fields.

**Verified (R-17):** `class W(FileEventBase): event_throttle_seconds = 0.5` →
`PydanticUserError: Field ... overridden by a non-annotated attribute`. This is the
review-001 C-02 trap, narrowed but not removed: it now requires *annotation*, which is
an un-Pythonic tax that even this review's repros tripped on twice. Every example and
test meticulously annotates — a strong signal the design is hostile to the natural
style.

### R-18 — Unread bodies on rejection paths corrupt the next keep-alive request (High)

**Where:** `webhook.py:191-240` (`_handle_request` returns after `_send_json(4xx/413)`
without draining); `webhook.py:259-272` (413 returns before reading);
`BaseHTTPRequestHandler` keep-alive loop (HTTP/1.1 default).

**Verified (R-18):** POST with `Content-Length: 99999999` + 9 junk bytes → `413`;
the same connection's next request arrives as **`501 Unsupported method
('JUNKPOST')`** — the leftover body is parsed as the request line. Same verified for
the 401 path (`'SECRETZZPOST'`). Affects 400/401/404/405/413 whenever the client
sent a body it didn't need to.

**Impact:** dropped/corrupted requests from real clients that reuse connections;
classic HTTP request-framing hazard.

**Fix direction:** on every rejection path either drain the body (bounded) or set
`Connection: close` / `self.close_connection = True` before responding.

---

## 3. R-removals — delete outright

| # | Item | Why |
|---|------|-----|
| 1 | `EventWatcher.run_async` (`core/base.py:220`) | lying stub; async is a non-goal |
| 2 | `can_persist()` (`_eventic.py:158`) | defined, exported, never called |
| 3 | `is_launched()` (`_eventic.py:95`) | reads DBOS private `_launched`; unused |
| 4 | `call_unwrapped`/`dispatch_direct` (`_eventic.py:112`, `core/base.py:167`) | only exist for the Record-watcher conflation; gone with the plain-class base |
| 5 | `auto_persist` + `persist_strict` fields (`core/base.py:51-56`) | incoherent with durable-v0 (R-01/R-02); replaced by up-front `persist_required` |
| 6 | `_last_hook_error` (`core/base.py:73`) | race (R-04); dispatch returns the error instead |
| 7 | `WebhookRecord` (`webhook.py:33-43`) | duplicate of `WebhookEvent` |
| 8 | `on_data_changed` legacy hook (`sqlite.py:157-160`) | superseded by per-row hooks; kept one release as deprecated or dropped (proposal: drop with migration note) |
| 9 | `confidantic` (pyproject:12,35) | declared, never imported (verified) |
| 10 | `python-dotenv` (pyproject:14) | declared, never imported (verified) |
| 11 | `[project.scripts] start` (pyproject:18) | calls `webhook_server.main` directly, bypassing typer CLI — runs a server that ignores all options |
| 12 | `src/examples` in the wheel (pyproject:38) | examples are dev artifacts, not library API; `# /// script` headers point at a private git repo |
| 13 | `_ConnectionTrackingMixIn` (webhook.py:58) | broken by design (R-05) |

---

## 4. What's actually good

These must survive the reimplementation:

1. **The lifecycle state machine** (`core/base.py:94-123`): validate-before-flip,
   rollback on impl failure, idempotent stop, `on_start`/`on_stop`/`on_error` via
   `_safe_call`. Verified rollback on validation and impl failure; genuinely well
   shaped (fixed H-10 properly).
2. **Hook error containment** (`_dispatch_hook`): swallow → `on_error` with the real
   event object; the observer survives user exceptions (tested with a raising hook +
   `observer.is_alive()`). The contract in §1.4(1) holds *for hook errors*; only the
   emit path (R-03) and the races (R-04) violate it.
3. **The Eventic seam** (`_eventic.py`): every eventic import behind one module, with
   a written contract and lazy imports that make eventic a viable *optional*
   dependency (verified: no module-level `eventic` import anywhere in `src/`).
4. **SQLite change detection**: snapshot-diffing correctly reports inserts, updates,
   deletes and DDL (verified by tests); the old `data_version` gate is gone; table
   names are quoted, connections are always closed, `max_table_rows` bounds memory,
   snapshots reset on restart.
5. **Webhook hardening that works**: `ThreadingHTTPServer` + daemon threads,
   `Content-Length` validation (400/413), socket `timeout=30`, constant-time auth
   with paired-header validation, generic 500s that never leak internals, bounded
   shutdown (<1 s verified), JSON/query decoding.
6. **`call_unwrapped` + `inspect.unwrap` correctness** — a legitimate fix for
   eventic's metaclass wrapping, executed right (verified against both decorated and
   undecorated Record methods). It is the *right fix for the wrong architecture*;
   the plain-class base makes it unnecessary, but the seam discipline stays.
7. **The test suite** is meaningful: 85 tests, mostly integration-level (real
   filesystem events, real sockets, real sqlite), stable across 5 consecutive runs of
   the file+sqlite modules, no Postgres required for the persistence tests (sqlite
   backend), and the eventic-reset autouse fixture is correct hygiene.
8. **Typed exceptions** exist and are used where validation happens
   (ConfigurationException for bad paths/auth pairs; WatcherException for
   start failures). R-12 is the remaining hole.

---

## 5. What's *not* tested (gaps the suite papers over)

- **Concurrency.** No test with concurrent webhook requests (R-04 uncovered it
  immediately), no concurrent `start_watching` (R-11), no stop-from-hook (R-06),
  no poll↔watchdog interleaving (R-05's sibling), no events-after-stop (R-07).
- **Resource leaks.** No test asserts thread counts stay flat over N start/stop
  cycles; no throttle-map boundedness test beyond the 2-entry unit case; no webhook
  handler-thread leak check after wedged clients.
- **Protocol edge cases.** No chunked encoding (R-08), no keep-alive reuse after
  rejection (R-18), no multi-server coexistence (R-05).
- **Persistence honesty.** No test asserts that `auto_persist=False` writes nothing
  (it writes — R-01); no test for `auto_persist` on plain models beyond the warning
  (nothing is persisted — R-02).
- **Fuzz/property.** No random row-op diffing test, no malformed-input fuzzing of
  the webhook parser.
- **White-box coupling:** `test_core_state.py`, `test_core_dispatch.py`, and
  `test_file_monitor.py` poke `_watching`, `_observer`, `_last_event_times`,
  `_dispatch_hook` directly — acceptable for unit tests, but the suite has no
  black-box behavioral layer for the public API.

---

## 6. Docs vs reality (README sentence-by-sentence)

| README claim | Reality |
|---|---|
| "Persistence is **opt-in and explicit** — the library does not write to Eventic unless you initialize it" | **False** for Record watchers: `init()` alone writes v0 per event (R-01). True only for plain watchers, which then can't persist at all. |
| "`auto_persist: bool = False` — when True, each emitted record is also explicitly appended to its Eventic store (an idempotent re-append for Record models)" | True but pointless: construction already persisted it (R-01); for plain models it warns per event and appends nothing (R-02). |
| "Hook errors are reported via `on_error(error, event)` and **do not stop the watcher**" | True for hook errors; false when `persist_strict=True` breaks out of `_emit` (R-03). |
| "`stop_watching()` closes live connections and returns promptly" | True for the *tested* wedge (Content-Length over cap → 413, no block); false for the genuinely wedged sub-cap client unless you wait for the 30 s timeout, and `close_all_connections` can't help (R-05). |
| "If you want DBOS queue semantics, register an explicitly-decorated callback via `register_hook` and set `dispatch_direct=False`" | **False** (R-10). |
| "Validation happens up front … raises a typed `ConfigurationException`" | False for a locked DB at start (raw `sqlite3.OperationalError`, R-12). |
| "Eventic may only be initialized once per process; `init()` is idempotent" | True, and `reset()` works (tested with sqlite backend). |
| "DBOS queue per `Record` subclass at class-definition time … duplicate declarations raise" | True (R-13), and it bites example imports. |
| Config: "`OBSERVANTIC_DB_URL` … wins when both are set" | True (H-13/H-14 fixed properly). |
| Examples run via `# /// script` headers | Headers reference a **private git repo** (`Bullish-Design/observantic`) — not runnable by third parties. |

---

## 7. Verified-repro summary

| ID | Severity | Repro | Result |
|----|----------|-------|--------|
| R-01 | Critical | `repros.py` R-01 | REPRODUCED (v0 row + create handlers with `auto_persist=False`) |
| R-02 | Medium | R-02 | REPRODUCED (warning per event, nothing persisted) |
| R-03 | High | R-03 | REPRODUCED (observer dead after strict-persist event) |
| R-04 | High | R-04 | REPRODUCED (3–9/20 wrong statuses across runs) |
| R-05 | Medium | R-05 | REPRODUCED (shared set) + NOTE (tracking never holds a blocked socket) |
| R-06 | Critical | R-06 | REPRODUCED (permanent deadlock; stack dump) |
| R-07 | Medium | R-07 | REPRODUCED (dispatch outlives stop) |
| R-08 | Medium | R-08 | REPRODUCED (chunked → empty body) |
| R-09 | Medium | R-09 | REPRODUCED (auth value in payload) |
| R-10 | Medium | R-10 | OBSERVED (callback runs inline; claim false) |
| R-11 | Medium | R-11 | REPRODUCED (both starts pass; two observers) |
| R-12 | Medium | R-12 | REPRODUCED (raw `sqlite3.OperationalError`) |
| R-13 | Low | R-13 | REPRODUCED (duplicate queue error) |
| R-14 | Low | by inspection | N/A |
| R-15 | High | R-15 | REPRODUCED (9 config fields in record) |
| R-16 | Medium | R-16 | REPRODUCED (`ValidationError` on required field) |
| R-17 | Medium | R-17 | REPRODUCED (`PydanticUserError`) |
| R-18 | High | R-18 | REPRODUCED (`501 Unsupported method ('JUNKPOST')`) |

---

## 8. Verdict

| Dimension | Current | Target | Justification |
|-----------|:-------:|:------:|---------------|
| API design | 5 | 9 | Good hook UX, but pydantic-as-base taxes users (R-15/16/17), knobs lie (R-01/02), `run_async`/`dispatch_direct` dead weight |
| Core architecture | 6 | 9 | State machine + seam are right; watcher-as-Record conflation and `_last_hook_error` are structural defects, not patchable |
| Threading | 4 | 9 | Deadlock (R-06), TOCTOU start (R-11), async stop (R-07), shared class state (R-05) |
| Persistence contract | 3 | 9 | `auto_persist`/`persist_strict` incoherent with durable-v0; "opt-in" promise false (R-01/02/03) |
| Monitoring correctness | 7 | 9 | Snapshot diffing correct; webhook framing (R-18), chunked drop (R-08), leaky auth (R-09) |
| Test quality | 6 | 9 | Meaningful, stable, green — but zero concurrency/leak/protocol tests; white-box-heavy; everything in §5 untested |
| Docs | 5 | 9 | Accurate about mechanics, false about the central persistence promise and the queue-semantics claim |
| Packaging | 4 | 9 | Unused deps (confidantic, dotenv), fake `start` script, examples in wheel with private-git script headers, eventic could be optional but isn't |

Gap ≈ 27 points across eight dimensions — i.e., the reimplementation in `proposal.md`
is a medium rewrite of the core (plain-class base, return-value dispatch, honest
persistence, stop protocol), not a patch.
