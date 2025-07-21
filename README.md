# Observantic

Event monitoring library that bridges external events to Eventic Records through customizable hooks. Automatic event tracking and persistence.

## Installation

```bash
uv add observantic
```

## Quick Start

```python
from observantic import FileEventBase, init
from eventic import Record

# Initialize Eventic
init(name="my-app", database_url="postgresql://...")

# Define event record
class FileEvent(Record, FileEventBase):
    path: str
    event_type: str
    
    watch_patterns = ["*.pdf", "*.txt"]
    
    def on_file_created(self, event):
        FileEvent(path=event.src_path, event_type="created")

# Monitor files
watcher = FileEvent()
watcher.start_watching("/documents")

# Stop when done
watcher.stop_watching()
```

## Features

- **File Monitoring**: Watch directories for file changes
- **SQLite Monitoring**: Track row-level database changes
- **Webhook Server**: Receive HTTP POST events
- **Eventic Integration**: Automatic Record persistence
- **Hook System**: Register multiple callbacks per event
- **Lifecycle Hooks**: on_start, on_stop, on_error

## Watchers

### FileEventBase

Monitor file system events:

```python
class DocumentWatcher(Record, FileEventBase):
    watch_patterns = ["*.docx", "*.pdf"]
    
    def on_file_modified(self, event):
        print(f"Modified: {event.src_path}")
    
    def on_start(self):
        print("Started monitoring files")

watcher = DocumentWatcher()
watcher.start_watching("/documents", recursive=True)
```

### SQLiteEventBase

Track SQLite database changes:

```python
class DatabaseSync(Record, SQLiteEventBase):
    def on_data_changed(self, db_path, new_rows):
        for row in new_rows:
            print(f"New row in {row.table_name}: {row.row_data}")

sync = DatabaseSync()
sync.start_watching("/path/to/database.db")
```

### WebhookEventBase

HTTP webhook server:

```python
class WebhookReceiver(Record, WebhookEventBase):
    port = 8080
    webhook_paths = ["/webhook"]
    require_auth_header = "X-API-Key"
    require_auth_value = "secret"
    
    def on_webhook_received(self, event):
        print(f"Received: {event.body}")

server = WebhookReceiver()
server.start_watching()
```

## Configuration

Observantic uses environment variables (or `.env` files):

```bash
OBSERVANTIC_DB_URL=postgresql://user:pass@localhost/db
OBSERVANTIC_LOG_LEVEL=DEBUG
```

Access settings:

```python
from observantic import settings
print(settings.DB_URL)
```

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

```python
class SafeWatcher(Record, FileEventBase):
    def on_file_created(self, event):
        # Errors are caught and passed to on_error
        raise ValueError("Test error")
    
    def on_error(self, error, event):
        print(f"Error: {error} for event: {event}")
        # Watcher continues running
```

## Development

```bash
# Clone repository
git clone https://github.com/observantic/observantic.git
cd observantic

# Install with dev dependencies
uv sync

# Run tests
uv run pytest

# Format code
uv run ruff format

# Type check
uv run mypy observantic
```

## License

MIT

## Contributing

Issues and PRs welcome! Please ensure:
- All tests pass
- Code is formatted with ruff
- Type hints are complete
- Lines are ≤80 characters
