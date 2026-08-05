# Observantic — Adversarial Code Review #001

- **Date:** 2026-08-04
- **Commit reviewed:** `716687f` (main, clean tree)
- **Scope:** entire `src/` tree (core, monitors, config, exceptions, tests, examples) plus packaging (`pyproject.toml`), docs (`README.md`), and dev environment (`devenv.*`)
- **Method:** static analysis + targeted runtime reproduction against a live Postgres (throwaway cluster on `127.0.0.1:5544`) and live watchdog/webhook servers. Every finding below marked **REPRODUCED** was executed and observed, not just inferred.

## Repository facts

| Metric | Value |
|---|---|
| Library LOC | ~850 (`src/observantic/`) |
| Test LOC | 196 |
| Examples LOC | ~590 (one is **0 bytes**: `src/examples/app.py`) |
| Test suite status | **7/7 tests FAIL** (100 % red) |
| README Quick Start | **crashes at class definition** |
| SQLite monitor | **silent no-op** (never reports rows) |
| Webhook server | **DoS-able; shutdown can hang forever** |
| `pyproject.toml` version | `0.1.0` vs `__version__` `0.2.0` |

## Documents

| File | Contents |
|---|---|
| `review.md` | Full adversarial review (findings C-01…C-09, H-10…H-20) with reproductions |
| `refactoring-guide.md` | **Step-by-step refactoring guide** — 11 steps in 4 phases, per-step verification, Eventic contract & seam, finding→step map, commit sequence |
| `repros.py` | Run-time reproduction scripts for the critical findings |

## Findings index

| ID | Severity | Area | Summary |
|----|----------|------|---------|
| C-01 | CRITICAL | sqlite | Change detection is a silent no-op (`data_version` gate) |
| C-02 | CRITICAL | API/docs | README examples crash at class definition (PydanticUserError) |
| C-03 | CRITICAL | core | Record-based watchers crash on first event (`DBOSException`) / double-execute with `launch()` |
| C-04 | CRITICAL | core | Hook exceptions kill the watchdog observer thread (README promises resilience) |
| C-05 | CRITICAL | webhook | Single-threaded server → trivial DoS; `stop_watching()` hangs forever |
| C-06 | HIGH | webhook | Invalid/absent `Content-Length` → unhandled ValueError / silent body loss |
| C-07 | HIGH | examples | Production `webhook_server.py`: typer options silently ignored (pydantic field no-op) |
| C-08 | HIGH | core | “Automatic persistence” is fiction — `_emit()` constructs & discards non-Record models |
| C-09 | HIGH | tests | Test suite is 100 % broken |
| H-10 | HIGH | core | `start_watching()` leaves `_watching=True` on validation failure |
| H-11 | MED | webhook | Broken-pipe traceback spam; internal errors leaked to clients (`send_error(500, str(e))`) |
| H-12 | MED | core | `on_error` receives the event-name string, not the event |
| H-13 | MED | config | Documented env vars `OBSERVANTIC_DB_URL` / `OBSERVANTIC_LOG_LEVEL` do nothing |
| H-14 | MED | core | Two divergent settings singletons; config is otherwise dead code |
| H-15 | MED | sqlite | Rowid-diff misses updates/deletes/rowid reuse; stale checkpoints across restarts |
| H-16 | MED | sqlite | `track_schema_changes` declared but never used; no DDL events emitted |
| H-17 | MED | webhook | No body-size cap; auth optional; GET/PUT accepted; query params not URL-decoded |
| H-18 | MED | packaging | Version mismatch; `ruff`/`mypy` promised in README but absent; empty example shipped |
| H-19 | LOW | core | Error paths skip `conn.close()`; throttle map never pruned; joins without timeout |
| H-20 | LOW | misc | Unused exception hierarchy, unused imports, dead config fields, injection-prone SQL f-strings |

See `review.md` for the full write-up, and `refactoring-guide.md` for the fix plan.
