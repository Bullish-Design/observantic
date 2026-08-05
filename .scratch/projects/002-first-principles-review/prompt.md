# Observantic — First-Principles Reimagination & Adversarial Review Prompt

**Role:** You are an adversarial senior architect reviewing Observantic. Your job is **not**
a bug hunt — it is to step back, question every design decision from first principles, and
produce a concrete plan to reimplement this library as the best, cleanest, most elegant
codebase and architecture possible. Treat the current implementation as *one possible
answer*, never as the floor.

**Inputs you must read before writing anything:**
- `README.md` — the documented promise.
- `src/observantic/__init__.py`, `_eventic.py`, `config.py`, `exceptions.py`
- `src/observantic/core/base.py` — the heart.
- `src/observantic/monitors/{file,sqlite,webhook}.py`
- `src/examples/*.py`
- `tests/*.py` — 85 tests; assess their *quality*, not just their pass rate.
- `.scratch/projects/001-code-review/review.md` + `refactoring-guide.md` — the previous
  review cycle and how the current code came to be. You must do better than it did.
- `pyproject.toml`, `devenv.nix`, `devenv.yaml`

---

## 0. Environment & ground rules

- **All in-repo commands run inside the devenv shell:** `devenv shell uv run pytest`,
  `devenv shell uv run ruff check .`, `devenv shell uv run mypy src/observantic`,
  `devenv shell uv build`.
- **Every claim must be verified empirically.** If you assert a deficiency, reproduce it
  with a runnable script (add it under your scratch dir as `repros.py`, self-contained).
  If you cannot reproduce it, say so — do not speculate as fact.
- **The tree must stay green at the end of each phase.** Never leave the suite broken.
- **The external contract is eventic 0.1.5** (git `9a6c2e2`, DBOS 2.29, SQLite-capable).
  You may critique how observantic *uses* eventic, but not demand eventic change. All
  eventic interaction stays behind one seam (`src/observantic/_eventic.py`).
- **Honesty is mandatory.** Score harshly, and include a "What's actually good" section —
  a reimplementation must not lose what genuinely works.
- Keep lines ≤ 88 chars; the codebase follows ruff (format + lint) and mypy clean.

---

## 1. Mission

Reimagine Observantic from first principles and produce:

1. **A first-principles analysis** — what is the *essence* of this library? Derive the
   minimal core from the goal, not from the current code.
2. **An adversarial audit** of the current architecture (below).
3. **A reimplementation proposal** — the ideal target architecture, file-by-file, with an
   honest migration path and a verification plan. Concrete enough that a follow-up session
   can implement it phase-by-phase with the tree green at every step.

## 2. First-principles analysis (Phase 1)

Answer these *before* looking for flaws, and write the answers down:

- What is the library's true job? Candidate phrasing: "connect external event sources
  (filesystem, SQLite, HTTP) to Eventic Records via user code." Is that the right job?
  Is the *eventic coupling* essential or incidental to the core value?
- Who are the users, and what are the three or four primary workflows? Which current
  features serve none of them?
- What is the smallest coherent core? What would you delete entirely?
- What are the invariants that must never break (observer survival, no double-execution,
  fail-fast start, honest persistence, bounded resource usage)? State them as a contract.
- Is "monitor" (base classes you subclass: `FileEventBase`, …) the right abstraction?
  Compare against: composition (a `Watcher` + source plugins/strategies), a plain
  callback registry, an async framework, or a declarative event-source DSL.
- Is `EventWatcher` being a pydantic `BaseModel` (frozen `Record` mixins, `PrivateAttr`
  runtime state, fields-as-config) the right shape? Challenge it. What breaks? What would
  a plain dataclass/class design buy?
- Is the stringly-typed hook dispatch (`_dispatch_hook("on_file_created", event)` +
  `register_hook(name, fn)` + override methods resolved via `call_unwrapped`) the right
  mechanism, or is there something more type-safe and less magical?
- Is the `record_model` / `auto_persist` / `persist_strict` persistence model coherent
  with eventic 0.1.5's durable-v0 semantics, or is it fighting the platform?
- Is the threading model (watchdog observer thread + sqlite poll thread + webhook
  `ThreadingHTTPServer` threads, all funneling into one hook-dispatch path) sound?
  Where are the races, deadlocks, and lifetime bugs?
- Should the library be synchronous at all? Is the `run_async` placeholder a lie?

## 3. Adversarial audit (Phase 2)

Audit each dimension with the standard above (verified, reproduced, scored):

1. **Public API** — is `__all__` the right surface? Are the base-class names
   (`FileEventBase`) good? Is `init`/`reset`/`is_eventic_ready` the right eventic façade?
2. **The core state machine** — `start_watching`/`stop_watching`/`_validate_start`/
   `_start_impl`/`_stop_impl`/`_safe_call`. Edge cases: start failure mid-impl, stop from
   within a hook (poll thread), concurrent start/stop, watcher reuse after stop, GC of
   threads on interpreter exit.
3. **Dispatch & hooks** — error containment, `raise_on_hook_error`, `_last_hook_error`
   semantics, ordering guarantees, re-entrancy, the webhook 500 path reading shared state.
4. **The seam** — is `_eventic.py` the right boundary? Is `persist()`'s contract
   (idempotent append, EventicNotReadyError) the right one? Is `call_unwrapped` /
   `dispatch_direct` still necessary given eventic 0.1.5 only wraps `@evented` methods?
5. **Config** — pydantic settings + env fallback: is the `confidantic` dependency still
   justified (nothing imports it)? Is snapshot-at-import the right choice?
6. **Each monitor** — file (throttle map, patterns, moved events), sqlite (snapshot diffs,
   poll loop, schema events, `max_table_rows`, locking), webhook (request parsing, auth,
   size caps, timeouts, shutdown, HTTP/1.1 keep-alive). Find the remaining gaps.
7. **Examples & packaging** — is a top-level `examples` namespace package good? The
   `start` entry point? The wheel contents? The `# /// script` headers?
8. **Tests** — are 85 passing tests the right tests? Which are slow/flaky/white-box?
   What is *not* tested (property tests, concurrency stress, real sockets, resource leaks,
   eventic integration)? Do the tests over-couple to private state (`_watching`,
   `_observer`, `_dispatch_hook`)?
9. **Docs** — does the README's promise match reality sentence by sentence?

Each finding: **ID** (R-01…), severity (Critical/High/Medium/Low), file:line, a **verified
reproduction** (or an explicit "could not reproduce"), impact, and a fix direction. Also
keep a running list of **R-removals**: things that should be deleted outright.

## 4. The reimplementation proposal (Phase 3)

Deliver a concrete target:

- **Target layout** (file-by-file) and public API sketch (signatures, not prose).
- The core data flow: source → event → user code → (optionally) Eventic. Show it.
- **Design decisions**, each with a one-paragraph rationale and the rejected alternatives.
- **What changes for users** (migration notes from the current API).
- **What is removed** and why.
- **Phased implementation plan** mirroring the previous guide's discipline: each phase ends
  with a runnable verification command and keeps the tree green. Sequence foundation →
  monitors → consumers → docs, and state exactly which current tests survive, which are
  rewritten, and which new tests are added (including at least one concurrency test and
  one resource-leak test per monitor).
- **Definition of done** for the reimplementation.

## 5. Deliverables (write these files)

Create `.scratch/projects/002-first-principles-review/` containing:

- `review.md` — the full audit: executive summary, Phase-1 analysis, findings (R-01…
  R-XX) with severities and verified repros, removals list, "what's actually good", and a
  final verdict score per dimension (1–10).
- `proposal.md` — the Phase-3 reimplementation plan (layout, API sketch, decisions,
  migration, phased plan, definition of done).
- `repros.py` — every reproduction script, self-contained and runnable with
  `devenv shell uv run python .scratch/projects/002-first-principles-review/repros.py`.
- `prompt.md` — this prompt (copy it verbatim).

## 6. Verdict format

End `review.md` with a table: for each of API design, core architecture, threading,
persistence contract, monitoring correctness, test quality, docs, packaging — score 1–10
for the **current** implementation and the **target** you propose, with a one-line
justification. The gap between the two is the work estimate.

---

*Remember: the previous review found 16 issues and the code was rewritten. The bar for
this review is higher: the deliverable is not a list of bugs, it is the best possible
design for this library, argued and verified.*
