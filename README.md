# Observantic

Event monitoring library that bridges external events — filesystem changes,
SQLite row changes, and HTTP webhooks — to Eventic Records through
customizable hooks.

## Installation

```bash
uv add observantic
```

## Quick Start

```python
from observantic import FileEventBase
from eventic import Record


# Define an event record. Because this watcher subclasses Eventic's `Record`,
# every event emits an instance of *your* record (see "Persistence").
class FileEvent(Record, FileEventBase):
    path: str = ""
    event_type: str = ""

    # Class-level configuration must be annotated (pydantic requirement).
    watch_patterns: list[str] = ["*.pdf", "*.txt"]

    def on_file_created(self, event):
        print(f"Created: {event.src_path}")


# Monitor files
watcher = FileEvent()
watcher.start_watching("/documents")

# Stop when done
watcher.stop_watching()
```

## Watchers

### FileEventBase

Monitor file system events (watchdog):

```python
class DocumentWatcher(FileEventBase):
    watch_patterns: list[str] = ["*.docx", "*.pdf"]
    event_throttle_seconds: float = 0.1

    def on_file_modified(self, event):
        print(f"Modified: {event.src_path}")

    def on_file_moved(self, event):
        print(f"Moved: {event.src_path} → {event.dest_path}")

    def on_start(self):
        print("Started monitoring files")


watcher = DocumentWatcher()
watcher.start_watching("/documents", recursive=True)
```

Hooks: `on_file_created`, `on_file_modified`, `on_file_deleted`,
`on_file_moved`, plus lifecycle `on_start` / `on_stop` / `on_error`.

### SQLiteEventBase

Track row-level SQLite changes — inserts, **updates**, **deletes**, and DDL:

```python
class DatabaseSync(SQLiteEventBase):
    poll_interval_seconds: float = 1.0  # background poll, 0 disables it
    track_schema_changes: bool = True

    def on_row_inserted(self, row):
        print(f"Inserted into {row.table_name}: {row.row_data}")

    def on_row_updated(self, row):
        print(f"Updated row {row.row_id}: {row.row_data}")

    def on_row_deleted(self, row):
        print(f"Deleted row {row.row_id}")

    def on_schema_changed(self, change):
        print(f"Schema: +{change.tables_added} -{change.tables_dropped}")


sync = DatabaseSync()
sync.start_watching("/path/to/database.db")
```

`on_data_changed(db_path, new_rows)` remains available for backward
compatibility: it fires once per check with all rows inserted since the last
check. Change detection is snapshot-based (per-table `{rowid: cells}` diffs);
there is no `PRAGMA data_version` gate.

### WebhookEventBase

HTTP webhook server (threaded, bounded):

```python
class WebhookReceiver(WebhookEventBase):
    port: int = 8080
    webhook_paths: list[str] = ["/webhook"]
    require_auth_header: str | None = "X-API-Key"
    require_auth_value: str | None = "secret"

    def on_webhook_received(self, event):
        print(f"Received: {event.body}")


server = WebhookReceiver()
server.start_watching()
```

Security defaults (all configurable):

* `max_body_bytes: int = 1_048_576` — larger requests get `413` without being read.
* `Content-Length` is validated strictly (invalid → `400`; absent → empty body).
* Per-request socket `timeout = 30` — idle clients cannot wedge the server.
* `allowed_methods: list[str] = ["POST", "PUT"]` — other methods get `405`.
* Auth uses constant-time comparison; `require_auth_header` and
  `require_auth_value` must be set together (validated at `start_watching`).
* Hook failures produce a generic `500 {"error": "internal"}` — the real
  exception goes to `on_error`, never to the client — and the server keeps
  serving subsequent requests.
* `stop_watching()` closes live connections and returns promptly.

## Configuration

Observantic reads settings from the environment **once, at import time**:

```bash
OBSERVANTIC_DB_URL=postgresql://user:pass@localhost/db   # preferred
OBSERVANTIC_LOG_LEVEL=DEBUG
```

`DB_URL` / `LOG_LEVEL` (without the prefix) are accepted as
backward-compatible aliases. When both spellings are set, the
`OBSERVANTIC_`-prefixed variable wins.

```python
from observantic import settings

print(settings.DB_URL)
```

## Persistence

Persistence is **opt-in and explicit** — the library does not write to
Eventic unless you initialize it (`init(...)`) and your watcher uses a
`Record`-based model.

* Every watcher constructs an event record per event via `_emit()`. For a
  `Record`-based watcher this is *your* record subclass; otherwise it is the
  monitor's internal model (`FileRecord`, `DatabaseRow`, `WebhookRecord`).
* **Durable v0 (Eventic 0.1.5+):** when a store is wired (`init(...)`),
  constructing a `Record`-based event record persists its initial version row
  automatically and fires `@on.create` handlers — no `launch()` needed.
  Later mutations (in your hooks) write new versions and fire `@on.update`.
* `auto_persist: bool = False` — when `True`, each emitted record is also
  explicitly appended to its Eventic store (an idempotent re-append for
  Record models, thanks to `ON CONFLICT DO NOTHING`).
* `persist_strict: bool = False` — when `True`, a missing Eventic backend
  raises `ConfigurationException` instead of logging a warning.
* The emitted record is returned from `_emit()`; hooks receive the raw event
  object (watchdog event, `DatabaseRow`, or `WebhookEvent`).

```python
from observantic import init, FileEventBase
from eventic import Record

# Eventic 0.1.5 may be initialized ONCE per process; init() is idempotent
# (repeated calls return the singleton). Use observantic.reset() to tear it
# down, e.g. in tests or multi-app processes.
init(name="my-app", database_url=settings.DB_URL)


class FileEvent(Record, FileEventBase):
    path: str = ""
    event_type: str = ""
    auto_persist: bool = True  # explicit append (idempotent)


watcher = FileEvent()
# Every emitted record now has its v0 row in the store; @on.create handlers
# fire; further Record mutations create new versions.
```

Note: Eventic declares a DBOS queue per `Record` subclass **at class-definition
 time**, keyed by class name — so `Record` subclass names must be unique per
process (two classes with the same name raise at definition time).

Hooks run synchronously in the observer's thread. Eventic 0.1.5 wraps **only**
methods explicitly marked `@evented` (opt-in); observantic's dispatch resolves
the raw function either way, so hooks run exactly once and never touch DBOS
queues. If you want DBOS queue semantics, register an explicitly-decorated
callback via `register_hook` and set `dispatch_direct=False`.

## Hook Registration

Register multiple callbacks without subclassing:

```python
def log_file(event):
    print(f"File: {event.src_path}")


def backup_file(event):
    shutil.copy(event.src_path, "/backup")


watcher = FileEventBase()
watcher.register_hook("on_file_created", log_file)
watcher.register_hook("on_file_created", backup_file)
watcher.start_watching("/important")
```

## Error Handling

Hook errors are reported via `on_error(error, event)` and **do not stop the
watcher** — the observer thread keeps running:

```python
class SafeWatcher(FileEventBase):
    def on_file_created(self, event):
        raise ValueError("Test error")

    def on_error(self, error, event):
        print(f"Error: {error} for event: {event}")
        # Monitoring continues.
```

Set `raise_on_hook_error=True` to collect hook errors instead of swallowing
them (`_dispatch_hook` returns the last error; the webhook monitor uses this
to answer `500` on hook failure).

Validation happens up front: `start_watching` with a bad path, missing
database, or inconsistent auth config raises a typed
`ConfigurationException` (from `observantic.exceptions`) before any state is
flipped.

## Development

```bash
uv sync --group dev

# Run tests (DB-backed tests skip unless TEST_DATABASE_URL is set)
uv run pytest

# Format and lint
uv run ruff format --check .
uv run ruff check .

# Type check
uv run mypy src/observantic
```

## License

MIT

## Contributing

Issues and PRs welcome! Please ensure:

* All tests pass (`uv run pytest`)
* Code is formatted with ruff and passes `ruff check`
* Type hints are complete (`uv run mypy src/observantic`)
