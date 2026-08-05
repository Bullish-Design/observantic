"""SQLite monitor tests (Step 7): snapshot diffing, schema events, polling."""

from __future__ import annotations

import sqlite3
import time

import pytest
from watchdog.events import FileSystemEventHandler

from observantic import SQLiteEventBase
from observantic.exceptions import ConfigurationException


def wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "watch.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
    conn.commit()
    conn.close()
    return p


def insert(db, data):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO t (data) VALUES (?)", (data,))
    conn.commit()
    conn.close()


def test_inserts_detected(db):
    inserted, changed = [], []

    class W(SQLiteEventBase):
        def _create_handler(self):
            # Suppress watchdog delivery: only the poll thread checks, so the
            # three inserts land in a single deterministic diff.
            class Noop(FileSystemEventHandler):
                def on_modified(self, event):
                    pass

            return Noop()

        def on_row_inserted(self, row):
            inserted.append(row)

        def on_data_changed(self, db_path, new_rows):
            changed.append(new_rows)

    w = W(poll_interval_seconds=0.4)
    w.start_watching(str(db))
    try:
        insert(db, "a")
        insert(db, "b")
        insert(db, "c")  # all before the first poll tick
        assert wait_for(lambda: len(inserted) >= 3, timeout=3.0), inserted
        assert {r.row_id for r in inserted} == {1, 2, 3}
        assert all(r.operation == "inserted" for r in inserted)
        assert wait_for(lambda: len(changed) == 1)
        assert len(changed[0]) == 3  # all three rows in one on_data_changed
    finally:
        w.stop_watching()


def test_update_detected(db):
    """C-01/H-15: updates never fired with the old rowid-diff design."""
    updated = []

    class W(SQLiteEventBase):
        def on_row_updated(self, row):
            updated.append(row)

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO t (data) VALUES ('orig')")
    conn.commit()
    conn.close()

    w = W(poll_interval_seconds=0.1)
    w.start_watching(str(db))
    try:
        time.sleep(0.3)  # let the initial snapshot settle
        conn = sqlite3.connect(db)
        conn.execute("UPDATE t SET data='changed' WHERE id=1")
        conn.commit()
        conn.close()
        assert wait_for(lambda: len(updated) == 1), updated
        assert updated[0].operation == "updated"
        assert updated[0].row_id == 1
        assert updated[0].row_data == {"id": 1, "data": "changed"}
    finally:
        w.stop_watching()


def test_delete_detected(db):
    deleted = []

    class W(SQLiteEventBase):
        def on_row_deleted(self, row):
            deleted.append(row)

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO t (data) VALUES ('gone')")
    conn.commit()
    conn.close()

    w = W(poll_interval_seconds=0.1)
    w.start_watching(str(db))
    try:
        time.sleep(0.3)
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM t WHERE id=1")
        conn.commit()
        conn.close()
        assert wait_for(lambda: len(deleted) == 1), deleted
        assert deleted[0].operation == "deleted"
        assert deleted[0].row_id == 1
    finally:
        w.stop_watching()


def test_schema_change_detected(db):
    schema_events = []

    class W(SQLiteEventBase):
        def on_schema_changed(self, change):
            schema_events.append(change)

    w = W(poll_interval_seconds=0.1)
    w.start_watching(str(db))
    try:
        time.sleep(0.3)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert wait_for(lambda: len(schema_events) >= 1), schema_events
        assert "extra" in schema_events[-1].tables_added
    finally:
        w.stop_watching()


def test_schema_tracking_can_be_disabled(db):
    schema_events = []

    class W(SQLiteEventBase):
        def on_schema_changed(self, change):
            schema_events.append(change)

    w = W(poll_interval_seconds=0.1, track_schema_changes=False)
    w.start_watching(str(db))
    try:
        time.sleep(0.3)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE extra2 (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        time.sleep(0.6)
        assert schema_events == []
    finally:
        w.stop_watching()


def test_restart_resets_snapshots(db):
    """H-15: no stale checkpoints across restarts."""
    seen = []

    class W(SQLiteEventBase):
        def on_row_inserted(self, row):
            seen.append(row.row_id)

    w = W(poll_interval_seconds=0.1)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO t (data) VALUES ('pre')")
    conn.commit()
    conn.close()

    w.start_watching(str(db))
    try:
        time.sleep(0.3)  # snapshot taken with row 1 present
        assert seen == []  # pre-existing rows are not "new"
    finally:
        w.stop_watching()

    seen.clear()
    w.start_watching(str(db))  # restart — snapshots must be fresh
    try:
        insert(db, "post-restart")
        assert wait_for(lambda: len(seen) == 1), seen
        assert seen[0] == 2
    finally:
        w.stop_watching()


def test_start_validates_existing_path():
    w = SQLiteEventBase()
    with pytest.raises(ConfigurationException, match="does not exist"):
        w.start_watching("/no/such/db.sqlite")
    assert w._watching is False


def test_locked_database_reports_error_and_survives(db):
    """A locked DB → on_error, watcher stays alive (C-04/H-19)."""
    errors = []

    class W(SQLiteEventBase):
        def on_error(self, error, event=None):
            errors.append(error)

    w = W(poll_interval_seconds=0.05, db_connect_timeout_seconds=0.2)
    w.start_watching(str(db))
    try:
        time.sleep(0.3)
        # Hold an exclusive lock, then try to make the watcher check the DB.
        blocker = sqlite3.connect(db)
        blocker.execute("BEGIN EXCLUSIVE")
        time.sleep(0.6)
        assert len(errors) >= 1  # connect/check timed out → on_error
        assert w._watching is True  # watcher survived
        blocker.rollback()
        blocker.close()
    finally:
        w.stop_watching()


def test_poll_thread_detects_changes_without_file_events(db):
    """H-20: poll_interval_seconds is real — detects changes when the
    watchdog file event is suppressed."""
    inserted = []

    class W(SQLiteEventBase):
        def _create_handler(self):
            class Noop(FileSystemEventHandler):
                def on_modified(self, event):
                    pass  # suppress watchdog delivery; poll must detect

            return Noop()

        def on_row_inserted(self, row):
            inserted.append(row)

    w = W(poll_interval_seconds=0.1)
    w.start_watching(str(db))
    try:
        insert(db, "polled")
        assert wait_for(lambda: len(inserted) == 1, timeout=5.0), inserted
        assert inserted[0].row_data == {"id": 1, "data": "polled"}
    finally:
        w.stop_watching()
