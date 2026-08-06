# Observantic × eventic 1.1.0 — Follow-up (004)

Follow-up to project 003 (`eventic-v1-alignment`, commit `4e50cc0`). 003 got
observantic onto eventic 1.1.0 (declarations, `bind()`, `auto_persist`). This
project is the **hardening pass**: the 003 checklist is green in the happy
path, but live probing found five concrete defects in the deployed shape of
that alignment — two of them data-loss/crash bugs.

## Evidence (all reproduced against the live repo)

| # | Finding | Severity | Evidence |
|---|---|---|---|
| F1 | `SQLite(":memory:")` corrupts under concurrent `create()` from observer threads (`StaticPool`, one shared connection). 4 threads × 50 emits → 76 errors (`StoreError('commit failed')`, `NotFound('revision absent')`) and ~half the writes silently lost. File-based SQLite is safe (QueuePool + WAL + busy_timeout). | **High — data loss** | probe run |
| F2 | A raise from `_emit`/`_persist` inside a watchdog handler **kills the observer thread**: watchdog's `dispatch_events` only catches `queue.Empty`. Violates observantic's own C-04 ("observer thread must never die"). | **High — monitoring dies** | watchdog 6.0.0 source |
| F3 | `eventic schema upgrade` fails: `CommandError: No 'script_location' key found in configuration` — the v1.1.0 wheel omits `alembic.ini` (untracked in the eventic repo at tag `v1.1.0`). `inspect`/`check`/`verify` work; SQLite auto-creates tables. | Medium — docs/ops | CLI run |
| F4 | `make_store("postgresql://...")` produces `ModuleNotFoundError: psycopg2` — SQLAlchemy's bare `postgresql://` dialect defaults to psycopg2, but eventic's `[postgres]` extra ships **psycopg3** (`psycopg[binary]>=3.2`). `postgresql+psycopg://` works. | Medium — documented URL broken | probe run |
| F5 | The 003 guide's Step 11 watcher smoke (`stream = s` in a subclass) raises `PydanticUserError` on pydantic ≥ 2.11: field overrides must be annotated. The annotated `stream: Stream = s` works. | Low — docs | probe run |

Minor items rolled into the work: `src/examples` is an implicit namespace
package (works, but `__init__.py` is cleaner), no committed outbox/worker
test exists (only the uncommitted Step 11 smoke), no Postgres integration
test exists (devenv.nix references `tests/test_postgres_integration.py`),
and version 0.3.0 needs a bump for the behavior changes.

## Plan (v0.4.0)

1. **P1 — Observer-thread safety** (`core/base.py` + monitors): `_persist`
   routes store errors to `on_error` instead of raising (unless
   `persist_strict`); new `_emit_safe()` for monitor handler threads so a
   model/persist error never kills the watchdog observer. Fixes F2.
2. **P2 — Serialized writes** (`core/base.py`): a module-level lock around
   `collection.create(...)` makes `:memory:` stores deterministic. Fixes F1.
3. **P3 — psycopg3 URL translation** (`_eventic.py`): bare `postgresql://`
   becomes `postgresql+psycopg://` inside `make_store`. Fixes F4.
4. **P4 — Docs for the migration gap** (README + `demo_app.py`):
   `schema upgrade` is Postgres-only and blocked on eventic v1.1.0's wheel
   missing `alembic.ini`; `Postgres(url)` auto-creates via `create_tables`
   default. Fixes F3.
5. **P5 — `examples` package** (`src/examples/__init__.py`).
6. **P6 — Committed outbox/worker test** (`tests/test_outbox_worker.py`).
7. **P7 — Opt-in Postgres integration test** (`tests/test_postgres_integration.py`,
   skipped unless `TEST_DATABASE_URL`).
8. **P8 — 0.4.0 release**: version bump, release notes, README updates
   (constructor `auto_persist`, `stream: Stream = s` annotation note,
   persist-error contract).
9. **P9 — Correction record**: the 003 guide's Step 11 smoke + Step 1
   verification are corrected in 004 (see IMPLEMENTATION_GUIDE.md Appendix C).

## Non-goals

- No changes to eventic itself (git dep pinned to `v1.1.0`); upstream gaps
  (F3 `alembic.ini`, F4 dialect default) are documented with workarounds.
- No async watchers, no new monitors, no API additions beyond `_emit_safe`.
- Postgres integration tests stay opt-in (no live Postgres with known
  credentials is reachable from this shell; the devenv service requires
  `devenv up` and its socket is not visible here).
