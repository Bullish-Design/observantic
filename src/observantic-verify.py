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
Quick verification that Observantic is working correctly.
"""

from __future__ import annotations

import time
from pathlib import Path

from eventic import Eventic, Record
from observantic import FileEventBase


# Initialize Eventic
Eventic.init(
    name="observantic-verify",
    database_url="postgresql://user:pass@localhost/verify_db"
)


class SimpleFileEvent(Record, FileEventBase):
    """Minimal file monitoring example."""
    
    path: str
    event_type: str
    
    def on_file_created(self, event):
        print(f"✅ File created: {event.src_path}")
        SimpleFileEvent(path=event.src_path, event_type="created")


def main():
    print("🔍 Verifying Observantic installation...\n")
    
    # Create test directory
    test_dir = Path("./verify_test")
    test_dir.mkdir(exist_ok=True)
    
    # Start monitoring
    watcher = SimpleFileEvent()
    watcher.start_watching(str(test_dir))
    print(f"👀 Watching: {test_dir}")
    
    # Create test file
    time.sleep(0.5)
    test_file = test_dir / "verify.txt"
    test_file.write_text("Observantic works!")
    
    # Wait for event
    time.sleep(1)
    
    # Check if record was created
    records = SimpleFileEvent._store.find_by_properties(
        {"path": str(test_file)}
    )
    
    if records:
        print("\n✅ Success! Observantic is working correctly.")
        print(f"   - Record created with ID: {records[0]}")
    else:
        print("\n❌ No records found. Check your setup.")
    
    # Cleanup
    watcher.stop_watching()
    test_file.unlink(missing_ok=True)
    test_dir.rmdir()


if __name__ == "__main__":
    main()
