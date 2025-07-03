#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic @ git+https://github.com/Bullish-Design/observantic",
#     "eventic @ git+https://github.com/Bullish-Design/eventic",
#     "python-dotenv",
# ]
# ///
"""
File monitoring example for Observantic.
Demonstrates watching a directory for PDF and text files.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from eventic import Record
from observantic import FileEventBase, init

from dotenv import load_dotenv

load_dotenv()

POSTGRES_DB = os.environ["POSTGRES_DB"]
POSTGRES_USER = os.environ["POSTGRES_USER"]
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]

db_url = (
    "postgresql://"
    + POSTGRES_USER
    + ":"
    + POSTGRES_PASSWORD
    + "@localhost/"
    + POSTGRES_DB
)

print(f"\nConnecting to Postgres at {db_url}\n")


# Initialize Eventic
init(name="file-monitor-demo", database_url=db_url)


class DocumentEvent(Record, FileEventBase):
    """Monitor documents and create Records for each event."""

    path: str
    event_type: str
    size: int = 0

    # Configure monitoring
    watch_patterns = ["*.pdf", "*.txt", "*.docx"]

    def on_file_created(self, event):
        """Handle new files."""
        file_path = Path(event.src_path)
        size = file_path.stat().st_size if file_path.exists() else 0

        # Record already emitted by parent class
        print(f"📄 Created: {file_path.name} ({size} bytes)")

    def on_file_modified(self, event):
        """Handle file modifications."""
        print(f"📝 Modified: {Path(event.src_path).name}")

    def on_file_deleted(self, event):
        """Handle file deletions."""
        print(f"🗑️  Deleted: {Path(event.src_path).name}")

    def on_start(self):
        """Called when monitoring starts."""
        print(f"Started monitoring: {self._watch_path}")


def main():
    """Run the file monitoring demo."""
    print("🚀 File Monitor Demo")
    print("Watching current directory for documents...")
    print("Press Ctrl+C to stop\n")

    watcher = DocumentEvent()
    watcher.start_watching(".")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n✅ Monitoring stopped")
    finally:
        watcher.stop_watching()


if __name__ == "__main__":
    main()
