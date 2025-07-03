from __future__ import annotations

"""
SQLite database monitoring mixin.
"""

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, PrivateAttr
from watchdog.observers import Observer
from watchdog.events import FileModifiedEvent, FileSystemEventHandler

from ..core import EventWatcher, RecordMixin


class DatabaseRow(BaseModel):
    """Raw SQLite row data."""

    table_name: str
    row_data: dict[str, Any]
    row_id: int | str
    timestamp: float = Field(default_factory=time.time)

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }


class SQLiteEventBase(EventWatcher, RecordMixin):
    """SQLite database monitoring mixin."""

    poll_interval_seconds: float = Field(
        default=1.0, description="Check interval for database changes"
    )
    track_schema_changes: bool = Field(default=True, description="Monitor DDL changes")

    _observer: Optional[Observer] = PrivateAttr(default=None)
    _db_path: Optional[str] = PrivateAttr(default=None)
    _last_checkpoint: dict[str, int | str] = PrivateAttr(default_factory=dict)
    _last_data_version: Optional[int] = PrivateAttr(default=None)

    def start_watching(self, db_path: str, **kwargs: Any) -> None:
        """Begin monitoring SQLite database file."""
        super().start_watching(db_path)

        if not Path(db_path).exists():
            raise ValueError(f"Database does not exist: {db_path}")

        self._db_path = db_path
        self._observer = Observer()

        # Initialize checkpoints
        self._initialize_checkpoints()

        # Create handler for file changes
        handler = self._create_handler()

        try:
            self._observer.schedule(handler, str(Path(db_path).parent), recursive=False)
            self._observer.start()
        except Exception as e:
            self._watching = False
            raise RuntimeError(f"Failed to start database observer: {e}") from e

    def stop_watching(self) -> None:
        """Stop monitoring."""
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join()
            self._observer = None

        super().stop_watching()

    def _initialize_checkpoints(self) -> None:
        """Get initial row counts for all tables."""
        if not self._db_path:
            return

        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()

            # Get all tables
            tables = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()

            for (table_name,) in tables:
                # Get max rowid for each table
                try:
                    max_rowid = cursor.execute(
                        f"SELECT MAX(rowid) FROM {table_name}"
                    ).fetchone()[0]
                    if max_rowid:
                        self._last_checkpoint[table_name] = max_rowid
                except sqlite3.OperationalError:
                    # Table might not have rowid
                    pass

            # Get initial data_version if available
            try:
                version = cursor.execute("PRAGMA data_version").fetchone()[0]
                self._last_data_version = int(version)
            except:
                pass

            conn.close()
        except Exception as e:
            raise RuntimeError(f"Failed to initialize checkpoints: {e}") from e

    def _check_for_changes(self) -> None:
        """Check database for new rows and schema changes."""
        if not self._db_path:
            return

        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()

            # Check data_version for quick change detection
            current_version = None
            try:
                current_version = int(
                    cursor.execute("PRAGMA data_version").fetchone()[0]
                )
            except:
                pass

            if (
                current_version is not None
                and self._last_data_version is not None
                and current_version == self._last_data_version
            ):
                conn.close()
                return

            # Data has changed, check for new rows
            new_rows = []

            # Get all tables
            tables = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()

            for (table_name,) in tables:
                try:
                    # Build query for new rows
                    query = f"SELECT rowid, * FROM {table_name}"

                    last_rowid = self._last_checkpoint.get(table_name)
                    if last_rowid is not None:
                        query += f" WHERE rowid > {last_rowid}"

                    rows = cursor.execute(query).fetchall()

                    # Get column names
                    columns = [desc[0] for desc in cursor.description]

                    for row in rows:
                        rowid = row[0]
                        row_data = dict(zip(columns[1:], row[1:]))

                        db_row = DatabaseRow(
                            table_name=table_name, row_data=row_data, row_id=rowid
                        )

                        # Emit record
                        self._emit(
                            DatabaseRow,
                            table_name=table_name,
                            row_data=row_data,
                            row_id=rowid,
                        )

                        new_rows.append(db_row)

                        # Update checkpoint
                        if (
                            table_name not in self._last_checkpoint
                            or rowid > self._last_checkpoint[table_name]
                        ):
                            self._last_checkpoint[table_name] = rowid

                except sqlite3.OperationalError as e:
                    # Skip tables without rowid or other issues
                    pass

            conn.close()

            # Dispatch events
            if new_rows:
                self._dispatch_hook("on_data_changed", self._db_path, new_rows)

            if current_version is not None:
                self._last_data_version = current_version

        except Exception as e:
            self.on_error(e, self._db_path)
            raise RuntimeError(f"Failed to check for changes: {e}") from e

    def _create_handler(self) -> FileSystemEventHandler:
        """Create handler for SQLite file changes."""
        parent = self

        class SQLiteHandler(FileSystemEventHandler):
            def on_modified(self, event: FileModifiedEvent) -> None:
                if not event.is_directory and event.src_path == parent._db_path:
                    parent._check_for_changes()

        return SQLiteHandler()

    # Override in subclasses
    def on_data_changed(self, db_path: str, new_rows: list[DatabaseRow]) -> None:
        """Called when new rows detected in database."""
        pass
