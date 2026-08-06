# Observantic

Event monitoring library that bridges external events — filesystem changes,
SQLite row changes, and HTTP webhooks — to eventic streams through
customizable hooks.

## Installation

```bash
uv add observantic
```

## Quick Start

```python
from pydantic import BaseModel
from eventic import App, Stream
from eventic.sql import SQLite
from observantic import FileEventBase


class FileEvent(BaseModel):
    path: str = ""
    event_type: str = ""
    is_directory: bool = False


files = Stream(FileEvent, name="files")
app = App(id="my-app", streams=[files])


class DocumentWatcher(FileEventBase):
    watch_patterns: list[str] = ["*.pdf", "*.txt"]

    def on_file_created(self, event):
        print(f"Created: {event.src_path}")


# Monitor files (no database required)
watcher = DocumentWatcher(stream=files)
watcher.start_watching("/documents")
watcher.stop_watching()

# Persistence is explicit: bind a store, opt in per watcher
store = SQLite("observantic.db")
watcher.bind(app.bind(store))
watcher.auto_persist = True
watcher.start_watching("/documents")
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
`OBSERVANTIC_`-prefixed variable wins. The default is
`sqlite:///observantic.db` (SQLite for dev/test; use `postgresql://` in
production).

```python
from observantic import settings

print(settings.DB_URL)
```

## Persistence

Eventic 1.1.0 is a **versioned document store** with declaration-based apps.
Watchers emit **plain pydantic state** into a **`Stream`**; commits go
through a **`Collection`** obtained from `app.bind(store)`.

* Every watcher declares a `stream` whose model is the persisted state
  contract. Monitors ship with default streams — `FILE_STREAM` (`files`),
  `SQLITE_STREAM` (`sqlite`), `WEBHOOK_STREAM` (`webhooks`) — built from the
  internal record models (`FileRecord`, `DatabaseRow`, `WebhookRecord`).
  Custom models must accept the monitor's emit fields (a mismatch raises
  `pydantic.ValidationError` loudly, never silently drops data).
* `watcher.bind(runtime)` resolves `runtime[stream]`; `auto_persist=True`
  commits each emitted state as a **new aggregate** (revision 0) via
  `collection.create(...)`. `persist_strict=True` turns the "not bound"
  warning into a `ConfigurationException`.

```python
from eventic import App, Stream
from eventic.sql import SQLite
from observantic import FileEventBase


class FileEvent(BaseModel):
    path: str = ""
    event_type: str = ""
    is_directory: bool = False


files = Stream(FileEvent, name="files")
app = App(id="my-app", streams=[files])

store = SQLite("observantic.db")  # dev/test backend
runtime = app.bind(store)

watcher = FileEventBase(stream=files, auto_persist=True)
watcher.bind(runtime)
watcher.start_watching("/documents")
```

* **Writes are compare-and-swap**: `collection.change(base, **fields)` and
  `collection.replace(base, state)` raise `RevisionConflict` on a stale
  base. Reads: `get(id)`, `get(id, revision=n)`, `history(id)`,
  `where(**filters)`.
* **Backends**: `SQLite` for dev/test/single-process; `Postgres` for
  production (`pip install eventic[postgres]`). Schema is created
  automatically by `SQLite`; for Postgres run
  `eventic --app myapp:app --url "$DATABASE_URL" schema upgrade`.
* **Delivery**: hooks are in-process and best-effort. For durable delivery
  declare `Subscription`s (`Inline()` or `Outbox(queue=...)`) on the App and
  run `eventic worker --queue q`. Outbox is at-least-once — handlers must be
  idempotent.
* **Schema evolution**: bump `stream.schema_version` and declare upcasters
  (`eventic.evolution.make_upcaster`); `eventic schema check` exits 3 on
  model drift.
* **Removed in 0.3.0**: `init()`/`reset()`/`is_eventic_ready()`,
  `Record`-based watchers, `@on.create`, `auto_persist` re-appends, DBOS
  queues. Data written by 0.2.0/eventic 0.1.5 is **not readable** by 0.3.0 —
  re-ingest (greenfield schema).

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

# Run tests (SQLite backend by default; no Postgres needed)
uv run pytest

# Format and lint
uv run ruff format --check .
uv run ruff check .

# Type check
uv run mypy src/observantic
```

Optional Postgres integration tests: install `eventic[postgres]` and point
`TEST_DATABASE_URL` at a database (e.g. the devenv Postgres at
`postgresql://postgres:postgres@127.0.0.1:5432/eventic`).

## License

MIT

## Contributing

Issues and PRs welcome! Please ensure:

* All tests pass (`uv run pytest`)
* Code is formatted with ruff and passes `ruff check`
* Type hints are complete (`uv run mypy src/observantic`)
