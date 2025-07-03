#!/usr/bin/env python
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "observantic",
#     "eventic>=0.1.5",
#     "watchdog>=6.0.0",
# ]
# ///
"""
Observantic demonstration showing file and database monitoring.
"""

from __future__ import annotations

import time
import sqlite3
from pathlib import Path
from datetime import datetime

from eventic import Eventic, Record
from observantic import FileEventBase, SQLiteEventBase


# Initialize Eventic (required for Record persistence)
Eventic.init(
    name="observantic-demo",
    database_url="postgresql://user:pass@localhost/observantic_demo"
)


# Example 1: File monitoring with Record creation
class DocumentEvent(Record, FileEventBase):
    """Monitor documents and create immutable records for each event."""
    
    path: str
    event_type: str
    size: int = 0
    timestamp: datetime = datetime.now()
    
    # Configure file monitoring
    watch_patterns = ["*.pdf", "*.docx", "*.txt"]
    event_throttle_seconds = 0.5
    
    def on_file_created(self, event):
        """Create new record when file is created."""
        file_path = Path(event.src_path)
        DocumentEvent(
            path=event.src_path,
            event_type="created",
            size=file_path.stat().st_size if file_path.exists() else 0,
            timestamp=datetime.now()
        )
        print(f"📄 Created: {file_path.name}")
    
    def on_file_modified(self, event):
        """Update record when file is modified."""
        # Find existing records for this path
        records = DocumentEvent._store.find_by_properties(
            {"path": event.src_path}
        )
        
        if records:
            # Hydrate most recent and update
            doc = DocumentEvent.hydrate(records[0])
            doc.event_type = "modified"
            doc.size = Path(event.src_path).stat().st_size
            doc.timestamp = datetime.now()
            print(f"📝 Modified: {Path(event.src_path).name} (v{doc.version})")
    
    def on_file_deleted(self, event):
        """Mark file as deleted."""
        records = DocumentEvent._store.find_by_properties(
            {"path": event.src_path}
        )
        
        if records:
            doc = DocumentEvent.hydrate(records[0])
            doc.event_type = "deleted"
            doc.timestamp = datetime.now()
            print(f"🗑️  Deleted: {Path(event.src_path).name}")


# Example 2: SQLite monitoring
class DataSync(Record, SQLiteEventBase):
    """Monitor SQLite database and sync changes."""
    
    source_db: str
    records_processed: int = 0
    last_sync: datetime = datetime.now()
    
    def on_data_changed(self, db_path, new_rows):
        """Process new rows from SQLite database."""
        print(f"\n📊 Database changed: {len(new_rows)} new rows")
        
        for row in new_rows:
            print(f"  - {row.table_name}: {row.row_id}")
            
            # Create record for each row (example)
            DataRecord(
                table=row.table_name,
                data=row.row_data,
                source_db=db_path,
                synced_at=datetime.now()
            )
        
        # Update sync status
        self.records_processed += len(new_rows)
        self.last_sync = datetime.now()


class DataRecord(Record):
    """Record representing synced data."""
    table: str
    data: dict
    source_db: str
    synced_at: datetime


def create_test_database(db_path: str):
    """Create a test SQLite database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()


def add_test_data(db_path: str, name: str):
    """Add a row to test database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO events (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()


def main():
    """Run the demonstration."""
    print("🚀 Observantic Demo\n")
    
    # Setup test directories
    test_dir = Path("./test_monitor")
    test_dir.mkdir(exist_ok=True)
    
    db_path = "./test.db"
    create_test_database(db_path)
    
    # Start file monitoring
    print("📁 Starting file monitoring...")
    file_watcher = DocumentEvent()
    file_watcher.start_watching(str(test_dir))
    
    # Start database monitoring
    print("🗄️  Starting database monitoring...")
    db_sync = DataSync(source_db=db_path)
    db_sync.start_watching(db_path)
    
    print("\n⏳ Running demo...\n")
    
    # Create test files
    time.sleep(1)
    test_file = test_dir / "test_document.txt"
    test_file.write_text("Hello, Observantic!")
    
    time.sleep(2)
    test_file.write_text("Updated content!")
    
    # Add database rows
    time.sleep(1)
    add_test_data(db_path, "First event")
    
    time.sleep(1)
    add_test_data(db_path, "Second event")
    
    # Let events process
    time.sleep(2)
    
    # Show results
    print("\n📈 Results:")
    print(f"  - Documents tracked: {len(DocumentEvent._store.find_by_properties({}))} unique files")
    print(f"  - Database rows synced: {db_sync.records_processed}")
    
    # Cleanup
    file_watcher.stop_watching()
    db_sync.stop_watching()
    
    # Clean up test files
    test_file.unlink(missing_ok=True)
    Path(db_path).unlink(missing_ok=True)
    test_dir.rmdir()
    
    print("\n✅ Demo complete!")


if __name__ == "__main__":
    main()
