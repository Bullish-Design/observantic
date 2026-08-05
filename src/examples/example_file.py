#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic @ git+https://github.com/Bullish-Design/observantic",
#     "eventic @ git+https://github.com/Bullish-Design/eventic",
# ]
# ///
"""
File monitoring example for Observantic.
Watches the current directory for documents and prints hook callbacks.
"""

from __future__ import annotations

import time
from pathlib import Path

from eventic import Record

from observantic import FileEventBase


class DocumentEvent(Record, FileEventBase):
    """Monitor documents; each event emits an instance of this Record.

    Because this watcher subclasses Eventic's ``Record``, ``_emit()`` creates
    *your* record (C-08). All class-level configuration must be annotated
    (pydantic requirement); give every Record field a default so the watcher
    can be built without a live store.
    """

    # Eventic Record fields (defaults so no store/DB is required).
    path: str = ""
    event_type: str = ""
    size: int = 0

    # Configure monitoring — annotated overrides (C-02).
    watch_patterns: list[str] = ["*.pdf", "*.txt", "*.docx", "*.md", "*.py"]

    # Persistence is opt-in. Call observantic.init(...) once to wire a store,
    # then set auto_persist=True (Eventic 0.1.5 also persists a durable v0 row
    # at construction and fires @on.create handlers). No launch() required.
    auto_persist: bool = False

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
