#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.3.0",
#     "eventic>=1.1.0",
# ]
# ///
"""
File monitoring example for Observantic.
Watches the current directory for documents; each event is committed to the
`documents` stream when persistence is wired.
"""

from __future__ import annotations

import time
from pathlib import Path

from eventic import App, Stream
from eventic.sql import SQLite
from pydantic import BaseModel

from observantic import FileEventBase


class DocumentEvent(BaseModel):
    """One emitted file event (the stream's state model).

    Must accept the monitor's emit fields: path, event_type, is_directory,
    dest_path (moved events). See IMPLEMENTATION_GUIDE.md Appendix A.
    """

    path: str = ""
    event_type: str = ""
    is_directory: bool = False
    dest_path: str | None = None


documents = Stream(DocumentEvent, name="documents")
app = App(id="file-demo", streams=[documents])


class DocumentWatcher(FileEventBase):
    """Monitor documents; each event emits a DocumentEvent."""

    watch_patterns: list[str] = ["*.pdf", "*.txt", "*.docx", "*.md", "*.py"]

    def on_file_created(self, event):
        src = Path(event.src_path)
        size = src.stat().st_size if src.exists() else 0
        print(f"📄 Created: {src.name} ({size} bytes)")

    def on_file_modified(self, event):
        print(f"📝 Modified: {Path(event.src_path).name}")

    def on_file_deleted(self, event):
        print(f"🗑️  Deleted: {Path(event.src_path).name}")

    def on_file_moved(self, event):
        print(f"➡️  Moved: {Path(event.src_path).name} → {Path(event.dest_path).name}")

    def on_start(self):
        print(f"Started monitoring: {self._watch_path}")


def main():
    """Run the file monitoring demo."""
    print("🚀 File Monitor Demo")
    print("Watching the current directory for documents...")
    print("Press Ctrl+C to stop\n")

    store = SQLite("demo.db")  # or observantic.make_store(settings.DB_URL)
    runtime = app.bind(store)

    watcher = DocumentWatcher(stream=documents, auto_persist=True)
    watcher.bind(runtime)
    watcher.start_watching(".")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n✅ Monitoring stopped")
    finally:
        watcher.stop_watching()
        store.close()


if __name__ == "__main__":
    main()
