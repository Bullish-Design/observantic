"""
SQLite database monitoring mixin.

Change detection is snapshot-based: per table we keep ``{rowid: cells}`` and
diff it against a fresh read on every check. This correctly reports inserts,
updates, deletes and (with ``track_schema_changes``) DDL — unlike the old
rowid-checkpoint approach (C-01, H-15).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Literal

from eventic import Stream
from pydantic import BaseModel, Field, PrivateAttr
from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from ..core import EventWatcher
from ..exceptions import ConfigurationException, WatcherException

logger = logging.getLogger("observantic.sqlite")


class DatabaseRow(BaseModel):
    """A single row-level change."""

    table_name: str
    row_data: dict[str, Any] | None = None
    row_id: int | str | None = None
    operation: Literal["inserted", "updated", "deleted"] = "inserted"
    timestamp: float = Field(default_factory=time.time)

    model_config = {"frozen": True, "extra": "forbid"}


SQLITE_STREAM: Stream = Stream(DatabaseRow, name="sqlite")


class SchemaChange(BaseModel):
    """A schema-level change (DDL)."""

    tables_added: list[str]
    tables_dropped: list[str]
    tables_modified: list[str]
    timestamp: float = Field(default_factory=time.time)

    model_config = {"frozen": True, "extra": "forbid"}


class SQLiteEventBase(EventWatcher):
    """SQLite database monitoring mixin."""

    stream: Stream = SQLITE_STREAM

    poll_interval_seconds: float = Field(
        default=1.0,
        description="Background poll interval; 0 disables the poll thread",
    )
    track_schema_changes: bool = Field(
        default=True, description="Emit SchemaChange events on DDL"
    )
    db_connect_timeout_seconds: float = Field(
        default=5.0, description="sqlite3 connect timeout"
    )
    max_table_rows: int = Field(
        default=100_000,
        description="Skip snapshotting tables with more rows than this",
    )

    _observer: BaseObserver | None = PrivateAttr(default=None)
    _poll_thread: threading.Thread | None = PrivateAttr(default=None)
    _check_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _db_path: str | None = PrivateAttr(default=None)
    _snapshots: dict[str, dict[Any, tuple[Any, ...]]] = PrivateAttr(
        default_factory=dict
    )
    _schema: dict[str, str] = PrivateAttr(default_factory=dict)
    _pending_inserts: list[DatabaseRow] = PrivateAttr(default_factory=list)

    # ---- state machine extension points ---------------------------------- #

    def _validate_start(self, path: str) -> None:
        resolved = Path(path).resolve()
        if not resolved.exists():
            raise ConfigurationException(f"Database does not exist: {path}")
        self._db_path = str(resolved)

    def _start_impl(self, path: str, **kwargs: Any) -> None:
        # Reset all state — checkpoints/snapshots must not leak across
        # restarts (H-15).
        self._snapshots = {}
        self._schema = {}
        self._pending_inserts = []
        self._refresh_snapshot()  # seeds _snapshots + _schema

        assert self._db_path is not None
        self._observer = Observer()
        try:
            self._observer.schedule(
                self._create_handler(),
                str(Path(self._db_path).parent),
                recursive=False,
            )
            self._observer.start()
        except Exception as e:
            self._observer = None
            raise WatcherException(f"Failed to start database observer: {e}") from e

        if self.poll_interval_seconds > 0:
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll_thread.start()

    def _stop_impl(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is not None and observer.is_alive():
            observer.stop()
            observer.join(timeout=5)  # bounded join (H-19)

        poll = self._poll_thread
        self._poll_thread = None
        if poll is not None:
            poll.join(timeout=5)

    def _default_record_model(self) -> type[Any]:
        return DatabaseRow

    # ---- polling --------------------------------------------------------- #

    def _poll_loop(self) -> None:
        while self._watching:
            time.sleep(self.poll_interval_seconds)
            try:
                self._check_for_changes()
            except Exception as e:  # never escape the poll thread (C-04)
                self._safe_call("on_error", e, self._db_path)

    # ---- change detection ------------------------------------------------ #

    def _check_for_changes(self) -> None:
        if not self._db_path:
            return
        with self._check_lock:
            self._pending_inserts = []
            conn = sqlite3.connect(
                self._db_path, timeout=self.db_connect_timeout_seconds
            )
            try:
                with conn:
                    self._check_schema(conn)  # DDL events (H-16)
                    for table in self._list_rowid_tables(conn):
                        self._diff_table(conn, table)  # row events
            except sqlite3.Error as e:
                self._safe_call("on_error", e, self._db_path)  # always close (H-19)
            finally:
                conn.close()
            if self._pending_inserts:
                self._dispatch_hook(
                    "on_data_changed", self._db_path, list(self._pending_inserts)
                )

    def _refresh_snapshot(self) -> None:
        """Take a fresh snapshot of every rowid table + sqlite_master."""
        if not self._db_path:
            return
        conn = sqlite3.connect(self._db_path, timeout=self.db_connect_timeout_seconds)
        try:
            with conn:
                self._schema = self._read_schema(conn)
                for table in self._list_rowid_tables(conn):
                    result = self._read_table(conn, table)
                    if result is not None:
                        _, snapshot = result
                        self._snapshots[table] = snapshot
        finally:
            conn.close()

    def _check_schema(self, conn: sqlite3.Connection) -> None:
        if not self.track_schema_changes:
            return
        now_schema = self._read_schema(conn)
        if not self._schema or now_schema == self._schema:
            self._schema = now_schema
            return

        added = sorted(set(now_schema) - set(self._schema))
        dropped = sorted(set(self._schema) - set(now_schema))
        modified = sorted(
            name
            for name in set(now_schema) & set(self._schema)
            if now_schema[name] != self._schema[name]
        )
        if added or dropped or modified:
            change = SchemaChange(
                tables_added=added,
                tables_dropped=dropped,
                tables_modified=modified,
            )
            self._dispatch_hook("on_schema_changed", change)
        self._schema = now_schema

    def _diff_table(self, conn: sqlite3.Connection, table: str) -> None:
        result = self._read_table(conn, table)
        if result is None:
            return  # oversized or not rowid-compatible — skip
        cols, now = result
        old = self._snapshots.setdefault(table, {})

        for rid, cells in now.items():
            if rid not in old:
                self._emit_row(table, rid, cols, cells, "inserted")
            elif old[rid] != cells:
                self._emit_row(table, rid, cols, cells, "updated")
        for rid in old.keys() - now.keys():
            self._emit_row(table, rid, cols, None, "deleted")

        self._snapshots[table] = now

    def _emit_row(
        self,
        table: str,
        rid: Any,
        cols: list[str],
        cells: tuple[Any, ...] | None,
        operation: Literal["inserted", "updated", "deleted"],
    ) -> None:
        row_data = dict(zip(cols, cells, strict=False)) if cells is not None else None
        row = DatabaseRow(
            table_name=table, row_data=row_data, row_id=rid, operation=operation
        )
        self._emit(
            table_name=table,
            row_data=row_data,
            row_id=rid,
            operation=operation,
        )
        hook = {
            "inserted": "on_row_inserted",
            "updated": "on_row_updated",
            "deleted": "on_row_deleted",
        }[operation]
        self._dispatch_hook(hook, row)
        if operation == "inserted":
            self._pending_inserts.append(row)

    # ---- sqlite helpers -------------------------------------------------- #

    @staticmethod
    def _list_rowid_tables(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _read_schema(conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {name: (sql or "") for name, sql in rows}

    @staticmethod
    def _quote(name: str) -> str:
        """Quote an identifier — never f-string table names into SQL (H-15)."""
        return '"' + name.replace('"', '""') + '"'

    def _read_table(
        self, conn: sqlite3.Connection, table: str
    ) -> tuple[list[str], dict[Any, tuple[Any, ...]]] | None:
        qname = self._quote(table)
        if self.max_table_rows > 0:
            count = conn.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
            if count > self.max_table_rows:
                logger.warning(
                    "table %s has %d rows; skipping snapshot (max_table_rows=%d)",
                    table,
                    count,
                    self.max_table_rows,
                )
                return None
        try:
            cur = conn.execute(f"SELECT rowid, * FROM {qname}")
        except sqlite3.OperationalError:
            return None  # WITHOUT ROWID or otherwise not rowid-compatible
        # Drop the leading rowid column: snapshot values are row[1:].
        cols = [d[0] for d in cur.description][1:]
        snapshot = {row[0]: tuple(row[1:]) for row in cur.fetchall()}
        return cols, snapshot

    # ---- watchdog handler ------------------------------------------------ #

    def _create_handler(self) -> FileSystemEventHandler:
        parent = self

        class SQLiteHandler(FileSystemEventHandler):
            def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
                if not event.is_directory:
                    if str(Path(str(event.src_path)).resolve()) == parent._db_path:
                        parent._check_for_changes()

        return SQLiteHandler()
