"""Keyed-aggregate persistence for SQLiteEventBase (key_aggregates=True).

Validated semantics (eventic 1.1.0/1.1.1):
* create(state, id=key) on an existing aggregate: identical content -> replay
  no-op; different content -> RevisionConflict (the rowid-reuse fallback).
* collection.get(key) -> NotFound for pre-existing rows never emitted
  (first snapshot); persist_row creates on first change/delete instead.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid5

from eventic import App, Stream
from eventic.sql import SQLite

from observantic._eventic import persist_row, sqlite_aggregate_key
from observantic.monitors.sqlite import DatabaseRow, SQLiteEventBase

KEYED_STREAM = Stream(DatabaseRow, name="keyed_rows")


def row(operation: str, rid: int, data: dict | None = None) -> DatabaseRow:
    return DatabaseRow(table_name="t", row_id=rid, row_data=data, operation=operation)


def col_fixture():
    store = SQLite(":memory:")
    runtime = App(id="keyed", streams=[KEYED_STREAM]).bind(store)
    return store, runtime[KEYED_STREAM]


# -- unit: persist_row ------------------------------------------------------


def test_aggregate_key_is_deterministic():
    k1 = sqlite_aggregate_key("t", 42)
    k2 = sqlite_aggregate_key("t", 42)
    assert k1 == k2 == uuid5(NAMESPACE_URL, "observantic:sqlite:t:42")
    assert sqlite_aggregate_key("t", 42) != sqlite_aggregate_key("t", 43)


def test_insert_update_delete_single_aggregate_history():
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 1, {"id": 1, "data": "a"}), keyed=True)
        persist_row(col, row("updated", 1, {"id": 1, "data": "b"}), keyed=True)
        persist_row(col, row("deleted", 1), keyed=True)  # tombstone
        r = col.get(sqlite_aggregate_key("t", 1))
        assert r.revision == 2
        assert r.state.operation == "deleted"
        assert [
            x.revision for x in col.history(sqlite_aggregate_key("t", 1)).items
        ] == [0, 1, 2]
    finally:
        store.close()


def test_distinct_rowids_distinct_aggregates():
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 1, {"id": 1}), keyed=True)
        persist_row(col, row("inserted", 2, {"id": 2}), keyed=True)
        assert col.get(sqlite_aggregate_key("t", 1)).revision == 0
        assert col.get(sqlite_aggregate_key("t", 2)).revision == 0
        assert len(col.where(table_name="t").items) == 2
    finally:
        store.close()


def test_rowid_reuse_appends_to_existing_aggregate():
    """A rowid reused after a delete must continue the aggregate, not clash."""
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 1, {"id": 1}), keyed=True)
        persist_row(col, row("deleted", 1), keyed=True)
        persist_row(col, row("inserted", 1, {"id": 1, "data": "again"}), keyed=True)
        r = col.get(sqlite_aggregate_key("t", 1))
        assert r.revision == 2  # insert -> replace on the head (rev 1 -> 2)
        assert [
            x.revision for x in col.history(sqlite_aggregate_key("t", 1)).items
        ] == [0, 1, 2]
    finally:
        store.close()


def test_pre_existing_row_delete_creates_fallback():
    """A row deleted before it was ever emitted must create, not NotFound."""
    store, col = col_fixture()
    try:
        persist_row(col, row("deleted", 99), keyed=True)
        r = col.get(sqlite_aggregate_key("t", 99))
        assert r.revision == 0
        assert r.state.operation == "deleted"
    finally:
        store.close()


def test_keyed_false_keeps_legacy_random_aggregates():
    store, col = col_fixture()
    try:
        persist_row(col, row("inserted", 7, {"id": 7}), keyed=False)
        assert len(col.where(row_id=7).items) == 1
        assert col.where(row_id=7).items[0].revision == 0
    finally:
        store.close()


def test_keyed_watcher_warns_without_bind(caplog):
    """key_aggregates alone is not persistence; the base warn/strict applies."""
    w = SQLiteEventBase(key_aggregates=True, auto_persist=True)
    with caplog.at_level(logging.WARNING, logger="observantic"):
        w._emit(table_name="t", row_data=None, row_id=1, operation="deleted")
    assert "not bound" in caplog.text


# -- integration: real monitor through the poll loop ------------------------


def _build_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
    conn.execute("INSERT INTO t (id, data) VALUES (5, 'pre-existing')")
    conn.commit()
    conn.close()


def _mutate(db, op, rid, data=None):
    conn = sqlite3.connect(db)
    if op == "insert":
        conn.execute("INSERT INTO t (data) VALUES (?)", (data,))
    elif op == "update":
        conn.execute("UPDATE t SET data=? WHERE id=?", (data, rid))
    else:
        conn.execute("DELETE FROM t WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def wait_for(predicate, timeout=6.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_monitor_keyed_integration(tmp_path):
    db = tmp_path / "keyed.db"
    _build_db(str(db))
    seen = []

    class W(SQLiteEventBase):
        def on_row_inserted(self, row):
            seen.append(("inserted", row.row_id))

        def on_row_updated(self, row):
            seen.append(("updated", row.row_id))

        def on_row_deleted(self, row):
            seen.append(("deleted", row.row_id))

    store = SQLite(":memory:")
    runtime = App(id="keyed-live", streams=[KEYED_STREAM]).bind(store)
    w = W(
        stream=KEYED_STREAM,
        key_aggregates=True,
        auto_persist=True,
        poll_interval_seconds=0.1,
    )
    w.bind(runtime)
    w.start_watching(str(db))
    try:
        time.sleep(0.4)  # initial snapshot seeds row 5 (never emitted)
        _mutate(str(db), "insert", None, "fresh")
        time.sleep(0.3)
        _mutate(str(db), "update", 5, "changed")  # pre-existing -> update path
        time.sleep(0.3)
        _mutate(str(db), "delete", 5, None)
        time.sleep(0.3)
        assert wait_for(lambda: ("inserted", 6) in seen), seen
        assert wait_for(lambda: ("updated", 5) in seen), seen
        assert wait_for(lambda: ("deleted", 5) in seen), seen
    finally:
        w.stop_watching()

    col = runtime[KEYED_STREAM]
    agg5 = col.get(sqlite_aggregate_key("t", 5))
    assert agg5.state.operation == "deleted"
    assert agg5.revision >= 1  # updated + deleted appends
    assert col.get(sqlite_aggregate_key("t", 6)).revision == 0
    store.close()
