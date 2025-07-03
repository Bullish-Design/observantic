#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.2.0",
#     "eventic>=0.1.5",
# ]
# ///
"""
SQLite monitoring example for Observantic.
Demonstrates tracking row-level database changes.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from eventic import Record
from observantic import SQLiteEventBase, init


# Initialize Eventic
init(
    name="sqlite-monitor-demo",
    database_url="postgresql://user:pass@localhost/demo"
)


class DatabaseSync(Record, SQLiteEventBase):
    """Monitor SQLite changes and sync to Eventic."""
    
    table: str
    operation: str
    row_count: int = 0
    
    def on_data_changed(self, db_path, new_rows):
        """Process detected changes."""
        print(f"📊 Database changed: {len(new_rows)} new rows")
        for row in new_rows:
            print(f"  - {row.table_name}: {row.row_id}")
        self.row_count += len(new_rows)
    
    def on_start(self):
        """Called when monitoring starts."""
        print(f"Started monitoring database: {self._db_path}")


def setup_test_db(db_path: str):
    """Create test database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def add_test_data(db_path: str):
    """Add rows to trigger monitoring."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    for i in range(3):
        cursor.execute(
            "INSERT INTO events (name) VALUES (?)",
            (f"Event {i+1}",)
        )
        conn.commit()
        print(f"  Added: Event {i+1}")
        time.sleep(1)
    
    conn.close()


def main():
    """Run SQLite monitoring demo."""
    print("🚀 SQLite Monitor Demo")
    
    db_path = "example.db"
    setup_test_db(db_path)
    
    print(f"Monitoring database: {db_path}")
    print("Adding test data...\n")
    
    monitor = DatabaseSync()
    monitor.start_watching(db_path)
    
    try:
        add_test_data(db_path)
        time.sleep(2)  # Let final events process
    finally:
        monitor.stop_watching()
    
    print(f"\n✅ Monitoring complete - processed {monitor.row_count} rows")
    
    # Cleanup
    Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
