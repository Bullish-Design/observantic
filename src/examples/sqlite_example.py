#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.4.0",
#     "eventic>=1.1.0",
# ]
# ///
"""
SQLite monitoring example for Observantic.
Tracks row-level changes (inserts, updates, deletes) in a demo database.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from eventic import App, Stream
from eventic.sql import SQLite
from pydantic import BaseModel

from observantic import SQLiteEventBase


class RowEvent(BaseModel):
    """One emitted row-level change (the stream's state model)."""

    table_name: str = ""
    row_data: dict[str, Any] | None = None
    row_id: int | str | None = None
    operation: Literal["inserted", "updated", "deleted"] = "inserted"


rows = Stream(RowEvent, name="row_events")
app = App(id="sqlite-demo", streams=[rows])


class DatabaseSync(SQLiteEventBase):
    """Monitor SQLite changes.

    Prefer the per-row hooks below; ``on_data_changed`` is kept for
    backward compatibility and receives all rows inserted since the last
    check.
    """

    def on_row_inserted(self, row):
        print(f"➕ Inserted into {row.table_name}: {row.row_data}")

    def on_row_updated(self, row):
        print(f"✏️  Updated {row.table_name} row {row.row_id}: {row.row_data}")

    def on_row_deleted(self, row):
        print(f"🗑️  Deleted {row.table_name} row {row.row_id}")

    def on_schema_changed(self, change):
        print(f"🧬 Schema changed: +{change.tables_added} -{change.tables_dropped}")

    def on_start(self):
        print(f"Started monitoring database: {self._db_path}")


def setup_test_db(db_path: str):
    """Create test database."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def add_test_data(db_path: str):
    """Add rows, update one, delete one to trigger monitoring."""
    conn = sqlite3.connect(db_path)

    for i in range(3):
        conn.execute("INSERT INTO events (name) VALUES (?)", (f"Event {i + 1}",))
        conn.commit()
        print(f"  Added: Event {i + 1}")
        time.sleep(0.5)

    conn.execute("UPDATE events SET name='Updated Event' WHERE id=1")
    conn.commit()
    print("  Updated: row 1")

    conn.execute("DELETE FROM events WHERE id=2")
    conn.commit()
    print("  Deleted: row 2")

    conn.close()


def main():
    print("🚀 SQLite Monitor Demo")
    db_path = "example.db"
    setup_test_db(db_path)
    print(f"Monitoring database: {db_path}")

    store = SQLite("demo-events.db")
    runtime = app.bind(store)
    monitor = DatabaseSync(stream=rows, auto_persist=True)
    monitor.bind(runtime)
    monitor.start_watching(db_path)

    try:
        add_test_data(db_path)
        time.sleep(2)
    finally:
        monitor.stop_watching()
        store.close()

    print("\n✅ Monitoring complete")
    Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
