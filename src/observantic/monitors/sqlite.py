"""
SQLite database monitoring mixin.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from pydantic import BaseModel
from watchdog.observers import Observer
from watchdog.events import FileModifiedEvent, FileSystemEventHandler

from ..core.base import EventWatcher
from ..exceptions import WatcherException


class DatabaseRow(BaseModel):
    """Raw SQLite row data."""
    table_name: str
    row_data: dict[str, Any]
    row_id: int | str


class SQLiteEventBase(EventWatcher):
    """SQLite database monitoring mixin."""
    
    poll_interval_seconds: float = 1.0
    track_schema_changes: bool = True
    
    _observer: Observer | None = None
    _db_path: str | None = None
    _last_checkpoint: dict[str, int | str] = {}  # table -> last rowid
    _last_data_version: int | None = None
    
    def start_watching(self, db_path: str) -> None:
        """Begin monitoring SQLite database file."""
        super().start_watching(db_path)
        
        if not Path(db_path).exists():
            raise WatcherException(f"Database does not exist: {db_path}")
        
        self._db_path = db_path
        self._observer = Observer()
        
        # Initialize checkpoints
        self._initialize_checkpoints()
        
        # Create handler for file changes
        handler = _SQLiteHandler(self)
        
        try:
            self._observer.schedule(
                handler, 
                str(Path(db_path).parent), 
                recursive=False
            )
            self._observer.start()
        except Exception as e:
            self._watching = False
            raise WatcherException(
                f"Failed to start database observer: {e}"
            ) from e
    
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
            raise WatcherException(
                f"Failed to initialize checkpoints: {e}"
            ) from e
    
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
            
            if (current_version is not None and 
                self._last_data_version is not None and 
                current_version == self._last_data_version):
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
                        
                        new_rows.append(DatabaseRow(
                            table_name=table_name,
                            row_data=row_data,
                            row_id=rowid
                        ))
                        
                        # Update checkpoint
                        if (table_name not in self._last_checkpoint or 
                            rowid > self._last_checkpoint[table_name]):
                            self._last_checkpoint[table_name] = rowid
                
                except sqlite3.OperationalError as e:
                    # Skip tables without rowid or other issues
                    pass
            
            conn.close()
            
            # Dispatch events
            if new_rows:
                self._dispatch_hook("on_data_changed", self._db_path, new_rows)
            
            if current_version is not None:
                if (self._last_data_version is not None and 
                    current_version != self._last_data_version):
                    self._dispatch_hook(
                        "on_data_changed", 
                        self._db_path,
                        self._last_data_version,
                        current_version
                    )
                self._last_data_version = current_version
        
        except Exception as e:
            self.on_error(e, self._db_path)
            raise WatcherException(
                f"Failed to check for changes: {e}"
            ) from e
    
    # Override in subclasses
    def on_data_changed(
        self, 
        db_path: str, 
        new_rows: list[DatabaseRow] | Any = None,
        *args
    ) -> None:
        """Called when new rows detected in database."""
        pass


class _SQLiteHandler(FileSystemEventHandler):
    """Internal handler for SQLite file changes."""
    
    def __init__(self, watcher: SQLiteEventBase):
        self.watcher = watcher
    
    def on_modified(self, event: FileModifiedEvent) -> None:
        if (not event.is_directory and 
            event.src_path == self.watcher._db_path):
            self.watcher._check_for_changes()
