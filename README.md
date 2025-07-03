# Observantic

[![PyPI version](badge-url)](pypi-url)
[![Python versions](badge-url)](python-3.13)
[![License](badge-url)](MIT-license-url)

Event watching, with a pydantic flavour.

Eventic is an event monitoring library that bridges external events to Eventic Records through customizable hooks.

Observantic provides composable mixins for monitoring file systems and databases, creating immutable Records for each detected change. Works standalone or integrates seamlessly with Eventic for shared database persistence.

## Installation

### Using UV (Recommended)
```bash
uv add observantic
```

### Using pip
```bash
pip install observantic
```

### Development Installation
```bash
git clone https://github.com/user/observantic.git
cd observantic
uv sync
```

## Quick Start

```python
# Basic file monitoring with Eventic integration
from observantic import FileEventBase
from eventic import Eventic, Record

# Initialize shared database
Eventic.init(name="my-app", database_url="postgresql://...")

# Define your event record
class FileEvent(Record, FileEventBase):
    path: str
    event_type: str
    
    def on_file_created(self, event):
        # Create immutable record for each file creation
        FileEvent(path=event.src_path, event_type="created")

# Start monitoring
watcher = FileEvent()
watcher.start_watching("/path/to/monitor")

## Core Concepts

### Key Abstractions
- **EventWatcher**: Base mixin providing hook registration and event dispatching
- **FileEventBase**: Watchdog-powered file system monitoring with customizable hooks
- **Hook System**: Register callbacks for specific events without subclassing

### Design Philosophy
Hook-based architecture lets developers control exactly how external events become immutable Records.

## Usage

### Basic Operations

Monitor directories and create Records for each file event:

```python
from observantic import FileEventBase
from eventic import Eventic, Record

# Initialize Eventic for shared database
Eventic.init(
    name="monitoring-app",
    database_url="postgresql://user:pass@localhost/db"
)

class DocumentEvent(Record, FileEventBase):
    path: str
    event_type: str
    size: int = 0
    
    def on_file_created(self, event):
        # Each call creates a new immutable Record
        DocumentEvent(
            path=event.src_path,
            event_type="created",
            size=event.stat().st_size
        )
    
    def on_file_modified(self, event):
        # Find existing records for this path
        records = DocumentEvent._store.find_by_properties({"path": event.src_path})
        if records:
            # Hydrate most recent and update
            doc = DocumentEvent.hydrate(records[0])
            doc.event_type = "modified"
            doc.size = event.stat().st_size

# Start monitoring
watcher = DocumentEvent()
watcher.start_watching("/documents", recursive=True)
```

### Advanced Features

Register multiple hooks without subclassing:

```python
from observantic import EventWatcher

# Create hook functions
def log_event(event):
    print(f"Event detected: {event}")

def create_audit_record(event):
    AuditRecord(path=event.src_path, timestamp=datetime.now())

# Register hooks
watcher = EventWatcher()
watcher.register_hook("on_file_created", log_event)
watcher.register_hook("on_file_created", create_audit_record)
watcher.start_watching("/logs")
```

### Configuration

Control monitoring behavior:

```python
class ConfiguredWatcher(Record, FileEventBase):
    # Filter events
    watch_patterns = ["*.pdf", "*.docx"]
    ignore_patterns = ["~*", ".*"]
    
    # Throttle high-frequency events
    event_throttle_seconds = 0.5
    
    # Custom lifecycle
    def on_start(self):
        print(f"Starting monitor for {self.watch_path}")
    
    def on_stop(self):
        print("Monitoring stopped")
```

### Error Handling

Built-in resilience for production use:

```python
class ResilientWatcher(Record, FileEventBase):
    def on_file_modified(self, event):
        try:
            process_file(event.src_path)
        except ProcessingError as e:
            # Errors don't crash the watcher
            self.log_error(e, event)
    
    def on_error(self, error, event=None):
        # Central error handling
        ErrorRecord(
            error_type=type(error).__name__,
            message=str(error),
            event_path=event.src_path if event else None
        )
```

## API Reference

### Classes

#### EventWatcher
Abstract base mixin providing core event monitoring functionality.

**Methods:**
- `register_hook(event_name: str, callback: Callable)`: Add callback for event type
- `unregister_hook(event_name: str, callback: Callable)`: Remove specific callback
- `start_watching(path: str, **kwargs)`: Begin monitoring specified path
- `stop_watching()`: Stop monitoring and cleanup resources

**Hook Methods (override in subclasses):**
- `on_start()`: Called when monitoring begins
- `on_stop()`: Called when monitoring ends
- `on_error(error: Exception, event: Any)`: Handle errors during processing

#### FileEventBase
File system monitoring using watchdog library.

**Parameters:**
- `watch_patterns` (List[str]): File patterns to monitor (default: ["*"])
- `ignore_patterns` (List[str]): Patterns to ignore (default: [])
- `event_throttle_seconds` (float): Minimum seconds between events per file
- `recursive` (bool): Monitor subdirectories (default: True)

**Hook Methods:**
- `on_file_created(event: FileSystemEvent)`: New file detected
- `on_file_modified(event: FileSystemEvent)`: File content changed
- `on_file_deleted(event: FileSystemEvent)`: File removed
- `on_file_moved(event: FileSystemMovedEvent)`: File renamed/moved

**Example:**
```python
class ImageProcessor(Record, FileEventBase):
    watch_patterns = ["*.jpg", "*.png"]
    
    def on_file_created(self, event):
        ImageRecord(
            path=event.src_path,
            timestamp=datetime.now()
        )
```

#### SQLiteEventBase
SQLite database monitoring through file watching + change detection.

**Additional Parameters:**
- `poll_interval_seconds` (float): Check interval for data_version
- `track_schema_changes` (bool): Monitor DDL changes (default: True)

**Hook Methods:**
- `on_data_changed(db_path: str, old_version: int, new_version: int)`: Data modified
- `on_schema_changed(db_path: str, changes: List[str])`: Schema altered

**Example:**
```python
class DatabaseSync(Record, SQLiteEventBase):
    def on_data_changed(self, db_path, old_version, new_version):
        # Trigger sync workflow
        SyncRecord(
            database=db_path,
            from_version=old_version,
            to_version=new_version
        )
```

### Functions

#### find_by_path(path: str, event_type: str = None)
Helper to retrieve Records associated with a specific file path.

**Returns:** List[Record] - Records matching the path criteria

**Example:**
```python
# Find all events for a specific file
events = FileEvent.find_by_path("/documents/report.pdf")
for event in events:
    print(f"{event.event_type} at version {event.version}")
```

## Architecture

### Overview
Observantic extends Eventic's Record system with event monitoring capabilities. External events trigger hook methods that create immutable Records, leveraging Eventic's copy-on-write persistence.

### Data Flow
1. **Event Detection**: Watchdog detects file system changes
2. **Hook Dispatch**: EventWatcher routes to registered callbacks
3. **Record Creation**: Hooks create/update Eventic Records
4. **Persistence**: Eventic handles versioning and database storage

### Extension Points
- **Custom Watchers**: Inherit from EventWatcher for new event sources
- **Hook Decorators**: Add cross-cutting concerns (logging, metrics)
- **Event Filters**: Implement custom filtering logic
- **Storage Backends**: Use any Eventic-supported database

### Performance Considerations
- Events are processed synchronously to ensure order
- Throttling prevents overwhelming the system
- Watchdog uses native OS APIs for efficiency
- Records are written in DBOS transactions for consistency

## Examples

### Use Case 1: Document Processing Pipeline
Monitor directory for PDFs, extract text, and track processing status:

```python
from observantic import FileEventBase
from eventic import Eventic, Record
import fitz  # PyMuPDF

class PDFDocument(Record, FileEventBase):
    path: str
    status: str = "pending"
    page_count: int = 0
    extracted_text: str = ""
    
    watch_patterns = ["*.pdf"]
    
    def on_file_created(self, event):
        # Create initial record
        doc = PDFDocument(
            path=event.src_path,
            status="processing"
        )
        
        # Extract text in background
        Eventic.queue("pdf_processing").enqueue(
            self.process_pdf, doc.id
        )
    
    @Eventic.step()
    def process_pdf(self, doc_id):
        doc = PDFDocument.hydrate(doc_id)
        
        with fitz.open(doc.path) as pdf:
            doc.page_count = len(pdf)
            doc.extracted_text = "\n".join(
                page.get_text() for page in pdf
            )
            doc.status = "completed"

# Usage
watcher = PDFDocument()
watcher.start_watching("/incoming/documents")
```

### Use Case 2: Multi-Database Synchronization
Monitor SQLite changes and sync to PostgreSQL:

```python
class DatabaseReplicator(Record, SQLiteEventBase):
    source_db: str
    last_sync_version: int = 0
    
    def on_data_changed(self, db_path, old_version, new_version):
        # Create sync job record
        job = SyncJob(
            source=db_path,
            from_version=old_version,
            to_version=new_version,
            status="pending"
        )
        
        # Queue replication work
        Eventic.queue("replication").enqueue(
            replicate_changes, job.id
        )

# Monitor multiple databases
for db_path in ["/data/app1.db", "/data/app2.db"]:
    replicator = DatabaseReplicator(source_db=db_path)
    replicator.start_watching(db_path)
```

### Integration Examples

#### With FastAPI
```python
from fastapi import FastAPI
from observantic import FileEventBase

app = Eventic.create_app("monitor-api", db_url=DB_URL)

class UploadMonitor(Record, FileEventBase):
    def on_file_created(self, event):
        # Process uploads automatically
        UploadRecord(
            filename=event.src_path,
            user_id=extract_user_from_path(event.src_path)
        )

# Start monitoring on app startup
@app.on_event("startup")
async def start_monitors():
    monitor = UploadMonitor()
    monitor.start_watching("/uploads")

@app.get("/recent-uploads")
def get_recent_uploads():
    return UploadRecord.find_recent(limit=10)
```

#### With Eventic DBOS Workflows
```python
@Eventic.workflow()
def file_processing_workflow(file_path: str):
    # Create event record
    event = FileEvent(path=file_path, status="received")
    
    # Process with retry logic
    result = process_with_retry(event.id)
    
    # Update final status
    event.status = "completed" if result else "failed"
    
    return event.id
```

## Development

### Project Structure
```
observantic/
├── src/observantic/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   └── watcher.py
│   ├── monitors/
│   │   ├── __init__.py
│   │   ├── file.py
│   │   └── sqlite.py
│   └── hooks/
│       ├── __init__.py
│       └── registry.py
├── tests/
├── pyproject.toml
└── README.md
```

### Running Tests
```bash
uv run pytest
```

### Code Quality
```bash
uv run ruff check
uv run ruff format
uv run mypy src/
```

## Technical Specifications

### Requirements
- Python 3.13+
- eventic>=0.1.5
- watchdog>=6.0.0
- pydantic>=2.0

### Compatibility
- Cross-platform file monitoring via watchdog
- PostgreSQL for Eventic integration
- SQLite monitoring through file watching + polling

### Limitations
- File system events subject to OS-specific delays
- SQLite monitoring requires polling for external changes